const state = { filename: "", fileId: null, issues: [], selectedIssueId: null, diff: "", busy: false };
const fileInput = document.querySelector("#file-input");
const uploadButton = document.querySelector("#upload-file-button");
const analyzeButton = document.querySelector("#analyze-file-button");
const fixButton = document.querySelector("#fix-file-button");
const editor = document.querySelector("#source-editor");
const syntaxLayer = document.querySelector("#syntax-layer");
const syntaxCode = syntaxLayer.querySelector("code");
const gutter = document.querySelector("#editor-gutter");
const marker = document.querySelector("#error-line-marker");
const issuesList = document.querySelector("#file-issues-list");
const emptyState = document.querySelector("#file-empty-state");
const issueCount = document.querySelector("#file-issue-count");
const chatForm = document.querySelector("#file-chat-form");
const chatInput = document.querySelector("#chat-input");
const chatSend = document.querySelector("#chat-send");
const chatMessages = document.querySelector("#chat-messages");
const toast = document.querySelector("#toast");

function notify(message, kind = "") {
  toast.textContent = message;
  toast.className = `toast show ${kind}`.trim();
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { toast.className = "toast"; }, 3200);
}

function errorMessage(payload, response) {
  if (payload?.reason) return payload.reason;
  if (payload?.message) return payload.message;
  if (typeof payload?.detail === "string") return payload.detail;
  if (Array.isArray(payload?.detail)) return payload.detail.map((item) => item.msg).join(", ");
  return `Request failed with HTTP ${response.status}.`;
}

function setStatus(label, copy, kind = "") {
  const badge = document.querySelector("#file-state-badge");
  badge.textContent = label;
  badge.className = `status-badge ${kind}`.trim();
  document.querySelector("#file-state-copy").textContent = copy;
}

