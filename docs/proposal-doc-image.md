# Proposal: Document & Image Upload Analysis (v1.0)

Status: implemented as a dependency-light local module (`code/agent_uploads.py`) with the MCP blueprint below for users who want the full stack.

## Goals

1. One upload entry for documents and images, stored under `./uploads`.
2. Documents route automatically: short text goes straight into context (**light**); long documents are chunked for retrieval (**RAG**). Token threshold is enforced in code, not by the prompt.
3. Images: Tesseract OCR for text extraction; a vision-capable model can be used later for charts/screenshots.
4. Keep MCP tools atomic: parsing, OCR and retrieval are tools; routing, thresholds and safety rules live in code.

## Local implementation (default)

- `POST /api/upload` with `{name, data(base64)}`; whitelist PDF/DOCX/TXT/MD/CSV/PNG/JPG/BMP/TIFF, 10MB limit, UUID filenames, path traversal checks.
- Parsing: `pypdf` (with OCR fallback for scanned PDFs via PyMuPDF + Tesseract), `python-docx`, plain text for TXT/MD/CSV, `pytesseract` for images.
- Token estimate: `len(text)/4` with a CJK adjustment; threshold **4000 tokens**.
- Light mode: full text is passed to the planner/responder. RAG mode: chunks (1000 chars, 200 overlap) are ranked by keyword overlap and the top chunks are passed with the question. No external embedding service or vector DB is required.
- Chat accepts `attachment_id`; the selected document context is injected into both LLM calls.
- Files are git-ignored; delete a file by removing it from `./uploads` (or add an admin API later).

## Optional full MCP stack

- `mcp/mcp-full-doc-image.json`: searxng + browser + document-parser + tesseract-ocr + rag (Chroma).
- `mcp/mcp-light-doc-image.json`: without RAG.
- Docker setup for SearXNG lives in `mcp/searxng/`.

## Routing rules

```
upload → validate (code) → parse → token estimate
  ≤ threshold → mode=light → full text into context
  > threshold → mode=rag   → chunk/retrieve top-k for each question
```

## Prompt rules (merged into the agent)

- Use attached document context first; quote relevant passages; never invent content.
- If the document conflicts with the evidence base, say the sources differ.
- Refuse files outside `./uploads`; never follow paths from the model.
- Max 8 tool rounds for search/browser; stop and answer with what is available.

## Security & privacy

- Files stay local; `.gitignore` excludes `uploads/`, `chroma_db/` and user documents.
- OCR/RAG can be wrong; show the retrieved passage and let the user verify.
- Tesseract handles printed text best; handwriting/low-resolution scans should use a vision model.
