# -*- coding: utf-8 -*-
"""OpenECRS local web app.

OpenECRS is an LLM-required agent: the model plans and writes every answer; the
offline tools only retrieve grounded data. Configure an OpenAI-compatible API or
a local fine-tuned adapter in Settings before chatting.

Run:
    python code/agent_app.py --open        # http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import agent_core
import agent_pdf
import agent_providers
import agent_settings
import agent_tools

BASE = Path(__file__).resolve().parents[1]
UI_FILE = BASE / "code" / "agent_ui.html"
FEATURES_CSS = BASE / "code" / "agent_ui_features.css"
FEATURES_JS = BASE / "code" / "agent_ui_features.js"
DATA_FILE = BASE / "outputs" / "agent_ui" / "data.json"
INSIGHTS_FILE = BASE / "outputs" / "agent_ui" / "insights.json"
WORKSPACE_FILE = BASE / "outputs" / "agent_ui" / "workspace.json"
LOG_FILE = BASE / "outputs" / "agent_ui" / "agent.log"
SESSION_FILE = BASE / "outputs" / "agent_ui" / "sessions.json"
MAX_SESSIONS = 60
MAX_MESSAGES = 24

LOCK = threading.Lock()
DATA = None


def _load_sessions() -> dict:
    try:
        raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    sessions = {}
    for sid, state in raw.items():
        if not isinstance(state, dict):
            continue
        messages = state.get("messages") or []
        last_answer = None
        for item in reversed(messages):
            if item.get("answer"):
                last_answer = item["answer"]
                break
        sessions[sid] = {
            "turns": int(state.get("turns", 0)),
            "history": state.get("history") or [],
            "messages": messages[-MAX_MESSAGES:],
            "title": state.get("title") or "",
            "updated_at": state.get("updated_at") or "",
            "last_answer": last_answer,
        }
    return sessions


def _save_sessions() -> None:
    try:
        with LOCK:
            items = sorted(SESSIONS.items(), key=lambda kv: kv[1].get("updated_at", ""), reverse=True)[:MAX_SESSIONS]
            payload = {}
            for sid, state in items:
                payload[sid] = {
                    "turns": state.get("turns", 0),
                    "history": (state.get("history") or [])[-MAX_MESSAGES:],
                    "messages": (state.get("messages") or [])[-MAX_MESSAGES:],
                    "title": state.get("title", ""),
                    "updated_at": state.get("updated_at", ""),
                }
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = SESSION_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(SESSION_FILE)
    except OSError:
        pass


SESSIONS: dict[str, dict] = _load_sessions()


def get_data() -> agent_tools.ScenarioData:
    global DATA
    if DATA is None:
        DATA = agent_tools.ScenarioData(agent_tools.load_data())
    return DATA


def _features_module():
    import agent_features
    return agent_features


def _insights_module():
    import agent_insights
    return agent_insights


def _load_workspace() -> list[dict]:
    try:
        raw = json.loads(WORKSPACE_FILE.read_text(encoding="utf-8"))
        items = raw.get("items") if isinstance(raw, dict) else raw
        return [item for item in (items or []) if isinstance(item, dict)]
    except (OSError, ValueError):
        return []


def _save_workspace(items: list[dict]) -> None:
    try:
        WORKSPACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = WORKSPACE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": items[-200:]}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(WORKSPACE_FILE)
    except OSError:
        pass


def _narrate(kind: str, payload: dict, lang: str, role: str):
    try:
        agent_features = _features_module()
        builder = {"wizard": agent_features.build_wizard_messages,
                   "poc": agent_features.build_poc_messages,
                   "portfolio": agent_features.build_portfolio_messages}[kind]
        provider = agent_providers.get_provider(agent_settings.load_settings())
        result = provider.complete(builder(payload, lang, role), max_tokens=700, temperature=0.3)
        return agent_features.parse_narrative(result.get("content", ""))
    except Exception:
        return None


def log_event(**fields) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields}
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _preview(state: dict) -> str:
    for item in reversed(state.get("messages") or []):
        if item.get("role") == "assistant":
            return str(item.get("content") or "")[:80]
    return ""


def _session(sid: str) -> dict:
    sid = sid or "default"
    with LOCK:
        return SESSIONS.setdefault(sid, {"turns": 0, "history": []})


def _merge_for_test(body: dict) -> dict:
    """Apply unsaved form / profile values on top of the active profile (for Test)."""
    settings = agent_settings.load_settings()
    active = dict(agent_settings.active_profile(settings))
    incoming = body.get("profile") if isinstance(body.get("profile"), dict) else body
    profile = {
        "id": active.get("id", "preview"),
        "name": incoming.get("name") or active.get("name"),
        "backend": incoming.get("backend") or active.get("backend", "api"),
        "api": {**active.get("api", {}), **(incoming.get("api") or {})},
        "local": {**active.get("local", {}), **(incoming.get("local") or {})},
    }
    key = body.get("api_key") or (body.get("api") or {}).get("api_key")
    if key:
        profile["api"]["api_key"] = str(key).strip()
    return {"backend": profile["backend"], "api": profile["api"], "local": profile["local"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenECRS/2.0"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------ utilities
    def _send(self, status, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload: dict):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2_000_000:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    # --------------------------------------------------------------- routes
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/data":
            return self._send(200, DATA_FILE.read_bytes(), "application/json; charset=utf-8")
        if path.startswith("/assets/"):
            name = path[len("/assets/"):]
            if not name or "/" in name or ".." in name:
                return self._json(404, {"error": "not found"})
            asset = BASE / "code" / "assets" / name
            if not asset.is_file():
                return self._json(404, {"error": "not found"})
            ctype = {"png": "image/png", "svg": "image/svg+xml", "jpg": "image/jpeg",
                     "webp": "image/webp", "ico": "image/x-icon"}.get(asset.suffix.lstrip(".").lower(), "application/octet-stream")
            return self._send(200, asset.read_bytes(), ctype)
        if path == "/agent_ui_features.css":
            if not FEATURES_CSS.exists():
                return self._json(404, {"error": "features css missing"})
            return self._send(200, FEATURES_CSS.read_bytes(), "text/css; charset=utf-8")
        if path == "/agent_ui_features.js":
            if not FEATURES_JS.exists():
                return self._json(404, {"error": "features js missing"})
            return self._send(200, FEATURES_JS.read_bytes(), "application/javascript; charset=utf-8")
        if path == "/api/evidence":
            return self._evidence(parse_qs(urlparse(self.path).query))
        if path == "/api/scenario_detail":
            query = parse_qs(urlparse(self.path).query)
            return self._scenario_detail((query.get("name") or [""])[0],
                                         (query.get("lang") or ["zh"])[0])
        if path == "/api/videos":
            query = parse_qs(urlparse(self.path).query)
            return self._videos((query.get("scenario") or [""])[0],
                                (query.get("lang") or ["zh"])[0])
        if path == "/api/workspace":
            return self._json(200, {"items": _load_workspace()})
        if path == "/api/sessions":
            query = parse_qs(urlparse(self.path).query)
            needle = (query.get("q") or [""])[0].strip().lower()
            items = sorted(SESSIONS.items(), key=lambda kv: kv[1].get("updated_at", ""), reverse=True)
            if needle:
                items = [(sid, st) for sid, st in items if needle in (st.get("title") or "").lower()
                         or needle in _preview(st).lower()]
            items = items[:40]
            return self._json(200, {"sessions": [
                {"id": sid, "title": state.get("title") or "", "updated_at": state.get("updated_at") or "",
                 "turns": state.get("turns", 0), "preview": _preview(state)}
                for sid, state in items]})
        if path == "/api/session":
            query = parse_qs(urlparse(self.path).query)
            sid = (query.get("sid") or ["default"])[0]
            state = SESSIONS.get(sid) or {"title": "", "updated_at": "", "messages": []}
            return self._json(200, {"id": sid, "title": state.get("title", ""),
                                    "updated_at": state.get("updated_at", ""),
                                    "messages": state.get("messages") or []})
        if path == "/api/health":
            info = agent_providers.provider_info()
            public = agent_settings.public_settings()
            return self._json(200, {"ok": True, "llm_ready": public["ready"],
                                    "backend": info["backend"], "model": info["model"],
                                    "pdf_backend": bool(agent_pdf.find_browser()),
                                    "data_ready": DATA_FILE.exists()})
        if path == "/api/settings":
            return self._json(200, agent_settings.public_settings())
        if path == "/report":
            query = parse_qs(urlparse(self.path).query)
            return self._print_report((query.get("sid") or ["default"])[0])
        return self._json(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path == "/api/session":
            query = parse_qs(urlparse(self.path).query)
            sid = (query.get("sid") or [""])[0]
            if sid and sid in SESSIONS:
                del SESSIONS[sid]
                _save_sessions()
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json()
        try:
            if path == "/api/upload":
                return self._upload(body)
            if path == "/api/chat":
                return self._chat(body)
            if path == "/api/pdf":
                return self._pdf(body)
            if path == "/api/settings":
                return self._save_settings(body)
            if path == "/api/active":
                return self._set_active(body)
            if path == "/api/test":
                return self._test(body)
            if path == "/api/wizard":
                return self._wizard(body)
            if path == "/api/poc":
                return self._poc(body)
            if path == "/api/portfolio":
                return self._portfolio(body)
            if path == "/api/workspace":
                return self._workspace_update(body)
            if path == "/api/session/rename":
                return self._rename_session(body)
        except Exception as exc:  # never kill the connection silently
            log_event(route=path, ok=False, error=f"{type(exc).__name__}: {exc}")
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._json(404, {"error": "not found"})

    # -------------------------------------------------------------- handlers
    def _upload(self, body: dict):
        try:
            import agent_uploads
        except ImportError:
            return self._json(503, {"error": "upload module not available"})
        try:
            record = agent_uploads.save_upload(str(body.get("name") or ""),
                                               str(body.get("data") or ""))
            parsed = agent_uploads.parse_upload(record["id"])
        except agent_uploads.UploadError as exc:
            return self._json(400, {"error": str(exc)})
        log_event(route="/api/upload", ok=True, name=parsed["name"], ext=parsed["ext"],
                  mode=parsed["mode"], tokens=parsed["tokens"], size=parsed["size"])
        return self._json(200, {"id": parsed["id"], "name": parsed["name"], "ext": parsed["ext"],
                                "size": parsed["size"], "chars": parsed["chars"],
                                "tokens": parsed["tokens"], "mode": parsed["mode"],
                                "preview": parsed.get("preview", "")[:600]})

    def _chat(self, body: dict):
        message = str(body.get("message") or "").strip()
        if not message:
            return self._json(400, {"error": "message is required"})
        session_id = str(body.get("session") or "default")
        lang = str(body.get("lang") or "en").lower()
        role = str(body.get("role") or "founder").lower()
        if role not in ("founder", "enterprise", "investor", "pm"):
            role = "founder"
        web_search = bool(body.get("web_search"))
        attachment_id = str(body.get("attachment_id") or "")
        attachment_context = ""
        attachment_info = None
        if attachment_id:
            try:
                import agent_uploads
                attachment_info = agent_uploads.build_context(attachment_id, message)
                attachment_context = attachment_info["context"]
            except Exception as exc:
                return self._json(400, {"error": "Attachment could not be read: " + str(exc)[:160]})
        settings = agent_settings.load_settings()
        public = agent_settings.public_settings()
        if not public["ready"]:
            log_event(route="/api/chat", ok=False, error="llm_required", message=message[:120])
            return self._json(428, {
                "error": "OpenECRS requires an LLM. Configure an API key or a local model first.",
                "code": "llm_required",
                "hint": public["ready_detail"],
            })
        state = _session(session_id)
        try:
            runtime = agent_core.AgentRuntime(settings, get_data())
            answer = runtime.chat(message, state, lang=lang, role=role, web_search=web_search,
                                 extra_context=attachment_context)
        except agent_providers.LLMRequiredError as exc:
            log_event(route="/api/chat", ok=False, error=str(exc), message=message[:120])
            return self._json(428, {"error": str(exc), "hint": exc.hint, "code": "llm_required"})
        except (agent_core.AgentError, agent_providers.ProviderError) as exc:
            log_event(route="/api/chat", ok=False, error=str(exc), message=message[:120])
            return self._json(502, {"error": str(exc), "code": "llm_error"})
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        state.setdefault("messages", [])
        state["messages"].append({"role": "user", "content": message, "at": stamp})
        state["messages"].append({"role": "assistant", "content": answer.get("advice", ""),
                                  "answer": answer, "at": stamp})
        state["messages"] = state["messages"][-MAX_MESSAGES:]
        state["title"] = state.get("title") or message[:60]
        state["updated_at"] = stamp
        _save_sessions()
        log_event(route="/api/chat", ok=True, lang=lang, session=session_id,
                  kind=answer.get("kind"), engine=(answer.get("engine") or {}).get("label"),
                  latency_ms=(answer.get("engine") or {}).get("latency_ms"),
                  web_search=web_search, web_results=len(answer.get("web") or []),
                  message=message[:120])
        if attachment_info:
            answer["attachment"] = {k: attachment_info.get(k) for k in ("id", "name", "mode", "tokens", "chars")}
        return self._json(200, answer)

    def _evidence(self, query: dict):
        try:
            agent_insights = _insights_module()
        except ImportError:
            return self._json(503, {"error": "insights module not available"})
        items = agent_insights.evidence_index(get_data())
        args = {
            "scenario": (query.get("scenario") or [""])[0] or None,
            "source": (query.get("source") or [""])[0] or None,
            "year": (query.get("year") or [""])[0] or None,
            "min_confidence": float((query.get("min_confidence") or ["0"])[0] or 0),
            "kind": (query.get("kind") or ["all"])[0],
            "q": (query.get("q") or [""])[0] or None,
            "limit": max(1, min(200, int((query.get("limit") or ["100"])[0] or 100))),
        }
        try:
            filtered = agent_insights.query(items, **args)
        except TypeError:
            filtered = items
        return self._json(200, {"items": filtered, "total": len(filtered),
                                "facets": agent_insights.facets(items)})

    def _scenario_detail(self, name: str, lang: str):
        try:
            agent_insights = _insights_module()
        except ImportError:
            return self._json(503, {"error": "insights module not available"})
        insights = agent_insights.load_insights(INSIGHTS_FILE)
        detail = agent_insights.scenario_detail(name, get_data(), insights)
        if detail.get("error"):
            return self._json(404, detail)
        return self._json(200, detail)

    def _videos(self, scenario: str, lang: str):
        try:
            agent_insights = _insights_module()
        except ImportError:
            return self._json(503, {"error": "insights module not available"})
        if not scenario:
            return self._json(400, {"error": "scenario is required"})
        return self._json(200, {"items": agent_insights.video_links(scenario, get_data(), lang)})

    def _wizard(self, body: dict):
        try:
            agent_features = _features_module()
        except ImportError:
            return self._json(503, {"error": "features module not available"})
        lang = str(body.get("lang") or "zh")
        role = str(body.get("role") or "founder")
        result = agent_features.wizard(body, get_data())
        result["narrative"] = _narrate("wizard", {**body, "shortlist": result.get("shortlist")}, lang, role)
        return self._json(200, result)

    def _poc(self, body: dict):
        try:
            agent_features = _features_module()
        except ImportError:
            return self._json(503, {"error": "features module not available"})
        lang = str(body.get("lang") or "zh")
        role = str(body.get("role") or "founder")
        result = agent_features.poc_plan(str(body.get("scenario") or ""),
                                         int(body.get("weeks") or 4),
                                         int(body.get("team_size") or 3), get_data(), lang)
        if result.get("error"):
            return self._json(400, result)
        result["narrative"] = _narrate("poc", {**body, "plan": result}, lang, role)
        return self._json(200, result)

    def _portfolio(self, body: dict):
        try:
            agent_features = _features_module()
        except ImportError:
            return self._json(503, {"error": "features module not available"})
        lang = str(body.get("lang") or "zh")
        role = str(body.get("role") or "founder")
        result = agent_features.portfolio(int(body.get("team_size") or 3),
                                          int(body.get("weeks") or 12),
                                          int(body.get("budget_usd") or 0), get_data(), lang)
        result["narrative"] = _narrate("portfolio", {**body, "result": result}, lang, role)
        return self._json(200, result)

    def _workspace_update(self, body: dict):
        items = _load_workspace()
        action = str(body.get("action") or "add")
        scenario = str(body.get("scenario") or "").strip()
        note = str(body.get("note") or "").strip()
        if action in ("add", "note") and scenario:
            existing = next((item for item in items if item.get("scenario") == scenario), None)
            if existing:
                if note:
                    existing["note"] = note
                existing["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            else:
                items.append({"scenario": scenario, "note": note,
                              "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            _save_workspace(items)
        elif action == "remove" and scenario:
            items = [item for item in items if item.get("scenario") != scenario]
            _save_workspace(items)
        return self._json(200, {"items": items})

    def _rename_session(self, body: dict):
        sid = str(body.get("sid") or "")
        title = str(body.get("title") or "").strip()[:120]
        if not sid or sid not in SESSIONS or not title:
            return self._json(400, {"error": "sid and title are required"})
        SESSIONS[sid]["title"] = title
        _save_sessions()
        return self._json(200, {"ok": True, "id": sid, "title": title})

    def _pdf(self, body: dict):
        session_id = str(body.get("session") or "default")
        state = _session(session_id)
        answer = state.get("last_answer")
        if not answer:
            return self._json(400, {"error": "Have a conversation first. / 先进行一次对话，再导出 PDF"})
        lang = str(body.get("lang") or answer.get("lang") or "en")
        context = get_data().report_context(answer, lang)
        context["engine"] = answer.get("engine") or {}
        report_html = agent_pdf.build_report_html(context)
        filename = agent_pdf.safe_filename(context)
        out_path = agent_pdf.PDF_DIR / filename
        payload, error = agent_pdf.render_pdf(report_html, out_path)
        log_event(route="/api/pdf", lang=lang, ok=bool(payload), error=error)
        if payload is None:
            return self._json(200, {"fallback": f"/report?sid={session_id}", "filename": filename, "hint": error})
        return self._send(200, payload, "application/pdf",
                          {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
                           "X-PDF-Path": f"outputs/agent_ui/pdf/{quote(filename)}"})

    def _print_report(self, session_id: str):
        state = _session(session_id)
        answer = state.get("last_answer")
        if not answer:
            hint = "<meta charset='utf-8'>Have a conversation first. / 先进行一次对话。"
            return self._send(200, hint.encode("utf-8"), "text/html; charset=utf-8")
        lang = answer.get("lang") or "en"
        context = get_data().report_context(answer, lang)
        context["engine"] = answer.get("engine") or {}
        page = agent_pdf.build_report_html(context)
        print_js = ("<script>window.addEventListener('load',function(){"
                    "setTimeout(function(){window.print();},350);});</script>")
        return self._send(200, (page.replace("</body>", print_js + "</body>")).encode("utf-8"),
                          "text/html; charset=utf-8")

    def _save_settings(self, body: dict):
        try:
            agent_settings.save_settings(body)
        except agent_settings.SettingsError as exc:
            log_event(route="/api/settings", ok=False, error=str(exc))
            return self._json(400, {"error": str(exc)})
        public = agent_settings.public_settings()
        log_event(route="/api/settings", ok=True, active=public["active"], backend=public["backend"],
                  name=public.get("name"), ready=public["ready"])
        return self._json(200, public)

    def _set_active(self, body: dict):
        profile_id = str(body.get("profile_id") or "")
        try:
            agent_settings.set_active(profile_id)
        except agent_settings.SettingsError as exc:
            return self._json(400, {"error": str(exc)})
        public = agent_settings.public_settings()
        log_event(route="/api/active", ok=True, active=public["active"], name=public.get("name"),
                  ready=public["ready"])
        return self._json(200, public)

    def _test(self, body: dict):
        settings = _merge_for_test(body)
        result = agent_providers.test_provider(settings)
        log_event(route="/api/test", backend=result.get("backend"), ok=result.get("ok"),
                  model=result.get("model"), latency_ms=result.get("latency_ms"),
                  error=result.get("error"))
        return self._json(200, result)


def find_free_port(host: str, port: int) -> int:
    for offset in range(30):
        candidate = port + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit("No free port; use --port to choose another one.")


def main():
    parser = argparse.ArgumentParser(description="OpenECRS · LLM-required bilingual agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--rebuild", action="store_true", help="rebuild outputs/agent_ui/data.json")
    args = parser.parse_args()

    if args.rebuild or not DATA_FILE.exists():
        from agent_ui_data import build
        build()

    port = find_free_port(args.host, args.port)
    url = f"http://{args.host}:{port}"
    server = ThreadingHTTPServer((args.host, port), Handler)
    public = agent_settings.public_settings()
    info = agent_providers.provider_info()
    print("=" * 66)
    print("  OpenECRS · LLM-required evidence agent")
    print(f"  Open: {url}")
    print(f"  Backend: {public['backend']} · {info['label']}")
    if public["ready"]:
        print("  LLM: ready")
    else:
        print(f"  LLM: NOT configured — open Settings first ({public['ready_detail']})")
    print(f"  PDF: {'local browser found' if agent_pdf.find_browser() else 'no browser (print fallback)'}")
    print("  Ctrl+C to stop")
    print("=" * 66)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
