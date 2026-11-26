# resume-editor

A Linux desktop-style GUI for your LaTeX resume workflow: **template + source
material + job description + agents.md in one folder**, edited side by side with
the rendered PDF, with an integrated terminal to run your agent CLI (e.g. `vibe`)
right inside the resume folder.

Everything runs **locally on your machine**. The resume folder never leaves the
server: the GUI is a thin client that talks to a loopback-only HTTP/WebSocket
server.

## Run it

```bash
bin/resume-editor --dir /path/to/your/resume-folder
# or
python3 -m resume_editor --dir /path/to/your/resume-folder --port 8787
```

The server prints a URL and a one-time session token. Open
`http://127.0.0.1:8787/`, paste the token once, and you are in.

Options:

| flag | default | meaning |
|---|---|---|
| `--dir` | cwd | resume folder (template + sources + job description + agents.md) |
| `--port` | `8787` | port on loopback |
| `--host` | `127.0.0.1` | bind address — **non-loopback values are refused** |
| `--token` | random / `$RESUME_EDITOR_TOKEN` | session token for the login page |
| `--compiler` | auto-detect | `pdflatex` / `xelatex` / `lualatex` / `tectonic` or a path |
| `--vibe-cmd` | `vibe` / `$RESUME_EDITOR_VIBE_CMD` | command behind the "run vibe" button |
| `--no-open` | off | don't auto-open the browser |

Example with a fixed token:

```bash
RESUME_EDITOR_TOKEN=dev bin/resume-editor --dir ~/resumes/acme --vibe-cmd vibe
```

## Layout

```
┌──────────────────────────────────────────────────────────────┐
│ folder · compile status · Compile Save New file · Terminal   │
├──────────┬──────────────────────────────┬────────────────────┤
│ file     │  editor (auto-save,           │  rendered PDF      │
│ tree     │  auto-compile on save)        │  preview           │
│          │                              ├────────────────────┤
│          │                              │  compile log       │
├──────────┴──────────────────────────────┴────────────────────┤
│ integrated CLI (xterm.js over WebSocket → PTY in the folder) │
│ [shell] [run vibe] [clear]                                   │
└──────────────────────────────────────────────────────────────┘
```

- Click any file in the tree to edit it (`.tex`, `agents.md`, job description, …).
- Saving recompiles automatically (debounced); `Ctrl+S` saves, `Ctrl+Shift+B` compiles.
- The right pane shows the freshly compiled PDF plus the LaTeX log (errors highlighted).
- The bottom terminal is a real PTY running in the resume folder. **run vibe**
  spawns your agent CLI (`--vibe-cmd`) there, so the agent edits the same files
  the GUI shows — save in either place and the preview updates.

## Security model

- Server binds to `127.0.0.1` only; passing `--host 0.0.0.0` (or anything non-loopback) exits with an error.
- Every route (including the WebSocket) requires the session token: `HttpOnly` cookie,
  `Authorization: Bearer`, or `?token=`. Login sets the cookie.
- `Host` header must be a loopback name (blocks DNS-rebinding from a browser).
- The file API is confined to the resume folder: relative paths only, `..` rejected,
  symlinks resolved and re-checked against the folder root.
- Compiled artifacts go to `<folder>/build/` (excluded from the file tree).

## API (all loopback + token)

| route | description |
|---|---|
| `GET /api/state` | folder name, file list, main tex, compiler, vibe cmd |
| `GET /api/file?path=…` | read one file |
| `PUT /api/file` `{path, content}` | write one file (atomic) |
| `POST /api/compile` `{file?}` | run the LaTeX compiler (twice), returns log |
| `GET /api/pdf` | latest compiled PDF |
| `GET /api/log` | last compile log |
| `GET /ws/term?cols=&rows=&cmd=` | WebSocket → PTY (JSON frames: `input`/`resize` in, base64 `output`/`exit` out) |

## Desktop launcher (optional)

```ini
# ~/.local/share/applications/resume-editor.desktop
[Desktop Entry]
Name=Resume Editor
Exec=/path/to/resume-editor/bin/resume-editor --dir %f --no-open
Type=Application
Terminal=true
```
(`Terminal=true` keeps the token visible in the console; open the printed URL
in your browser.)

## Development

Zero-dependency Python 3.10+ server (stdlib only); the frontend vendors
xterm.js locally (no CDN). Tests:

```bash
python3 -m unittest discover -s tests -v
```

They cover auth, host-header rejection, path traversal/symlink escape, file
CRUD, compilation (via a stub compiler), PDF serving, and the WebSocket
terminal.

## Layout of this repo

```
resume_editor/    Python package (server, websocket framing, PTY, latex)
gui/              static frontend (index.html, app.js, style.css, vendored xterm.js)
bin/resume-editor launcher script
sample-resume/    demo folder: template.tex + source/ + job-description.txt + agents.md
tests/            end-to-end tests
```
