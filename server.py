#!/usr/bin/env python3
"""make-interactive — Designer comment canvas server.

Serves an HTML file with an injected overlay, accepts pin/selection comments
from the browser, broadcasts hot-reload events via SSE, and emits structured
stdout lines that Claude watches with the Monitor tool.

Stdout protocol (one event per line):
    [ready] http://localhost:<port>
    [file] <absolute path>
    [queue] <absolute path to queue file>
    [comment] {json}         # single comment payload
    [batch] count=<n> ids=<id,id,id>
    [reload] mtime=<ts>
    [error] <msg>
    [shutdown]
"""
import argparse
import json
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

SKILL_DIR = Path(__file__).parent

# Set after arg parse — anchors to HTML file's parent dir so the queue file
# lives next to the HTML being iterated on, not wherever the launcher happened
# to be. Avoids Path.cwd() permission errors under sandboxed launchers.
HTML_PATH: Path = None
QUEUE_FILE: Path = None
HISTORY_DIR: Path = None
HISTORY_FILE: Path = None
sse_clients = []
sse_lock = threading.Lock()
queue_lock = threading.Lock()
history_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(line: str) -> None:
    print(line, flush=True)


def load_queue() -> dict:
    if not QUEUE_FILE.exists():
        return {"comments": []}
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"comments": []}


def save_queue(data: dict) -> None:
    QUEUE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {"versions": []}
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"versions": []}


def save_history(data: dict) -> None:
    HISTORY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def snapshot_now(label: str = None) -> int:
    """Snapshot the current HTML file to the history dir. Returns the new version number.

    Versions are numbered v0001, v0002, ... and stored as raw HTML alongside a
    history.json index. The first snapshot taken at server boot is auto-labelled
    "Original" so the user can revert back to the pristine state.
    """
    with history_lock:
        data = load_history()
        versions = data.get("versions", [])
        next_n = (versions[-1]["version"] + 1) if versions else 1
        snap_path = HISTORY_DIR / f"v{next_n:04d}.html"
        try:
            content = HTML_PATH.read_text(encoding="utf-8")
        except Exception as exc:
            emit(f"[error] snapshot read: {exc}")
            return -1
        snap_path.write_text(content, encoding="utf-8")
        entry = {
            "version": next_n,
            "timestamp": now_iso(),
            "label": label or ("Original" if next_n == 1 else None),
            "appliedComments": [],
            "fileSize": len(content),
        }
        versions.append(entry)
        data["versions"] = versions
        save_history(data)
        emit(f"[snapshot] v{next_n:04d}")
        return next_n


def attribute_resolved_to_latest_version(resolved_ids: list) -> None:
    """Tag newly-resolved comment IDs onto the most recent version entry.

    Idempotent — a comment that is already attributed to ANY version (not just
    the latest) is skipped. This protects against double-counting after a
    server restart, where the queue_watcher would otherwise re-attribute every
    already-resolved comment to the newest version on its first pass.
    """
    if not resolved_ids:
        return
    with history_lock:
        data = load_history()
        versions = data.get("versions", [])
        if not versions:
            return
        already_anywhere = set()
        for v in versions:
            already_anywhere.update(v.get("appliedComments", []))
        latest = versions[-1]
        existing = set(latest.get("appliedComments", []))
        changed = False
        for cid in resolved_ids:
            if cid in already_anywhere:
                continue
            if cid not in existing:
                latest.setdefault("appliedComments", []).append(cid)
                existing.add(cid)
                already_anywhere.add(cid)
                changed = True
        if changed:
            save_history(data)


def queue_watcher() -> None:
    """Watch the queue file for newly-resolved comments. Attribute them to the
    most recent snapshot so the History panel can show what changed at each step.
    """
    prev_applied = {}
    while True:
        try:
            time.sleep(0.5)
            data = load_queue()
            current_applied = {
                c["id"]: c.get("appliedNote")
                for c in data.get("comments", [])
                if c.get("appliedNote")
            }
            newly = [cid for cid, note in current_applied.items() if prev_applied.get(cid) != note]
            if newly:
                attribute_resolved_to_latest_version(newly)
            prev_applied = current_applied
        except FileNotFoundError:
            time.sleep(1)
        except Exception as exc:
            emit(f"[error] queue_watcher: {exc}")
            time.sleep(1)


