"use strict";

const $ = (id) => document.getElementById(id);
const editor = $("editor");
const gutter = $("gutter");

let state = { files: [], main_tex: null, compiler: null, vibe_cmd: "vibe", folder: "" };
let currentFile = null;
let dirty = false;
let saveTimer = null;
let compileTimer = null;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("unauthorized");
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

/* ---------- file tree ---------- */

const collapsedDirs = new Set();

function renderTree() {
  const tree = $("filetree");
  const sidebar = $("sidebar");
  const scrollTop = sidebar.scrollTop;
  tree.innerHTML = "";
  const groups = new Map();
  for (const f of state.files) {
    const parts = f.path.split("/");
    const dir = parts.length > 1 ? parts.slice(0, -1).join("/") : "";
    if (!groups.has(dir)) groups.set(dir, []);
    groups.get(dir).push(f.path);
  }
  const dirs = [...groups.keys()].sort();
  for (const dir of dirs) {
    const isCollapsed = dir !== "" && collapsedDirs.has(dir);
    if (dir) {
      const d = document.createElement("div");
      d.className = "ft-dir" + (isCollapsed ? " collapsed" : "");
      d.textContent = (isCollapsed ? "▸ " : "▾ ") + dir;
      d.onclick = () => {
        if (collapsedDirs.has(dir)) collapsedDirs.delete(dir);
        else collapsedDirs.add(dir);
        renderTree();
      };
      tree.appendChild(d);
    }
    if (isCollapsed) continue;
    for (const p of groups.get(dir)) {
      const el = document.createElement("div");
      el.className = "ft-file" + (p === currentFile ? " current" : "");
      const star = p === state.main_tex ? '<span class="star">★</span> ' : "";
      el.innerHTML = star + escapeHtml(p.split("/").pop());
      el.title = p;
      el.onclick = () => openFile(p);
      tree.appendChild(el);
    }
  }
  sidebar.scrollTop = scrollTop;
  const cur = tree.querySelector(".ft-file.current");
  if (cur) cur.scrollIntoView({ block: "nearest" });
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------- editor ---------- */

function updateGutter() {
  const lines = editor.value.split("\n").length;
  let html = "";
  for (let i = 1; i <= lines; i++) html += i + "<br>";
  gutter.innerHTML = html;
}

async function openFile(path) {
  await saveNow();
  const f = await api("/api/file?path=" + encodeURIComponent(path));
  currentFile = f.path;
  editor.value = f.content;
  dirty = false;
  $("dirty").textContent = "";
  $("saved-at").textContent = "";
  $("cur-file").textContent = f.path;
  updateGutter();
  renderTree();
  if (f.path.endsWith(".tex")) scheduleCompile(400);
}

async function saveNow() {
  if (!currentFile || !dirty) return;
  clearTimeout(saveTimer);
  try {
    await api("/api/file", {
      method: "PUT",
      body: JSON.stringify({ path: currentFile, content: editor.value }),
    });
    dirty = false;
    $("dirty").textContent = "";
    $("saved-at").textContent = "saved " + new Date().toLocaleTimeString();
    scheduleCompile();
  } catch (e) {
    $("saved-at").textContent = "save failed: " + e.message;
  }
}

function scheduleSave() {
  dirty = true;
  $("dirty").textContent = "● unsaved";
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveNow, 700);
}

editor.addEventListener("input", () => {
  updateGutter();
  scheduleSave();
});
editor.addEventListener("scroll", () => {
  gutter.scrollTop = editor.scrollTop;
});
editor.addEventListener("keydown", (e) => {
  if (e.key === "Tab") {
    e.preventDefault();
    const s = editor.selectionStart;
    editor.setRangeText("  ", s, editor.selectionEnd, "end");
    updateGutter();
    scheduleSave();
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    saveNow();
  }
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "b") {
    e.preventDefault();
    compileNow();
  }
});

/* ---------- compile + preview ---------- */

function scheduleCompile(ms = 1200) {
  clearTimeout(compileTimer);
  compileTimer = setTimeout(compileNow, ms);
}

async function compileNow() {
  const file = currentFile && currentFile.endsWith(".tex") ? currentFile : state.main_tex;
  const status = $("compile-status");
  status.className = "";
  status.textContent = "compiling…";
  try {
    const r = await api("/api/compile", {
      method: "POST",
      body: JSON.stringify({ file: file || undefined }),
    });
    showLog(r.log || "");
    if (r.ok) {
      status.textContent = `✓ ${r.stem}.pdf in ${r.duration_ms} ms`;
      status.className = "ok";
      $("pdf").src = "/api/pdf?t=" + Date.now();
      $("preview-empty").classList.add("hidden");
    } else {
      status.textContent = "✗ compile failed — see log";
      status.className = "bad";
    }
  } catch (e) {
    status.textContent = "✗ " + e.message;
    status.className = "bad";
    showLog(String(e.message || e));
  }
}

