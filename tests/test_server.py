"""End-to-end tests for resume-editor: auth, confinement, file API, compile, WS terminal."""

import base64
import http.client
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resume_editor import __main__ as cli
from resume_editor.server import App, make_server

TOKEN = "test-token-abc123"

FAKE_COMPILER = r'''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

def build_pdf(text):
    stream = b"BT /F1 18 Tf 72 720 Td (" + text + b") Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)

args = sys.argv[1:]
if "-output-directory" in args:
    outdir = Path(args[args.index("-output-directory") + 1])
    tex = Path(args[-1])
else:
    outdir = Path(args[args.index("--outdir") + 1])
    tex = Path(args[-1])
outdir.mkdir(parents=True, exist_ok=True)
stem = tex.stem
(outdir / (stem + ".pdf")).write_bytes(build_pdf(b"stub resume pdf"))
(outdir / (stem + ".log")).write_text("cwd=%s\nThis is the fake log.\nOutput written on %s.pdf (1 page).\n" % (os.getcwd(), stem))
print("fakepdflatex: compiled", tex.name)
'''


class WSClient:
    def __init__(self, port, path, token=TOKEN):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Cookie: re_token={token}\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            resp += chunk
        head, _, self.buf = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n")[0]
        if b"101" not in status:
            raise AssertionError("handshake failed: " + repr(head))

    def send_text(self, obj):
        payload = json.dumps(obj).encode()
        assert len(payload) < 126
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes([0x81, 0x80 | len(payload)]) + mask + masked)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_msg(self, timeout=15):
        self.sock.settimeout(timeout)
        b1, b2 = self._read(2)
        opcode = b1 & 0x0F
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        payload = self._read(ln) if ln else b""
        if opcode == 8:
            return {"type": "close"}
        return json.loads(payload.decode())

    def collect_output(self, needle: bytes, timeout=20):
        data = b""
        deadline = time.time() + timeout
        while time.time() < deadline and needle not in data:
            try:
                msg = self.read_msg(timeout=max(0.2, deadline - time.time()))
            except (socket.timeout, TimeoutError):
                break
            if msg.get("type") == "output":
                data += base64.b64decode(msg["data"])
            elif msg.get("type") == "exit":
                break
        return data

    def close(self):
        try:
            self.send_text({"type": "noop"})
        except OSError:
            pass
        self.sock.close()


