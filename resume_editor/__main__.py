"""CLI entry point: python3 -m resume_editor --dir <resume-folder> [--port N]"""

import argparse
import os
import secrets
import sys
from pathlib import Path

from . import __version__, latex
from .server import App, is_loopback_host, make_server


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="resume-editor",
        description="Local-only side-by-side LaTeX resume editor with an integrated terminal.",
    )
    parser.add_argument("--dir", default=os.getcwd(),
                        help="resume folder (template + source material + job description + agents.md); default: cwd")
    parser.add_argument("--port", type=int, default=8787, help="port on 127.0.0.1 (default 8787)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; only loopback values are accepted (default 127.0.0.1)")
    parser.add_argument("--token", default=None,
                        help="session token (default: read RESUME_EDITOR_TOKEN or generate one)")
    parser.add_argument("--compiler", default=None,
                        help="LaTeX compiler to use (pdflatex/xelatex/lualatex/tectonic or a path); default: auto-detect")
    parser.add_argument("--vibe-cmd", default=os.environ.get("RESUME_EDITOR_VIBE_CMD", "vibe"),
                        help="command the 'run vibe' button starts in the terminal (default: vibe)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument("--version", action="version", version=f"resume-editor {__version__}")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    if not is_loopback_host(args.host):
        print(f"refusing to bind {args.host!r}: only loopback addresses are allowed "
              "(127.0.0.1, localhost, ::1). The resume folder must stay on this machine.",
              file=sys.stderr)
        return 2

    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    token = args.token or os.environ.get("RESUME_EDITOR_TOKEN") or secrets.token_urlsafe(24)
    try:
        compiler = latex.find_compiler(args.compiler)
    except RuntimeError as e:
        print(f"warning: {e}", file=sys.stderr)
        compiler = None
    if compiler is None:
        print("warning: no LaTeX compiler found (looked for pdflatex/xelatex/lualatex/tectonic); "
              "compiles will fail until one is installed or passed via --compiler", file=sys.stderr)

    gui_dir = Path(__file__).resolve().parent.parent / "gui"
    app = App(root, token, compiler, args.vibe_cmd, gui_dir)
    server = make_server(app, args.host, args.port)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"

    print("=" * 72)
    print("resume-editor")
    print(f"  folder   : {root}")
    print(f"  url      : {url}")
    print(f"  token    : {token}")
    print(f"  compiler : {compiler or 'none found'}")
    print("  bound to loopback only; paste the token on the login page.")
    print("=" * 72)

    if not args.no_open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