function showLog(log) {
  const el = $("log");
  el.innerHTML = (log || "(empty log)")
    .split("\n")
    .map((l) => /^(!|.*Error|.*error:|Emergency stop)/.test(l) ? `<span class="err-line">${escapeHtml(l)}</span>` : escapeHtml(l))
    .join("\n");
  el.scrollTop = el.scrollHeight;
}

/* ---------- top bar buttons ---------- */

$("btn-save").onclick = saveNow;
$("btn-compile").onclick = compileNow;
$("btn-new").onclick = async () => {
  const name = prompt("new file path (relative to the resume folder), e.g. source/notes.md");
  if (!name) return;
  await api("/api/file", { method: "PUT", body: JSON.stringify({ path: name, content: "" }) });
  await refreshState();
  openFile(name);
};
$("btn-log-toggle").onclick = () => {
  const w = $("log-wrap");
  w.classList.toggle("collapsed");
  $("btn-log-toggle").textContent = w.classList.contains("collapsed") ? "show" : "hide";
};

/* ---------- terminal ---------- */

const term = new Terminal({
  cursorBlink: true,
  fontSize: 13,
  fontFamily: "Consolas, 'Liberation Mono', Menlo, monospace",
  theme: { background: "#101014", foreground: "#e6e6e6" },
});
const fitAddon = new FitAddon.FitAddon();
term.loadAddon(fitAddon);
term.open($("term"));
fitAddon.fit();

let ws = null;
let wsCmd = null;
let connSeq = 0;
let reconnectTimer = null;

function termConnect(cmd) {
  connSeq++;
  const mySeq = connSeq;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (ws) { try { ws.close(); } catch (_) {} }
  wsCmd = cmd || null;
  let attempts = 0;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/term?cols=${term.cols}&rows=${term.rows}` +
    (cmd ? `&cmd=${encodeURIComponent(cmd)}` : "");
  ws = new WebSocket(url);
  ws.onopen = () => {
    attempts = 0;
    ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "output") {
      const bin = atob(msg.data);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      term.write(new TextDecoder("utf-8").decode(bytes));
    } else if (msg.type === "exit") {
      term.write("\r\n\x1b[90m[process exited — press the shell/vibe button to start a new one]\x1b[0m\r\n");
    }
  };
  ws.onclose = () => {
    if (mySeq !== connSeq) return;
    attempts++;
    if (attempts === 1) term.write("\r\n\x1b[90m[disconnected — reconnecting…]\x1b[0m\r\n");
    if (attempts > 20) {
      term.write("\r\n\x1b[90m[still disconnected — click shell / Run vibe to retry]\x1b[0m\r\n");
      return;
    }
    reconnectTimer = setTimeout(() => termConnect(wsCmd), Math.min(1000 * 2 ** (attempts - 1), 15000));
  };
  ws.onerror = () => {};
}

term.onData((d) => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "input", data: d }));
});
term.onResize(({ cols, rows }) => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "resize", cols, rows }));
  fitAddon.fit();
});

$("btn-shell").onclick = () => { term.reset(); termConnect(null); };
$("btn-vibe").onclick = () => { term.reset(); termConnect(state.vibe_cmd || "vibe"); };
$("btn-vibe-top").onclick = () => {
  if (document.body.classList.contains("term-hidden")) {
    document.body.classList.remove("term-hidden");
    $("btn-term").classList.add("active");
    setTimeout(() => fitAddon.fit(), 50);
  }
  term.reset();
  termConnect(state.vibe_cmd || "vibe");
};
$("btn-clear").onclick = () => term.clear();
$("btn-term").onclick = () => {
  document.body.classList.toggle("term-hidden");
  $("btn-term").classList.toggle("active");
  setTimeout(() => fitAddon.fit(), 50);
};
$("btn-term-close").onclick = () => {
  document.body.classList.add("term-hidden");
  $("btn-term").classList.remove("active");
};

window.addEventListener("resize", () => fitAddon.fit());

/* ---------- init ---------- */

async function refreshState() {
  state = await api("/api/state");
  $("folder").textContent = state.folder + "/";
  $("folder").title = "served locally from this folder only";
  $("btn-vibe").textContent = "run " + (state.vibe_cmd || "vibe");
  $("btn-vibe-top").textContent = "Run " + (state.vibe_cmd || "vibe");
  renderTree();
}

(async function init() {
  try {
    await refreshState();
    if (state.main_tex) {
      await openFile(state.main_tex);
    } else if (state.files.length) {
      await openFile(state.files[0].path);
    }
    if (state.has_pdf) {
      $("pdf").src = "/api/pdf?t=" + Date.now();
      $("preview-empty").classList.add("hidden");
    }
    termConnect(null);
    $("btn-term").classList.add("active");
  } catch (e) {
    document.body.innerHTML = `<pre style="padding:2rem">failed to load: ${escapeHtml(String(e))}</pre>`;
  }
})();
