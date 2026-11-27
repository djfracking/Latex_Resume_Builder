"""LaTeX compilation for the resume folder."""

import os
import shutil
import subprocess
import time
from pathlib import Path

CANDIDATES = ["pdflatex", "xelatex", "lualatex", "tectonic"]


def find_compiler(explicit=None):
    if explicit:
        found = shutil.which(explicit)
        if not found and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            found = explicit
        if not found:
            raise RuntimeError(f"LaTeX compiler not found: {explicit}")
        return found
    for name in CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def compile_tex(root: Path, tex_rel: str, compiler: str, timeout: int = 90):
    tex_abs = (root / tex_rel).resolve()
    if os.path.commonpath([str(root), str(tex_abs)]) != str(root):
        return {"ok": False, "error": "tex file escapes the resume folder", "log": ""}
    if not tex_abs.is_file():
        return {"ok": False, "error": f"not a file: {tex_rel}", "log": ""}
    workdir = tex_abs.parent
    build = workdir / "build"
    build.mkdir(exist_ok=True)
    stem = tex_abs.stem
    base = os.path.basename(compiler)
    if base.startswith("tectonic"):
        cmd = [compiler, "--outdir", str(build), tex_abs.name]
    else:
        cmd = [
            compiler,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-output-directory",
            str(build),
            tex_abs.name,
        ]
    output = []
    started = time.time()
    for _ in range(2):
        try:
            proc = subprocess.run(
                cmd, cwd=str(workdir), capture_output=True, text=True, timeout=timeout
            )
            output.append(proc.stdout + proc.stderr)
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"compiler timed out after {timeout}s",
                "log": "".join(output)[-20000:],
            }
    log_file = build / (stem + ".log")
    if log_file.exists():
        log = log_file.read_text(errors="replace")
    else:
        log = "\n".join(output)
    pdf = build / (stem + ".pdf")
    return {
        "ok": pdf.exists(),
        "log": log[-20000:],
        "pdf": pdf.exists(),
        "pdf_path": str(pdf) if pdf.exists() else None,
        "stem": stem,
        "compiler": base,
        "duration_ms": int((time.time() - started) * 1000),
    }