class Fixture:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.root = tmp / "resume"
        (self.root / "source").mkdir(parents=True)
        (self.root / "template.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n"
        )
        (self.root / "agents.md").write_text("# workflow\n")
        (self.root / "job-description.txt").write_text("job\n")
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        compiler = bin_dir / "fakepdflatex"
        compiler.write_text(FAKE_COMPILER)
        compiler.chmod(0o755)
        gui_dir = Path(__file__).resolve().parent.parent / "gui"
        self.app = App(self.root, TOKEN, str(compiler), "vibe", gui_dir)
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def req(self, method, path, body=None, cookie=True, host=None, raw_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {}
        if cookie:
            headers["Cookie"] = f"re_token={TOKEN}"
        if host:
            headers["Host"] = host
        if raw_headers:
            headers.update(raw_headers)
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, dict(res.getheaders()), data


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.fx = Fixture(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.fx.stop()
        cls._tmp.cleanup()

    def test_requires_auth(self):
        status, _, _ = self.fx.req("GET", "/api/state", cookie=False)
        self.assertEqual(status, 401)
        status, headers, _ = self.fx.req("GET", "/", cookie=False)
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")
        status, headers, body = self.fx.req("GET", "/login", cookie=False)
        self.assertEqual(status, 200)
        self.assertIn(b"token", body)
        self.assertIn("text/html", headers.get("Content-Type", ""))

    def test_login_flow(self):
        status, _, body = self.fx.req(
            "POST", "/login", body="token=wrong", cookie=False,
            raw_headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 200)
        self.assertIn(b"wrong token", body)
        status, headers, _ = self.fx.req(
            "POST", "/login", body=f"token={TOKEN}", cookie=False,
            raw_headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertIn(f"re_token={TOKEN}", headers.get("Set-Cookie", ""))

    def test_bearer_token(self):
        status, _, _ = self.fx.req(
            "GET", "/api/state", cookie=False,
            raw_headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.assertEqual(status, 200)

    def test_host_header_rejected(self):
        status, _, _ = self.fx.req("GET", "/api/state", host="evil.example.com")
        self.assertEqual(status, 403)

    def test_path_traversal_blocked(self):
        for bad in ["../secret.txt", "/etc/passwd", "..%2F..%2Fetc%2Fpasswd", "....//etc/passwd"]:
            status, _, body = self.fx.req("GET", "/api/file?path=" + bad)
            self.assertIn(status, (400, 404), bad)
            self.assertNotIn(b"root:", body)
        link = self.fx.root / "link"
        link.symlink_to("/etc")
        status, _, _ = self.fx.req("GET", "/api/file?path=link/passwd")
        self.assertEqual(status, 400)

    def test_static_traversal_blocked(self):
        status, _, _ = self.fx.req("GET", "/static/..%2Fresume_editor%2Fserver.py")
        self.assertIn(status, (400, 404))

    def test_index_and_static(self):
        status, _, body = self.fx.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"resume", body)
        status, headers, body = self.fx.req("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertIn(b"WebSocket", body)

    def test_state_and_files(self):
        status, _, body = self.fx.req("GET", "/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state["main_tex"], "template.tex")
        self.assertEqual(state["vibe_cmd"], "vibe")
        paths = [f["path"] for f in state["files"]]
        self.assertIn("template.tex", paths)
        self.assertIn("agents.md", paths)
        self.assertIn("job-description.txt", paths)
        self.assertTrue(state["compiler"].endswith("fakepdflatex"))

    def test_file_crud(self):
        status, _, body = self.fx.req(
            "PUT", "/api/file", body={"path": "source/notes.md", "content": "# hi\n"}
        )
        self.assertEqual(status, 200)
        status, _, body = self.fx.req("GET", "/api/file?path=source/notes.md")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["content"], "# hi\n")
        status, _, body = self.fx.req("GET", "/api/files")
        self.assertIn("source/notes.md", [f["path"] for f in json.loads(body)["files"]])
        status, _, _ = self.fx.req("PUT", "/api/file", body={"path": "../evil.txt", "content": "x"})
        self.assertEqual(status, 400)
        self.assertFalse((self.fx.tmp / "evil.txt").exists())

    def test_compile_and_pdf(self):
        status, _, body = self.fx.req("POST", "/api/compile", body={})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stem"], "template")
        self.assertIn("fake log", result["log"])
        status, headers, body = self.fx.req("GET", "/api/pdf")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/pdf")
        self.assertTrue(body.startswith(b"%PDF"))
        status, _, body = self.fx.req("GET", "/api/log")
        self.assertIn("fake log", json.loads(body)["log"])

    def test_compile_subdir_uses_tex_dir_as_cwd(self):
        status, _, _ = self.fx.req(
            "PUT", "/api/file",
            body={"path": "versions/whoop.tex", "content": "\\documentclass{article}\\begin{document}x\\end{document}\n"},
        )
        self.assertEqual(status, 200)
        status, _, body = self.fx.req("POST", "/api/compile", body={"file": "versions/whoop.tex"})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertTrue(result["ok"], result)
        expected_cwd = str(Path(self.fx.root, "versions").resolve())
        self.assertIn("cwd=" + expected_cwd, result["log"])
        status, _, body = self.fx.req("GET", "/api/pdf")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"%PDF"))

    def test_compile_path_traversal_blocked(self):
        status, _, body = self.fx.req("POST", "/api/compile", body={"file": "../evil.tex"})
        self.assertEqual(status, 400)
        self.assertIn(b"error", body)

    def test_ws_terminal_shell(self):
        ws = WSClient(self.fx.port, "/ws/term?cols=100&rows=30")
        try:
            ws.send_text({"type": "input", "data": "echo WS_OK_7f3a; pwd\n"})
            out = ws.collect_output(b"WS_OK_7f3a")
            self.assertIn(b"WS_OK_7f3a", out)
            ws.send_text({"type": "input", "data": "pwd\n"})
            out = ws.collect_output(self.fx.root.name.encode(), timeout=10)
            self.assertIn(self.fx.root.name.encode(), out)
            ws.send_text({"type": "resize", "cols": 120, "rows": 40})
        finally:
            ws.close()

    def test_ws_terminal_cmd(self):
        ws = WSClient(self.fx.port, "/ws/term?cmd=" + "echo%20hello-cmd-99")
        try:
            out = ws.collect_output(b"hello-cmd-99")
            self.assertIn(b"hello-cmd-99", out)
        finally:
            ws.close()

    def test_ws_bad_cmd_is_fatal_not_crash(self):
        ws = WSClient(self.fx.port, "/ws/term?cmd=/nonexistent-cmd-xyz-123")
        fatal = None
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    msg = ws.read_msg(timeout=2)
                except (socket.timeout, TimeoutError, ConnectionError):
                    break
                if msg.get("type") == "fatal":
                    fatal = msg
                    break
                if msg.get("type") == "close":
                    break
        finally:
            ws.close()
        self.assertIsNotNone(fatal, "expected a fatal message for a missing command")
        self.assertIn("nonexistent-cmd-xyz-123", fatal.get("error", ""))

    def test_ws_requires_auth(self):
        with self.assertRaises(AssertionError):
            WSClient(self.fx.port, "/ws/term", token="wrong")


class TimeoutTests(unittest.TestCase):
    def test_run_with_timeout(self):
        from resume_editor.server import run_with_timeout
        ok, err = run_with_timeout(lambda: 42, 1.0, "t/o")
        self.assertEqual(ok, 42)
        self.assertIsNone(err)
        val, err = run_with_timeout(lambda: time.sleep(3), 0.2, "t/o")
        self.assertIsNone(val)
        self.assertEqual(err, "t/o")


class CompilerTests(unittest.TestCase):
    def test_resolve_compiler_falls_back_on_missing_explicit(self):
        from resume_editor import latex
        result = latex.resolve_compiler("/nonexistent-compiler-xyz")
        self.assertEqual(result, latex.find_compiler(None))


class CliTests(unittest.TestCase):
    def test_refuses_non_loopback_bind(self):
        self.assertEqual(cli.main(["--host", "0.0.0.0", "--no-open"]), 2)
        self.assertEqual(cli.main(["--host", "192.168.1.10", "--no-open"]), 2)

    def test_refuses_missing_dir(self):
        self.assertEqual(cli.main(["--dir", "/nonexistent-xyz-123", "--no-open"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
