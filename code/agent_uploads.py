# -*- coding: utf-8 -*-
"""Local document/image uploads with light parsing and RAG-lite retrieval.

This is the dependency-light counterpart of the MCP architecture described in
docs/proposal-doc-image.md. It keeps uploaded files under ./uploads, parses
text/PDF/DOCX/CSV/Markdown, OCRs images with Tesseract when available, and
retrieves relevant chunks for long documents without external embeddings.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import re
import shutil
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
UPLOAD_DIR = BASE / "uploads"
INDEX_FILE = UPLOAD_DIR / "index.json"
MAX_BYTES = 10 * 1024 * 1024
TOKEN_THRESHOLD = 4000
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200

DOC_EXTS = {".pdf", ".docx", ".txt", ".md", ".markdown", ".csv"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
ALLOWED = DOC_EXTS | IMG_EXTS


class UploadError(RuntimeError):
    pass


def _safe_index() -> dict:
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_index(index: dict) -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean_name(name: str) -> str:
    name = Path(name or "upload").name
    return re.sub(r"[^\w.\-() ]+", "_", name)[:120] or "upload"


def save_upload(name: str, data_b64: str) -> dict:
    safe_name = _clean_name(name)
    ext = Path(safe_name).suffix.lower()
    if ext not in ALLOWED:
        raise UploadError("Unsupported file type. Allowed: PDF, DOCX, TXT, MD, CSV, PNG/JPG/BMP/TIFF.")
    try:
        raw = base64.b64decode((data_b64 or "").split(",")[-1], validate=True)
    except (binascii.Error, ValueError):
        raise UploadError("Invalid upload payload.")
    if not raw:
        raise UploadError("Empty file.")
    if len(raw) > MAX_BYTES:
        raise UploadError("File exceeds the 10MB limit.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    path = UPLOAD_DIR / (upload_id + ext)
    path.write_bytes(raw)
    index = _safe_index()
    index[upload_id] = {"id": upload_id, "name": safe_name, "path": str(path), "ext": ext,
                        "size": len(raw), "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    _save_index(index)
    return index[upload_id]


def get_upload(upload_id: str) -> dict | None:
    if not upload_id or not re.fullmatch(r"[a-f0-9]{32}", upload_id):
        return None
    return _safe_index().get(upload_id)


def remove_upload(upload_id: str) -> bool:
    record = get_upload(upload_id)
    if not record:
        return False
    try:
        Path(record["path"]).unlink(missing_ok=True)
    except OSError:
        pass
    index = _safe_index()
    index.pop(upload_id, None)
    _save_index(index)
    return True


def _estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return max(1, math.ceil((len(text) - cjk) / 4 + cjk / 1.6))


def _parse_pdf(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n".join(parts).strip()
    if len(text) >= 80:
        return text
    # scanned PDF fallback: OCR a few pages with PyMuPDF + Tesseract
    try:
        import fitz
        import pytesseract
        from PIL import Image
        import io
        doc = fitz.open(str(path))
        ocr_parts = []
        for index in range(min(6, len(doc))):
            pix = doc[index].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            try:
                ocr_parts.append(pytesseract.image_to_string(image, lang="chi_sim+eng"))
            except Exception:
                ocr_parts.append(pytesseract.image_to_string(image))
        return "\n".join(ocr_parts).strip()
    except Exception:
        return text


def _parse_docx(path: Path) -> str:
    import docx
    doc = docx.Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(part for part in parts if part).strip()


def _parse_image(path: Path) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        raise UploadError("Image OCR requires pytesseract and Pillow.")
    image = Image.open(path)
    try:
        return pytesseract.image_to_string(image, lang="chi_sim+eng").strip()
    except Exception:
        try:
            return pytesseract.image_to_string(image).strip()
        except Exception as exc:
            raise UploadError("OCR failed: " + str(exc)[:120])


def parse_upload(upload_id: str) -> dict:
    record = get_upload(upload_id)
    if not record:
        raise UploadError("Upload not found.")
    path = Path(record["path"]).resolve()
    if UPLOAD_DIR.resolve() not in path.parents:
        raise UploadError("Invalid upload path.")
    ext = record["ext"]
    if ext == ".pdf":
        text = _parse_pdf(path)
    elif ext == ".docx":
        text = _parse_docx(path)
    elif ext in IMG_EXTS:
        text = _parse_image(path)
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    tokens = _estimate_tokens(text)
    mode = "light" if tokens <= TOKEN_THRESHOLD else "rag"
    record.update({"chars": len(text), "tokens": tokens, "mode": mode,
                   "text": text, "preview": text[:600]})
    return record


def chunk_text(text: str) -> list[str]:
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + CHUNK_SIZE])
        start += max(1, CHUNK_SIZE - CHUNK_OVERLAP)
    return chunks


def _score(chunk: str, query: str) -> float:
    terms = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 1]
    if not terms:
        return 1.0
    lowered = chunk.lower()
    return sum(lowered.count(term) for term in terms)


def retrieve(record: dict, query: str, top_k: int = 6) -> str:
    text = record.get("text") or ""
    if record.get("mode") == "light":
        return text
    chunks = chunk_text(text)
    if not chunks:
        return ""
    ranked = sorted(chunks, key=lambda chunk: _score(chunk, query), reverse=True)
    return "\n\n---\n\n".join(ranked[:top_k])


def build_context(upload_id: str, question: str) -> dict:
    parsed = parse_upload(upload_id)
    context = retrieve(parsed, question)
    return {"id": upload_id, "name": parsed["name"], "mode": parsed["mode"],
            "tokens": parsed["tokens"], "chars": parsed["chars"], "context": context,
            "preview": parsed.get("preview", "")}