function escapeHtml(value) {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

function highlightLine(line) {
  const escaped = escapeHtml(line);
  return escaped
    .replace(/(#.*)$/g, '<span class="syntax-comment">$1</span>')
    .replace(/\b(False|None|True|and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield)\b/g, '<span class="syntax-keyword">$1</span>')
    .replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="syntax-number">$1</span>') || " ";
}

function syncScroll() {
  syntaxLayer.scrollTop = editor.scrollTop;
  syntaxLayer.scrollLeft = editor.scrollLeft;
  gutter.scrollTop = editor.scrollTop;
  if (!marker.hidden) marker.style.transform = `translateY(${15 - editor.scrollTop}px)`;
}

function updateCursor() {
  const before = editor.value.slice(0, editor.selectionStart).split("\n");
  document.querySelector("#cursor-position").textContent = `Ln ${before.length}, Col ${before.at(-1).length + 1}`;
}

function renderEditor() {
  const lines = editor.value.split("\n");
  syntaxCode.innerHTML = lines.map(highlightLine).join("\n") + (editor.value.endsWith("\n") ? "\n " : "");
  const issueLines = new Set(state.issues.map((issue) => issue.line));
  gutter.replaceChildren(...lines.map((_, index) => {
    const line = document.createElement("span");
    line.textContent = String(index + 1);
    if (issueLines.has(index + 1)) line.classList.add("issue-line");
    return line;
  }));
  syncScroll();
  updateCursor();
}

function resetAnalysis() {
  state.fileId = null;
  state.issues = [];
  state.selectedIssueId = null;
  state.diff = "";
  issuesList.replaceChildren();
  issueCount.textContent = "Not analyzed";
  fixButton.disabled = true;
  chatInput.disabled = true;
  chatSend.disabled = true;
  document.querySelector("#repair-summary").hidden = true;
  document.querySelector("#view-file-diff").hidden = true;
  document.querySelector("#download-fixed-file").hidden = true;
  emptyState.hidden = false;
  emptyState.className = "empty-state";
  emptyState.querySelector("h3").textContent = "Ready to analyze";
  emptyState.querySelector("p").textContent = "The file is loaded. Run the fast static analysis when ready.";
  marker.hidden = true;
}

async function loadFile(file) {
  if (!file.name.toLowerCase().endsWith(".py")) return notify("Only Python (.py) files are supported", "error");
  if (file.size > 1_000_000) return notify("The file exceeds the 1 MB limit", "error");
  let source;
  try { source = await file.text(); } catch { return notify("The file could not be read", "error"); }
  if (source.includes("\0")) return notify("Binary files are not supported", "error");
  state.filename = file.name;
  editor.value = source;
  editor.disabled = false;
  document.querySelector("#file-name").textContent = file.name;
  document.querySelector("#editor-filename").textContent = file.name;
  analyzeButton.disabled = false;
  resetAnalysis();
  renderEditor();
  setStatus("FILE LOADED", "Ready to analyze", "running");
}

function focusIssue(issue, card) {
  state.selectedIssueId = issue.issue_id;
  document.querySelectorAll(".file-issue-card.selected").forEach((item) => item.classList.remove("selected"));
  card.classList.add("selected");
  const lines = editor.value.split("\n");
  const start = lines.slice(0, issue.line - 1).reduce((total, line) => total + line.length + 1, 0);
  editor.focus();
  editor.setSelectionRange(start, start + (lines[issue.line - 1]?.length || 0));
  editor.scrollTop = Math.max(0, (issue.line - 4) * 20);
  marker.hidden = false;
  marker.style.top = `${(issue.line - 1) * 20}px`;
  syncScroll();
}

function renderIssues(issues) {
  state.issues = issues || [];
  state.selectedIssueId = state.issues[0]?.issue_id || null;
  issuesList.replaceChildren();
  issueCount.textContent = `${state.issues.length} ${state.issues.length === 1 ? "issue" : "issues"}`;
  emptyState.hidden = state.issues.length > 0;
  if (!state.issues.length) {
    emptyState.className = "empty-state success";
    emptyState.querySelector("h3").textContent = "No issues detected";
    emptyState.querySelector("p").textContent = "The source passed all available analysis signals.";
  }
  state.issues.forEach((issue, index) => {
    const card = document.createElement("article");
    card.className = "file-issue-card";
    card.dataset.issueId = issue.issue_id;
    if (index === 0) card.classList.add("selected");
    card.dataset.severity = issue.severity;
    card.style.animationDelay = `${Math.min(index * 30, 180)}ms`;
    const body = document.createElement("div");
    const meta = document.createElement("div");
    meta.className = "file-issue-meta";
    for (const value of [issue.severity, issue.type.replaceAll("_", " "), issue.source]) {
      const tag = document.createElement("span"); tag.textContent = value; meta.append(tag);
    }
    const title = document.createElement("h3"); title.textContent = issue.title;
    body.append(meta, title);
    const location = document.createElement("small");
    location.textContent = issue.end_line > issue.line ? `Lines ${issue.line}-${issue.end_line}` : `Line ${issue.line}`;
    const description = document.createElement("p"); description.textContent = `${issue.description} ${issue.suggestion}`;
    card.append(body, location, description);
    card.addEventListener("click", () => focusIssue(issue, card));
    issuesList.append(card);
  });
  renderEditor();
}

async function analyzeFile() {
  if (state.busy || !state.filename) return;
  state.busy = true;
  analyzeButton.disabled = true;
  analyzeButton.classList.add("loading");
  analyzeButton.querySelector("span").textContent = "Analyzing...";
  emptyState.hidden = false;
  emptyState.className = "empty-state";
  emptyState.querySelector("h3").textContent = "Scanning file...";
  emptyState.querySelector("p").textContent = "Running AST, Ruff, security, and complexity checks concurrently.";
  issuesList.replaceChildren();
  issueCount.textContent = "Analyzing...";
  setStatus("ANALYZING", "Static fast path is running", "running");
  const form = new FormData();
  form.append("file", new Blob([editor.value], { type: "text/x-python" }), state.filename);
  try {
    const response = await fetch("/api/files/analyze", { method: "POST", body: form });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorMessage(payload, response));
    state.fileId = payload.file_id;
    state.filename = payload.filename;
    editor.value = payload.source;
    document.querySelector("#file-name").textContent = payload.filename;
    document.querySelector("#editor-filename").textContent = payload.filename;
    document.querySelector("#analysis-origin").textContent = payload.cache_hit ? "SHA-256 cache hit" : `AI review: ${payload.gemini_status}`;
    renderIssues(payload.issues);
    fixButton.disabled = payload.issues.length === 0;
    chatInput.disabled = false;
    chatSend.disabled = false;
    setStatus(payload.issues.length ? "ISSUES FOUND" : "CLEAN", payload.issues.length ? `${payload.issues.length} ranked issues` : "0 issues detected", payload.issues.length ? "error" : "success");
    if (["unavailable", "timed_out"].includes(payload.gemini_status)) notify(`Local analysis complete; AI review ${payload.gemini_status.replace("_", " ")}`, "info");
    else notify(payload.cache_hit ? "Cached analysis returned" : "File analysis complete", "success");
  } catch (error) {
    emptyState.hidden = false;
    emptyState.className = "empty-state error";
    emptyState.querySelector("h3").textContent = "Analysis could not be completed";
    emptyState.querySelector("p").textContent = error.message;
    setStatus("ERROR", "Analysis could not be completed", "error");
    notify(error.message, "error");
  } finally {
    state.busy = false;
    analyzeButton.disabled = false;
    analyzeButton.classList.remove("loading");
    analyzeButton.querySelector("span").textContent = "Analyze file";
  }
}

async function fixFile() {
  if (state.busy || !state.fileId || !state.selectedIssueId) return;
  const requestedIssueId = state.selectedIssueId;
  state.busy = true;
  fixButton.disabled = true;
  fixButton.classList.add("loading");
  fixButton.querySelector("span").textContent = "Repairing...";
  setStatus("FIXING", "Applying and validating repair", "running");
  try {
    const response = await fetch(`/api/files/${state.fileId}/fix`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ issue_id: requestedIssueId }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorMessage(payload, response));
    if (payload.success === false) throw new Error(payload.reason || "The selected issue was not repaired.");
    editor.value = payload.fixed_code || payload.source;
    state.diff = payload.diff;
    renderIssues(payload.analysis?.issues || payload.issues);
    document.querySelector("#repair-summary").hidden = false;
    document.querySelector("#before-issues").textContent = payload.before_count;
    document.querySelector("#after-issues").textContent = payload.after_count;
    document.querySelector("#fixed-issues").textContent = payload.issues_fixed;
    document.querySelector("#changed-lines").textContent = payload.lines_changed;
    document.querySelector("#file-diff").textContent = payload.diff || "No source lines changed.";
    document.querySelector("#view-file-diff").hidden = false;
    const download = document.querySelector("#download-fixed-file");
    download.href = `/api/files/${state.fileId}/download`;
    download.hidden = false;
    fixButton.disabled = payload.issues.length === 0;
    setStatus(payload.after_count === 0 ? "FIXED" : "PARTIALLY FIXED", `${payload.after_count} issues remaining`, payload.after_count === 0 ? "success" : "error");
    notify("Repair validated and diff generated", "success");
  } catch (error) {
    console.error("FixFlow file repair failed", { fileId: state.fileId, issueId: requestedIssueId, error });
    setStatus("ERROR", "Repair could not be completed", "error");
    notify(error.message, "error");
    fixButton.disabled = false;
  } finally {
    state.busy = false;
    fixButton.classList.remove("loading");
    fixButton.querySelector("span").textContent = "Fix Now";
    fixButton.disabled = state.issues.length === 0 || !state.selectedIssueId;
  }
}

function appendChat(role, message) {
  chatMessages.querySelector(".chat-welcome")?.remove();
  const bubble = document.createElement("div");
  bubble.className = `chat-message ${role}`;
  const label = document.createElement("b"); label.textContent = role === "user" ? "You" : "FixFlow";
  bubble.append(label, document.createTextNode(message));
  chatMessages.append(bubble);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return bubble;
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = chatInput.value.trim();
  if (!message || !state.fileId || state.busy) return;
  appendChat("user", message);
  chatInput.value = "";
  chatInput.disabled = true;
  chatSend.disabled = true;
  const pending = appendChat("assistant", "Thinking...");
  try {
    const response = await fetch(`/api/files/${state.fileId}/chat`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message }) });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorMessage(payload, response));
    pending.lastChild.textContent = payload.answer;
  } catch (error) { pending.lastChild.textContent = `I couldn't answer that: ${error.message}`; }
  finally { chatInput.disabled = false; chatSend.disabled = false; chatInput.focus(); }
});