def append_comments(new_comments: list) -> list:
    with queue_lock:
        data = load_queue()
        max_n = 0
        for c in data["comments"]:
            try:
                n = int(str(c.get("id", "c0")).lstrip("c"))
                max_n = max(max_n, n)
            except ValueError:
                continue
        added = []
        for raw in new_comments:
            max_n += 1
            entry = {
                "id": f"c{max_n}",
                "createdAt": now_iso(),
                "mode": raw.get("mode", "pin"),
                "selector": raw.get("selector"),
                "xpath": raw.get("xpath"),
                "previewHTML": raw.get("previewHTML"),
                "selectionText": raw.get("selectionText"),
                "comment": raw.get("comment", ""),
                "quickAction": raw.get("quickAction"),
                "viewport": raw.get("viewport"),
                "anchor": raw.get("anchor"),
                "status": "pending",
                "appliedNote": None,
            }
            data["comments"].append(entry)
            added.append(entry)
        save_queue(data)
        return added


def broadcast(event_type: str, data: str) -> None:
    payload = (event_type, data)
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            if q in sse_clients:
                sse_clients.remove(q)


def inject_overlay(html: str) -> str:
    inject = (
        '<link rel="stylesheet" href="/__overlay.css">\n'
        '<script src="/__overlay.js" defer></script>\n'
    )
    lower = html.lower()
    idx = lower.find("</head>")
    if idx != -1:
        return html[:idx] + inject + html[idx:]
    idx = lower.find("<body")
    if idx != -1:
        return html[:idx] + inject + html[idx:]
    return inject + html


def file_watcher() -> None:
    try:
        last_mtime = HTML_PATH.stat().st_mtime
    except FileNotFoundError:
        last_mtime = 0
    while True:
        try:
            time.sleep(0.4)
            mtime = HTML_PATH.stat().st_mtime
            if mtime != last_mtime:
                last_mtime = mtime
                # Snapshot BEFORE broadcasting reload so the History panel
                # always reflects the current state when the browser re-fetches.
                snapshot_now()
                emit(f"[reload] mtime={mtime}")
                broadcast("reload", str(mtime))
        except FileNotFoundError:
            time.sleep(1)
        except Exception as exc:
            emit(f"[error] watcher: {exc}")
            time.sleep(1)


