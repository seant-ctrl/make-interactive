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
sse_clients = []
sse_lock = threading.Lock()
queue_lock = threading.Lock()


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
                emit(f"[reload] mtime={mtime}")
                broadcast("reload", str(mtime))
        except FileNotFoundError:
            time.sleep(1)
        except Exception as exc:
            emit(f"[error] watcher: {exc}")
            time.sleep(1)


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
    global HTML_PATH, QUEUE_FILE
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

    if not QUEUE_FILE.exists():
        save_queue({"comments": []})

    threading.Thread(target=file_watcher, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    emit(f"[ready] http://localhost:{args.port}")
    emit(f"[file] {HTML_PATH}")
    emit(f"[queue] {QUEUE_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        emit("[shutdown]")
        server.shutdown()


if __name__ == "__main__":
    main()