uploadButton.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => { if (fileInput.files[0]) loadFile(fileInput.files[0]); fileInput.value = ""; });
analyzeButton.addEventListener("click", analyzeFile);
fixButton.addEventListener("click", fixFile);
editor.addEventListener("input", () => { if (state.fileId) { resetAnalysis(); setStatus("FILE LOADED", "Code changed - analyze again", "running"); } renderEditor(); });
editor.addEventListener("scroll", syncScroll);
editor.addEventListener("click", updateCursor);
editor.addEventListener("keyup", updateCursor);
editor.addEventListener("keydown", (event) => { if (event.key === "Tab") { event.preventDefault(); editor.setRangeText("    ", editor.selectionStart, editor.selectionEnd, "end"); editor.dispatchEvent(new Event("input")); } });
document.querySelector("#view-file-diff").addEventListener("click", () => { const panel = document.querySelector("#file-diff-panel"); panel.hidden = !panel.hidden; });
document.querySelector("#copy-file-diff").addEventListener("click", async () => { try { await navigator.clipboard.writeText(state.diff); notify("Diff copied", "success"); } catch { notify("Clipboard access was unavailable", "error"); } });
document.querySelector("#collapse-sidebar").addEventListener("click", () => { if (window.innerWidth <= 720) document.body.classList.remove("mobile-nav-open"); else document.body.classList.toggle("sidebar-collapsed"); });
document.querySelector("#mobile-menu").addEventListener("click", () => document.body.classList.add("mobile-nav-open"));
document.querySelector("#sidebar-scrim").addEventListener("click", () => document.body.classList.remove("mobile-nav-open"));
renderEditor();