class _QuietThreadingServer(ThreadingHTTPServer):
    """Silence the noisy traceback on benign client disconnects (SSE clients
    closing on page reload, browser nav, etc.). Real server errors still raise."""
    def handle_error(self, request, client_address):
        import sys, traceback
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        traceback.print_exc()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        return  # silence default logging

    def _send(self, status, body, content_type="text/plain; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        body_bytes = body.encode("utf-8") if isinstance(body, str) else body
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        try:
            self.wfile.write(body_bytes)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                html = HTML_PATH.read_text(encoding="utf-8")
            except Exception as exc:
                self._send(500, f"Failed to read HTML: {exc}")
                return
            self._send(200, inject_overlay(html), "text/html; charset=utf-8")
            return
        if path == "/__overlay.js":
            js = (SKILL_DIR / "overlay.js").read_text(encoding="utf-8")
            self._send(200, js, "application/javascript; charset=utf-8")
            return
        if path == "/__overlay.css":
            css = (SKILL_DIR / "overlay.css").read_text(encoding="utf-8")
            self._send(200, css, "text/css; charset=utf-8")
            return
        if path == "/api/queue":
            self._send(200, json.dumps(load_queue()), "application/json")
            return
        if path == "/api/history":
            # Enrich each version with the resolved comment objects (so the
            # client can render the applied note without a second roundtrip).
            history = load_history()
            q_data = load_queue()
            by_id = {c["id"]: c for c in q_data.get("comments", [])}
            for v in history.get("versions", []):
                v["comments"] = [by_id[cid] for cid in v.get("appliedComments", []) if cid in by_id]
            self._send(200, json.dumps(history), "application/json")
            return
        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            client_q = queue.Queue()
            with sse_lock:
                sse_clients.append(client_q)
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event_type, data = client_q.get(timeout=15)
                        msg = f"event: {event_type}\ndata: {data}\n\n"
                        self.wfile.write(msg.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with sse_lock:
                    if client_q in sse_clients:
                        sse_clients.remove(client_q)
            return
        self._send(404, "Not Found")

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""

        if path == "/api/comments":
            try:
                payload = json.loads(raw)
            except Exception as exc:
                self._send(400, f"Bad JSON: {exc}")
                return
            incoming = payload if isinstance(payload, list) else [payload]
            added = append_comments(incoming)
            if len(added) > 1:
                ids = ",".join(c["id"] for c in added)
                emit(f"[batch] count={len(added)} ids={ids}")
            for c in added:
                emit(f"[comment] {json.dumps(c, ensure_ascii=False)}")
            self._send(
                200,
                json.dumps({"ok": True, "ids": [c["id"] for c in added]}),
                "application/json",
            )
            return

        if path == "/api/revert":
            try:
                payload = json.loads(raw)
            except Exception as exc:
                self._send(400, f"Bad JSON: {exc}")
                return
            version = payload.get("version")
            if not isinstance(version, int):
                self._send(400, "version must be an integer")
                return
            snap_path = HISTORY_DIR / f"v{version:04d}.html"
            if not snap_path.exists():
                self._send(404, f"version v{version:04d} not found")
                return
            try:
                content = snap_path.read_text(encoding="utf-8")
                HTML_PATH.write_text(content, encoding="utf-8")
            except Exception as exc:
                self._send(500, f"revert failed: {exc}")
                return
            emit(f"[revert] version={version}")
            # The file write will trigger the watcher which will snapshot the
            # new state (= a snapshot of the old version, intentionally — we
            # want reverts in history too so you can undo a revert).
            self._send(200, json.dumps({"ok": True, "version": version}), "application/json")
            return

        if path == "/api/dismiss":
            try:
                payload = json.loads(raw)
            except Exception as exc:
                self._send(400, f"Bad JSON: {exc}")
                return
            cid = payload.get("id")
            with queue_lock:
                data = load_queue()
                for c in data["comments"]:
                    if c["id"] == cid:
                        c["status"] = "dismissed"
                        break
                save_queue(data)
            emit(f"[dismiss] id={cid}")
            self._send(200, json.dumps({"ok": True}), "application/json")
            return

        self._send(404, "Not Found")


def main():
    global HTML_PATH, QUEUE_FILE, HISTORY_DIR, HISTORY_FILE
    parser = argparse.ArgumentParser()
    parser.add_argument("html", help="Path to HTML file to serve")
    parser.add_argument("--port", type=int, default=7321)
    parser.add_argument(
        "--queue",
        default=None,
        help="Override queue file path (default: <html-dir>/.make-interactive-queue.json)",
    )
    args = parser.parse_args()

    HTML_PATH = Path(args.html).resolve()
    if not HTML_PATH.exists():
        emit(f"[error] file not found: {HTML_PATH}")
        sys.exit(1)

    QUEUE_FILE = Path(args.queue).resolve() if args.queue else HTML_PATH.parent / ".make-interactive-queue.json"
    HISTORY_DIR = HTML_PATH.parent / ".make-interactive-history"
    HISTORY_FILE = HISTORY_DIR / "history.json"

    HISTORY_DIR.mkdir(exist_ok=True)

    if not QUEUE_FILE.exists():
        save_queue({"comments": []})

    # Initial snapshot — represents the pristine state, used by "Revert to Original".
    if not HISTORY_FILE.exists() or not load_history().get("versions"):
        snapshot_now(label="Original")

    threading.Thread(target=file_watcher, daemon=True).start()
    threading.Thread(target=queue_watcher, daemon=True).start()

    server = _QuietThreadingServer(("127.0.0.1", args.port), Handler)
    emit(f"[ready] http://localhost:{args.port}")
    emit(f"[file] {HTML_PATH}")
    emit(f"[queue] {QUEUE_FILE}")
    emit(f"[history] {HISTORY_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        emit("[shutdown]")
        server.shutdown()


if __name__ == "__main__":
    main()
