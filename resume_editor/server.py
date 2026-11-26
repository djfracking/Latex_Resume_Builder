"""Loopback-only HTTP + WebSocket server for the resume editor.

Security model:
  - binds to 127.0.0.1 (or localhost/::1) only; refuses anything else
  - every request must carry the session token (cookie, Bearer, or ?token=)
  - Host header must be a loopback name (blocks DNS-rebinding from browsers)
  - file API is confined to the resume folder (realpath-checked, no symlink escape)
"""

import base64
import hmac
import json
import mimetypes
import os
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import latex, wsproto
from .terminal import PtySession

LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}
EXCLUDED_DIRS = {".git", "build", "__pycache__", "node_modules", ".DS_Store"}
MAX_BODY = 8 * 1024 * 1024
MAX_READ = 2 * 1024 * 1024
COOKIE = "re_token"

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>resume-editor login</title>
<style>body{font-family:system-ui,sans-serif;background:#1e1f24;color:#e6e6e6;display:flex;height:100vh;margin:0;align-items:center;justify-content:center}
form{background:#2a2b33;padding:2rem;border-radius:10px;display:flex;flex-direction:column;gap:.8rem;width:320px}
input,button{padding:.6rem;border-radius:6px;border:1px solid #444;background:#1e1f24;color:#e6e6e6;font-size:1rem}
button{background:#4f7cff;border:none;cursor:pointer}button:hover{background:#3f6af0}h1{font-size:1.1rem;margin:0}
.err{color:#ff7b72;font-size:.85rem}</style></head><body>
<form method="post" action="/login"><h1>resume-editor</h1>
<input type="password" name="token" placeholder="session token (printed by the server)" autofocus>
<button type="submit">Sign in</button><!--ERR--></form></body></html>"""


def resolve_under(root: Path, rel: str) -> Path:
    if not rel or "\x00" in rel:
        raise ValueError("invalid path")
    pure = urllib.parse.unquote(rel)
    if pure.startswith("/") or pure.startswith("\\"):
        raise ValueError("absolute paths are not allowed")
    parts = Path(pure).parts
    if any(p == ".." for p in parts):
        raise ValueError("path traversal is not allowed")
    candidate = (root / pure).resolve()
    if os.path.commonpath([str(root), str(candidate)]) != str(root):
        raise ValueError("path escapes the resume folder")
    return candidate


def list_files(root: Path):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            if name in EXCLUDED_DIRS:
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            try:
                st = full.stat()
            except OSError:
                continue
            out.append({"path": rel, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda f: f["path"])
    return out


def find_main_tex(root: Path, files):
    paths = [f["path"] for f in files]
    if "template.tex" in paths:
        return "template.tex"
    tex = sorted(p for p in paths if p.endswith(".tex"))
    return tex[0] if tex else None


class App:
    def __init__(self, root: Path, token: str, compiler, vibe_cmd: str, gui_dir: Path):
        self.root = root.resolve()
        self.token = token
        self.compiler = compiler
        self.vibe_cmd = vibe_cmd
        self.gui_dir = gui_dir.resolve()
        self.compile_lock = threading.Lock()
        self.last_log = ""
        self.last_pdf = None
        self.last_compile = {"ok": None}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "resume-editor/0.1"
    app: App = None

    def log_message(self, fmt, *args):
        pass

    # ---------- helpers ----------

    def _host_ok(self):
        host = self.headers.get("Host", "")
        if host.startswith("["):
            name = host[1:host.index("]")] if "]" in host else ""
        else:
            name = host.split(":")[0]
        return name.lower() in LOOPBACK_NAMES

    def _cookie_token(self):
        header = self.headers.get("Cookie", "")
        for part in header.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == COOKIE:
                    return v.strip()
        return ""

    def _authed(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        candidates = [self._cookie_token()]
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            candidates.append(auth[7:].strip())
        candidates.extend(query.get("token", []))
        return any(c and hmac.compare_digest(c, self.app.token) for c in candidates)

    def _send(self, status, body=b"", content_type="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

    def _error(self, status, msg):
        self._json({"error": msg}, status)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(length) if length else b""

    def _guard(self):
        """Common preflight: loopback Host header + auth. Returns True if OK."""
        if not self._host_ok():
            self._error(403, "non-loopback Host header rejected")
            return False
        return True

    def _require_auth(self):
        if not self._authed():
            self._error(401, "unauthorized")
            return False
        return True

    # ---------- HTTP ----------

    def do_GET(self):
        if not self._guard():
            return
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        query = urllib.parse.parse_qs(url.query)

        if path == "/login":
            self._send(200, LOGIN_PAGE.replace("<!--ERR-->", ""))
            return
        if path == "/ws/term":
            if not self._require_auth():
                return
            self._handle_ws(query)
            return
        if not self._authed():
            if path == "/":
                self._send(303, b"", headers={"Location": "/login"})
            else:
                self._error(401, "unauthorized")
            return
        if path == "/":
            self._serve_static("index.html")
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        elif path == "/api/state":
            files = list_files(self.app.root)
            self._json({
                "folder": self.app.root.name,
                "files": files,
                "main_tex": find_main_tex(self.app.root, files),
                "compiler": self.app.compiler,
                "vibe_cmd": self.app.vibe_cmd,
                "has_pdf": bool(self.app.last_pdf and self.app.last_pdf.exists()),
            })
        elif path == "/api/files":
            self._json({"files": list_files(self.app.root)})
        elif path == "/api/file":
            self._get_file(query)
        elif path == "/api/compile":
            self._error(405, "use POST")
        elif path == "/api/pdf":
            self._get_pdf()
        elif path == "/api/log":
            self._json({"log": self.app.last_log})
        else:
            self._error(404, "not found")

    def do_POST(self):
        if not self._guard():
            return
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/login":
            body = urllib.parse.parse_qs(self._body().decode("utf-8", "replace"))
            token = body.get("token", [""])[0]
            if token and hmac.compare_digest(token, self.app.token):
                self._send(
                    303,
                    b"",
                    headers={
                        "Location": "/",
                        "Set-Cookie": f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict",
                    },
                )
            else:
                self._send(200, LOGIN_PAGE.replace("<!--ERR-->", '<p class="err">wrong token</p>'))
            return
        if not self._require_auth():
            return
        if url.path == "/api/compile":
            self._compile()
        else:
            self._error(404, "not found")

    def do_PUT(self):
        if not self._guard():
            return
        if not self._require_auth():
            return
        if urllib.parse.urlsplit(self.path).path == "/api/file":
            self._put_file()
        else:
            self._error(404, "not found")

    # ---------- route bodies ----------

    def _serve_static(self, rel):
        try:
            full = resolve_under(self.app.gui_dir, rel)
        except ValueError as e:
            self._error(400, str(e))
            return
        if not full.is_file():
            self._error(404, "not found")
            return
        ctype = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        self._send(200, full.read_bytes(), ctype)

    def _get_file(self, query):
        rel = query.get("path", [""])[0]
        try:
            full = resolve_under(self.app.root, rel)
        except ValueError as e:
            self._error(400, str(e))
            return
        if not full.is_file():
            self._error(404, "not a file")
            return
        if full.stat().st_size > MAX_READ:
            self._error(413, "file too large to edit")
            return
        self._json({
            "path": rel,
            "content": full.read_text(encoding="utf-8", errors="replace"),
            "mtime": full.stat().st_mtime,
        })

    def _put_file(self):
        try:
            data = json.loads(self._body().decode("utf-8"))
            rel = data["path"]
            content = data["content"]
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            self._error(400, f"bad request: {e}")
            return
        try:
            full = resolve_under(self.app.root, rel)
        except ValueError as e:
            self._error(400, str(e))
            return
        if full.is_dir():
            self._error(400, "path is a directory")
            return
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.with_name(full.name + ".tmp-re")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, full)
        self._json({"ok": True, "path": rel, "mtime": full.stat().st_mtime})

    def _compile(self):
        try:
            data = json.loads(self._body().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            data = {}
        files = list_files(self.app.root)
        tex = data.get("file") or find_main_tex(self.app.root, files)
        if not tex:
            self._error(400, "no .tex file in folder")
            return
        if not self.app.compiler:
            self._json({
                "ok": False,
                "error": "no LaTeX compiler found (install texlive or tectonic, or pass --compiler)",
                "log": "",
            })
            return
        with self.app.compile_lock:
            result = latex.compile_tex(self.app.root, tex, self.app.compiler)
        self.app.last_log = result.get("log", "")
        self.app.last_pdf = (
            self.app.root / "build" / (result.get("stem", "template") + ".pdf")
            if result.get("pdf") else None
        )
        self.app.last_compile = result
        self._json(result)

    def _get_pdf(self):
        pdf = self.app.last_pdf
        if not pdf or not pdf.is_file():
            build = self.app.root / "build"
            if build.is_dir():
                candidates = sorted(build.glob("*.pdf"))
                pdf = candidates[-1] if candidates else None
        if not pdf or not pdf.is_file():
            self._error(404, "no compiled PDF yet")
            return
        self._send(200, pdf.read_bytes(), "application/pdf")

    # ---------- WebSocket terminal ----------

    def _handle_ws(self, query):
        if not self._require_auth():
            return
        upgrade = self.headers.get("Upgrade", "").lower()
        if "websocket" not in upgrade:
            self._error(400, "expected a WebSocket upgrade")
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._error(400, "missing Sec-WebSocket-Key")
            return
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", wsproto.accept_key(key))
        self.end_headers()
        self.close_connection = True

        cols = int(query.get("cols", ["80"])[0])
        rows = int(query.get("rows", ["24"])[0])
        cmd = query.get("cmd", [None])[0]
        sess = PtySession(self.app.root, cmd=cmd, cols=cols, rows=rows)
        reader = wsproto.FrameReader(self.rfile.read1)
        self.connection.settimeout(0.05)
        sent_exit = False
        try:
            while True:
                try:
                    fin, opcode, payload = reader.read_frame()
                except TimeoutError:
                    _, sent_exit = self._flush_pty(sess, sent_exit)
                    continue
                if opcode == wsproto.OP_CLOSE:
                    break
                if opcode == wsproto.OP_PING:
                    self.connection.sendall(wsproto.encode_frame(wsproto.OP_PONG, payload))
                    continue
                if opcode in (wsproto.OP_TEXT, wsproto.OP_BINARY, wsproto.OP_CONT):
                    try:
                        msg = json.loads(payload.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    mtype = msg.get("type")
                    if mtype == "input":
                        sess.write(msg.get("data", ""))
                    elif mtype == "resize":
                        try:
                            sess.resize(int(msg.get("cols", 80)), int(msg.get("rows", 24)))
                        except (TypeError, ValueError):
                            pass
                    elif mtype == "spawn":
                        sess.close()
                        sess = PtySession(self.app.root, cmd=msg.get("cmd") or None,
                                          cols=cols, rows=rows)
                _, sent_exit = self._flush_pty(sess, sent_exit)
        except (ConnectionError, ValueError, OSError):
            pass
        finally:
            sess.close()

    def _flush_pty(self, sess, sent_exit):
        for chunk in sess.drain():
            if chunk is None:
                if not sent_exit:
                    self._ws_send({"type": "exit"})
                    sent_exit = True
            else:
                self._ws_send({"type": "output", "data": base64.b64encode(chunk).decode("ascii")})
        return True, sent_exit

    def _ws_send(self, msg):
        self.connection.sendall(
            wsproto.encode_frame(wsproto.OP_TEXT, json.dumps(msg).encode("utf-8"))
        )


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    Handler.app = app
    return QuietThreadingHTTPServer((host, port), Handler)


def is_loopback_host(host: str) -> bool:
    return host in LOOPBACK_NAMES
