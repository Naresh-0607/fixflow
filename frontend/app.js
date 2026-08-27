const state = {
  running: false,
  result: null,
  selectedIssue: null,
  repoId: null,
  eventSource: null,
};

const form = document.querySelector("#analyze-form");
const repositoryInput = document.querySelector("#repository-url");
const analyzeButton = document.querySelector("#analyze-button");
const progressTitle = document.querySelector("#progress-title");
const progressBadge = document.querySelector("#progress-badge");
const progressTrack = document.querySelector("#progress-track");
const pipelineSteps = [...document.querySelectorAll("#pipeline-steps li")];
const activityLog = document.querySelector("#activity-log");
const runtimeStatus = document.querySelector("#runtime-status");
const resultMessage = document.querySelector("#result-message");
const issuesList = document.querySelector("#issues-list");
const issueTemplate = document.querySelector("#issue-template");
const inspectorContent = document.querySelector("#inspector-content");
const outputPanel = document.querySelector("#pytest-output-panel");
const outputElement = document.querySelector("#pytest-output");
const outputButton = document.querySelector("#toggle-output");
const fixRepositoryButton = document.querySelector("#fix-repository");
const repositoryRepairPanel = document.querySelector("#repository-repair-progress");
const repairProgressTitle = document.querySelector("#repair-progress-title");
const repairProgressCount = document.querySelector("#repair-progress-count");
const repairProgressTrack = document.querySelector(".repair-progress-track span");
const repairProgressSummary = document.querySelector("#repair-progress-summary");
const repairActivityFeed = document.querySelector("#repair-activity-feed");
const toast = document.querySelector("#toast");

let toastTimer;

function notify(message, type = "info") {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = `toast ${type} show`;
  toastTimer = setTimeout(() => toast.classList.remove("show"), 3400);
}

function log(message, type = "info") {
  const line = document.createElement("p");
  const label = document.createElement("span");
  const labels = {
    info: "READY",
    proc: "PROC",
    ai: "AI",
    warn: "WARN",
    error: "ERROR",
    note: "NOTE",
  };
  label.className = `log-${type}`;
  label.textContent = `[${labels[type] || "INFO"}]`;
  line.append(label, ` ${message}`);
  activityLog.append(line);
  activityLog.scrollTop = activityLog.scrollHeight;
}

function setRuntime(kind, title, detail) {
  runtimeStatus.className = `runtime-status ${kind || ""}`.trim();
  runtimeStatus.querySelector("b").textContent = title;
  runtimeStatus.querySelector("small").textContent = detail;
}

function resetPipeline() {
  pipelineSteps.forEach((step) => step.className = "");
  progressTrack.className = "progress-track";
  progressBadge.className = "status-badge idle";
  progressBadge.textContent = "IDLE";
  progressTitle.textContent = "Ready to analyze";
  progressTrack.querySelector("span").style.width = "0%";
}

function beginPipeline(repositoryUrl) {
  resetPipeline();
  pipelineSteps[0].classList.add("running");
  progressTrack.classList.add("running", "determinate");
  progressBadge.className = "status-badge running";
  progressBadge.textContent = "RUNNING";
  progressTitle.textContent = "Analysis in progress";
  setRuntime("busy", "Analysis running", "Waiting for backend workflow");
  document.querySelector("#terminal-title").textContent = "fixflow / current-job.log";
  log(`Preparing managed analysis for ${repositoryUrl}`, "proc");
}

const progressStepByStage = {
  repo_validated: 0,
  clone_started: 0,
  clone_completed: 1,
  workspace_created: 1,
  project_detected: 1,
  container_started: 2,
  container_ready: 3,
  scan_started: 3,
  scan_completed: 4,
  ai_analysis_started: 4,
  ai_analysis_completed: 4,
  static_analysis_started: 4,
  file_scanned: 4,
};

function applyProgressEvent(event) {
  if (
    event.stage.startsWith("repair_")
    || event.stage.startsWith("issue_")
    || event.stage.startsWith("validation_")
    || event.stage.startsWith("zip_")
  ) {
    applyRepairProgressEvent(event);
    return;
  }
  const active = pipelineSteps.findIndex((step) => step.classList.contains("running"));
  const index = progressStepByStage[event.stage] ?? Math.max(0, active);
  pipelineSteps.forEach((step, stepIndex) => {
    step.className = stepIndex < index ? "complete" : stepIndex === index ? "running" : "";
  });
  progressTrack.className = "progress-track running determinate";
  progressTrack.querySelector("span").style.width = `${event.progress}%`;
  progressTitle.textContent = event.message;
  progressBadge.className = "status-badge running";
  progressBadge.textContent = `${event.progress}%`;
  log(event.message, event.stage.includes("ai_") ? "ai" : "proc");
  if (event.stage === "completed") {
    pipelineSteps.forEach((step) => step.className = "complete");
    state.eventSource?.close();
  } else if (event.stage === "failed") {
    pipelineSteps[index].className = "error";
    progressTrack.className = "progress-track error";
    state.eventSource?.close();
  }
}

function connectProgress(repoId, phase = "analysis") {
  state.eventSource?.close();
  state.eventSource = new EventSource(`/api/repositories/${repoId}/events?phase=${phase}`);
  state.eventSource.addEventListener("progress", (message) => {
    try { applyProgressEvent(JSON.parse(message.data)); }
    catch { log("A progress event could not be read.", "warn"); }
  });
}

function completePipeline(result) {
  state.eventSource?.close();
  pipelineSteps.forEach((step) => step.className = "complete");
  progressTrack.className = "progress-track complete";
  progressBadge.className = "status-badge success";

  if (result.status === "tests_passed") {
    progressBadge.textContent = "PASSED";
    progressTitle.textContent = "All tests passed";
    log(`pytest completed: ${result.tests.passed} passed.`, "info");
  } else if (result.status === "no_tests") {
    pipelineSteps[4].className = "";
    progressBadge.textContent = "NO TESTS";
    progressTitle.textContent = "No tests were collected";
    log("pytest completed without collecting tests.", "warn");
  } else if (result.status === "analysis_failed") {
    pipelineSteps[4].className = "error";
    progressTrack.className = "progress-track error";
    progressBadge.className = "status-badge error";
    progressBadge.textContent = "PARTIAL";
    progressTitle.textContent = "Tests complete · analysis unavailable";
    log(result.analysis_error || "Gemini analysis was unavailable.", "warn");
  } else {
    progressBadge.textContent = "COMPLETE";
    progressTitle.textContent = "Issues analyzed";
    log(`Gemini returned ${result.analysis.length} structured findings.`, "ai");
  }

  setRuntime("", "Analysis complete", result.job_id);
}

function failPipeline(payload) {
  state.eventSource?.close();
  const code = payload?.error || "request_failed";
  const stepByError = {
    invalid_repository_url: 0,
    clone_failed: 0,
    unsupported_project: 1,
    docker_unavailable: 2,
    docker_build_failed: 2,
    test_execution_failed: 3,
    execution_timeout: 3,
    gemini_analysis_failed: 4,
  };
  const failedIndex = stepByError[code] ?? 0;
  pipelineSteps.forEach((step, index) => {
    step.className = index < failedIndex ? "complete" : "";
  });
  pipelineSteps[failedIndex].className = "error";
  progressTrack.className = "progress-track error";
  progressBadge.className = "status-badge error";
  progressBadge.textContent = "STOPPED";
  progressTitle.textContent = "Analysis could not complete";
  setRuntime("error", "Action required", code.replaceAll("_", " "));
}

function setMetrics(tests, repositoryIssueCount = null) {
  document.querySelector("#metric-total").textContent = tests?.total ?? "—";
  document.querySelector("#metric-passed").textContent = tests?.passed ?? "—";
  document.querySelector("#metric-failed").textContent = repositoryIssueCount ?? tests?.failed ?? "—";
  document.querySelector("#metric-skipped").textContent = tests?.skipped ?? "—";
}

function showResultMessage(title, message, type = "") {
  resultMessage.hidden = false;
  resultMessage.className = `empty-state ${type}`.trim();
  resultMessage.querySelector("h3").textContent = title;
  resultMessage.querySelector("p").textContent = message;
}

function normalizedIssues(result) {
  if (result.analysis?.length) {
    return result.analysis.map((item) => ({
      ...item,
      fallback: false,
    }));
  }
  return (result.tests?.failures || []).map((failure) => ({
    test: failure.test,
    what_failed: failure.error || "pytest reported a failure.",
    why: failure.trace || "See the complete pytest output for the failure trace.",
    root_cause: "Gemini root-cause analysis was not available for this run.",
    file: failure.file || "Unknown file",
    symbol: failure.test?.split("::").at(-1) || "Unknown symbol",
    suggested_fix: "Review the captured trace and relevant source before making a change.",
    confidence: "low",
    fallback: true,
  }));
}

function issueTitle(issue) {
  const testName = issue.test?.split("::").at(-1);
  return testName || issue.what_failed || "Test failure";
}

function renderIssues(result) {
  issuesList.replaceChildren();
  state.selectedIssue = null;
  const issues = normalizedIssues(result);

  if (result.status === "tests_passed") {
    showResultMessage(
      "Repository tests passed",
      result.message || "No test failures were detected.",
      "success",
    );
    renderInspectorWelcome("No failures to inspect", "Every collected test passed in the isolated container.");
    return;
  }

  if (result.status === "no_tests") {
    showResultMessage("No tests collected", result.message || "pytest found no tests.", "error");
    return;
  }

  if (!issues.length) {
    showResultMessage("No structured failures", result.message || "Review the pytest output for details.", "error");
    return;
  }

  resultMessage.hidden = true;
  issues.forEach((issue, index) => {
    const card = issueTemplate.content.firstElementChild.cloneNode(true);
    const confidence = ["high", "medium", "low"].includes(issue.confidence)
      ? issue.confidence
      : "low";
    card.classList.add(confidence);
    card.dataset.issueId = issue.issue_id || "";
    card.style.animationDelay = `${Math.min(index * 35, 210)}ms`;
    card.querySelector(".confidence-badge").textContent = `${confidence} confidence`;
    card.querySelector(".issue-title").textContent = issueTitle(issue);
    card.querySelector(".issue-location").textContent = `${issue.file || "Unknown file"} · ${issue.symbol || "Unknown symbol"}`;
    card.querySelector(".issue-summary").textContent = issue.root_cause || issue.what_failed;
    card.querySelector(".view-issue").addEventListener("click", () => viewRepositoryCode(issue, card));
    issuesList.append(card);
  });

  selectIssue(issues[0], issuesList.firstElementChild);
}

function renderInspectorWelcome(title, message) {
  inspectorContent.replaceChildren();
  const wrapper = document.createElement("div");
  wrapper.className = "inspector-welcome";
  const orb = document.createElement("span");
  orb.className = "inspector-orb";
  orb.textContent = "✦";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const copy = document.createElement("p");
  copy.textContent = message;
  wrapper.append(orb, heading, copy);
  inspectorContent.append(wrapper);
}

function detailBlock(label, value, className = "") {
  const block = document.createElement("section");
  block.className = `detail-block ${className}`.trim();
  const heading = document.createElement("h4");
  heading.textContent = label;
  const content = document.createElement("p");
  content.textContent = value || "Not provided.";
  block.append(heading, content);
  return block;
}

function selectIssue(issue, card) {
  state.selectedIssue = issue;
  document.querySelectorAll(".issue-card.selected").forEach((item) => item.classList.remove("selected"));
  card?.classList.add("selected");
  inspectorContent.replaceChildren();

  const detail = document.createElement("article");
  detail.className = "inspector-detail";
  const heading = document.createElement("header");
  heading.className = "detail-heading";
  const badge = document.createElement("span");
  badge.className = "confidence-badge";
  badge.textContent = `${issue.confidence || "low"} confidence`;
  const title = document.createElement("h3");
  title.textContent = issueTitle(issue);
  const location = document.createElement("p");
  location.textContent = `${issue.file || "Unknown file"} · ${issue.symbol || "Unknown symbol"}`;
  heading.append(badge, title, location);

  detail.append(
    heading,
    detailBlock("What failed", issue.what_failed),
    detailBlock("Why", issue.why),
    detailBlock("Likely root cause", issue.root_cause),
    detailBlock("Suggested fix", issue.suggested_fix, "fix"),
  );
  inspectorContent.append(detail);
}

async function viewRepositoryCode(issue, card) {
  selectIssue(issue, card);
  if (!state.repoId || !issue.file) return;
  try {
    const response = await fetch(`/api/repositories/${state.repoId}/file?path=${encodeURIComponent(issue.file)}`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorMessage(payload, response));
    const lines = payload.source.split("\n");
    const start = Math.max(1, (issue.line || 1) - 5);
    const end = Math.min(lines.length, (issue.end_line || issue.line || 1) + 5);
    const code = document.createElement("pre");
    code.className = "repository-code-preview";
    code.textContent = lines.slice(start - 1, end).map((line, index) => `${start + index}  ${line}`).join("\n");
    inspectorContent.querySelector(".inspector-detail")?.append(code);
  } catch (error) {
    notify(error.message, "error");
  }
}

function updateRepairSummary(fixed, alreadyResolved, failed) {
  repairProgressSummary.replaceChildren();
  for (const label of [
    `${fixed} fixed`,
    `${alreadyResolved} already resolved`,
    `${failed} failed`,
  ]) {
    const item = document.createElement("span");
    item.textContent = label;
    repairProgressSummary.append(item);
  }
}

function repairFeedItem(event) {
  const key = event.issue_id || event.stage;
  let item = repairActivityFeed.querySelector(`[data-repair-key="${CSS.escape(key)}"]`);
  if (!item) {
    item = document.createElement("li");
    item.dataset.repairKey = key;
    repairActivityFeed.append(item);
  }
  item.className = `${event.stage}${event.stage === "issue_fixed" && !event.provider ? " issue_already_resolved" : ""}`;
  const provider = event.provider ? ` · ${event.provider}` : "";
  item.textContent = `${event.message}${event.file ? ` — ${event.file}${provider}` : ""}`;
  repairActivityFeed.scrollTop = repairActivityFeed.scrollHeight;
}

function applyRepairProgressEvent(event) {
  repositoryRepairPanel.hidden = false;
  repairProgressTrack.style.width = `${event.progress}%`;
  repairProgressTitle.textContent = event.message;
  if (event.current != null && event.total != null) {
    repairProgressCount.textContent = `${event.current} / ${event.total}`;
  }
  if (event.stage === "repair_started") {
    repairActivityFeed.replaceChildren();
    updateRepairSummary(0, 0, 0);
  }
  if (["issue_fix_started", "issue_fixed", "issue_failed", "validation_started", "validation_completed", "zip_started", "zip_created"].includes(event.stage)) {
    repairFeedItem(event);
  }
  if (event.stage === "issue_fix_started") {
    fixRepositoryButton.querySelector("span").textContent = `Fixing ${event.current} / ${event.total}...`;
  }
  const fixed = repairActivityFeed.querySelectorAll(".issue_fixed:not(.issue_already_resolved)").length;
  const alreadyResolved = repairActivityFeed.querySelectorAll(".issue_already_resolved").length;
  const failed = repairActivityFeed.querySelectorAll(".issue_failed").length;
  updateRepairSummary(fixed, alreadyResolved, failed);
  if (event.stage === "repair_completed") {
    state.eventSource?.close();
    fixRepositoryButton.classList.remove("loading");
    fixRepositoryButton.querySelector("span").textContent = "Repair complete";
  } else if (event.stage === "repair_failed") {
    state.eventSource?.close();
    fixRepositoryButton.disabled = false;
    fixRepositoryButton.classList.remove("loading");
    fixRepositoryButton.querySelector("span").textContent = "Fix Repository";
  }
}

async function fixRepository() {
  if (!state.repoId || state.running || !state.result?.analysis?.length) return;
  state.running = true;
  fixRepositoryButton.disabled = true;
  fixRepositoryButton.classList.add("loading");
  fixRepositoryButton.querySelector("span").textContent = "Starting repair...";
  document.querySelector("#download-repository").hidden = true;
  repositoryRepairPanel.hidden = false;
  repairActivityFeed.replaceChildren();
  repairProgressTrack.style.width = "0%";
  repairProgressCount.textContent = `0 / ${state.result.analysis.length}`;
  updateRepairSummary(0, 0, 0);
  connectProgress(state.repoId, "repair");
  try {
    const response = await fetch(`/api/repositories/${state.repoId}/repair`, {
      method: "POST",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorMessage(payload, response));
    state.result = payload.updated_analysis;
    setMetrics(state.result.tests, payload.remaining_issue_count);
    renderIssues(state.result);
    updateRepairSummary(payload.fixed, payload.already_resolved, payload.failed);
    repairProgressTitle.textContent = "Repository repair complete";
    repairProgressCount.textContent = `${payload.total} / ${payload.total}`;
    const download = document.querySelector("#download-repository");
    download.href = `/api/repositories/${state.repoId}/download`;
    download.hidden = !payload.zip_ready;
    fixRepositoryButton.hidden = payload.zip_ready;
    notify(
      payload.failed
        ? `${payload.fixed} fixed; ${payload.failed} could not be safely fixed`
        : `${payload.fixed} repository issues fixed`,
      payload.failed ? "info" : "success",
    );
  } catch (error) {
    repairProgressTitle.textContent = error.message;
    fixRepositoryButton.disabled = false;
    fixRepositoryButton.classList.remove("loading");
    fixRepositoryButton.querySelector("span").textContent = "Fix Repository";
    notify(error.message, "error");
  } finally {
    state.running = false;
  }
}

function renderResult(result) {
  state.result = result;
  document.querySelector("#repository-name").textContent = `${result.repository} · ${result.job_id.slice(0, 8)}`;
  setMetrics(result.tests, result.issue_count);
  state.repoId = result.repo_id || result.job_id;
  const download = document.querySelector("#download-repository");
  download.href = `/api/repositories/${state.repoId}/download`;
  download.hidden = true;
  fixRepositoryButton.hidden = !result.analysis?.length;
  fixRepositoryButton.disabled = false;
  fixRepositoryButton.classList.remove("loading");
  fixRepositoryButton.querySelector("span").textContent = "Fix Repository";
  repositoryRepairPanel.hidden = true;
  outputElement.textContent = result.tests?.output || "No pytest output was returned.";
  outputButton.disabled = !result.tests?.output;
  outputPanel.hidden = true;
  outputButton.classList.remove("open");
  renderIssues(result);
  completePipeline(result);

  log(
    `pytest: ${result.tests.total} collected, ${result.tests.passed} passed, ${result.tests.failed} failed, ${result.tests.skipped} skipped.`,
    result.tests.failed ? "warn" : "info",
  );
  document.querySelector("#results").scrollIntoView({ behavior: "smooth", block: "start" });
}

function errorMessage(payload, response) {
  if (payload?.reason) return payload.reason;
  if (payload?.message) return payload.message;
  if (typeof payload?.detail === "string") return payload.detail;
  if (Array.isArray(payload?.detail)) {
    return payload.detail.map((item) => item.msg).join(", ");
  }
  return `Request failed with HTTP ${response.status}.`;
}

async function analyzeRepository(event) {
  event.preventDefault();
  if (state.running) return;

  const repositoryUrl = repositoryInput.value.trim();
  state.running = true;
  analyzeButton.disabled = true;
  analyzeButton.classList.add("loading");
  analyzeButton.querySelector("span").textContent = "Analyzing…";
  beginPipeline(repositoryUrl);

  try {
    const createResponse = await fetch("/api/repositories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repository_url: repositoryUrl }),
    });
    const created = await createResponse.json().catch(() => ({}));
    if (!createResponse.ok) {
      const requestError = new Error(errorMessage(created, createResponse));
      requestError.payload = created;
      throw requestError;
    }
    state.repoId = created.repo_id;
    connectProgress(state.repoId);
    const response = await fetch(`/api/repositories/${state.repoId}/analyze`, {
      method: "POST",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const requestError = new Error(errorMessage(payload, response));
      requestError.payload = payload;
      throw requestError;
    }

    renderResult(payload);
    notify("Repository analysis complete", "success");
  } catch (error) {
    const payload = error.payload || { error: "network_error" };
    failPipeline(payload);
    log(error.message, "error");
    showResultMessage("Analysis stopped", error.message, "error");
    notify(error.message, "error");
  } finally {
    state.eventSource?.close();
    state.running = false;
    analyzeButton.disabled = false;
    analyzeButton.classList.remove("loading");
    analyzeButton.querySelector("span").textContent = "Analyze repository";
  }
}

form.addEventListener("submit", analyzeRepository);
fixRepositoryButton.addEventListener("click", fixRepository);

document.querySelector("#clear-log").addEventListener("click", () => {
  activityLog.replaceChildren();
  log("Activity log cleared.", "note");
});

outputButton.addEventListener("click", () => {
  outputPanel.hidden = !outputPanel.hidden;
  outputButton.classList.toggle("open", !outputPanel.hidden);
});

document.querySelector("#copy-output").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(outputElement.textContent);
    notify("Pytest output copied", "success");
  } catch {
    notify("Clipboard access was unavailable", "error");
  }
});

document.querySelector("#collapse-sidebar").addEventListener("click", () => {
  if (window.innerWidth <= 720) {
    document.body.classList.remove("mobile-nav-open");
    return;
  }
  document.body.classList.toggle("sidebar-collapsed");
  localStorage.setItem(
    "fixflow-sidebar-collapsed",
    String(document.body.classList.contains("sidebar-collapsed")),
  );
});

document.querySelector("#mobile-menu").addEventListener("click", () => {
  document.body.classList.add("mobile-nav-open");
});

document.querySelector("#sidebar-scrim").addEventListener("click", () => {
  document.body.classList.remove("mobile-nav-open");
});

document.querySelectorAll(".nav-link[href^='#']").forEach((link) => {
  link.addEventListener("click", () => {
    document.querySelectorAll(".nav-link.active").forEach((item) => item.classList.remove("active"));
    link.classList.add("active");
    document.body.classList.remove("mobile-nav-open");
  });
});

if (localStorage.getItem("fixflow-sidebar-collapsed") === "true" && window.innerWidth > 720) {
  document.body.classList.add("sidebar-collapsed");
}

resetPipeline();

const fileAnalyzer = (() => {
  const fileState = {
    filename: "",
    fileId: null,
    source: "",
    issues: [],
    selectedIssueId: null,
    diff: "",
    busy: false,
  };
  const dashboard = document.querySelector("#dashboard");
  const analyzer = document.querySelector("#file-analyzer");
  const repositoryInspector = document.querySelector("#inspector");
  const chatPanel = document.querySelector("#file-chat");
  const fileInput = document.querySelector("#file-input");
  const uploadButton = document.querySelector("#upload-file-button");
  const analyzeFileButton = document.querySelector("#analyze-file-button");
  const fixButton = document.querySelector("#fix-file-button");
  const sourceEditor = document.querySelector("#source-editor");
  const syntaxCode = document.querySelector("#syntax-layer code");
  const syntaxLayer = document.querySelector("#syntax-layer");
  const gutter = document.querySelector("#editor-gutter");
  const marker = document.querySelector("#error-line-marker");
  const issuesList = document.querySelector("#file-issues-list");
  const emptyState = document.querySelector("#file-empty-state");
  const issueCount = document.querySelector("#file-issue-count");
  const chatForm = document.querySelector("#file-chat-form");
  const chatInput = document.querySelector("#chat-input");
  const chatSend = document.querySelector("#chat-send");
  const chatMessages = document.querySelector("#chat-messages");

  function activateWorkspace(kind, hash = "") {
    const isFile = kind === "file";
    dashboard.hidden = isFile;
    analyzer.hidden = !isFile;
    repositoryInspector.hidden = isFile;
    chatPanel.hidden = !isFile;
    document.querySelectorAll(".nav-link.active").forEach((item) => item.classList.remove("active"));
    const link = isFile
      ? document.querySelector("#file-analyzer-nav")
      : document.querySelector(`.nav-link[href="${hash || "#dashboard"}"]`);
    link?.classList.add("active");
    if (!isFile && hash === "#results") {
      requestAnimationFrame(() => document.querySelector("#results").scrollIntoView({ behavior: "smooth" }));
    } else {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
    document.body.classList.remove("mobile-nav-open");
  }

  const fileAnalyzerNav = document.querySelector("#file-analyzer-nav");
  if (fileAnalyzerNav.getAttribute("href") === "#file-analyzer") {
    fileAnalyzerNav.addEventListener("click", (event) => {
      event.preventDefault();
      activateWorkspace("file");
      history.replaceState(null, "", "#file-analyzer");
    });
  }
  document.querySelectorAll('.nav-link[href="#dashboard"], .nav-link[href="#results"]').forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      activateWorkspace("repository", link.getAttribute("href"));
      history.replaceState(null, "", link.getAttribute("href"));
    });
  });

  function escapeHtml(value) {
    return value
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function highlightPlain(value) {
    const escaped = escapeHtml(value);
    return escaped
      .replace(/\b(False|None|True|and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield)\b/g, '<span class="syntax-keyword">$1</span>')
      .replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="syntax-number">$1</span>')
      .replace(/\b(print|len|range|str|int|float|list|dict|set|tuple|open|super|enumerate|zip|min|max|sum)\b/g, '<span class="syntax-builtin">$1</span>');
  }

  function highlightLine(line) {
    let output = "";
    let plain = "";
    let quote = null;
    let string = "";
    let escaped = false;
    const flushPlain = () => {
      output += highlightPlain(plain);
      plain = "";
    };
    for (let index = 0; index < line.length; index += 1) {
      const character = line[index];
      if (quote) {
        string += character;
        if (escaped) escaped = false;
        else if (character === "\\") escaped = true;
        else if (character === quote) {
          output += `<span class="syntax-string">${escapeHtml(string)}</span>`;
          quote = null;
          string = "";
        }
      } else if (character === '"' || character === "'") {
        flushPlain();
        quote = character;
        string = character;
      } else if (character === "#") {
        flushPlain();
        output += `<span class="syntax-comment">${escapeHtml(line.slice(index))}</span>`;
        return output;
      } else {
        plain += character;
      }
    }
    flushPlain();
    if (string) output += `<span class="syntax-string">${escapeHtml(string)}</span>`;
    return output || " ";
  }

  function renderEditor() {
    fileState.source = sourceEditor.value;
    const lines = sourceEditor.value.split("\n");
    syntaxCode.innerHTML = lines.map(highlightLine).join("\n") + (sourceEditor.value.endsWith("\n") ? "\n " : "");
    const issueLines = new Set(fileState.issues.map((issue) => issue.line));
    gutter.replaceChildren(...lines.map((_, index) => {
      const line = document.createElement("span");
      line.textContent = String(index + 1);
      if (issueLines.has(index + 1)) line.classList.add("issue-line");
      return line;
    }));
    syncEditorScroll();
    updateCursor();
  }

  function syncEditorScroll() {
    syntaxLayer.scrollTop = sourceEditor.scrollTop;
    syntaxLayer.scrollLeft = sourceEditor.scrollLeft;
    gutter.scrollTop = sourceEditor.scrollTop;
    if (!marker.hidden) {
      marker.style.transform = `translateY(${15 - sourceEditor.scrollTop}px)`;
    }
  }

  function updateCursor() {
    const beforeCursor = sourceEditor.value.slice(0, sourceEditor.selectionStart);
    const lines = beforeCursor.split("\n");
    document.querySelector("#cursor-position").textContent = `Ln ${lines.length}, Col ${lines.at(-1).length + 1}`;
  }

  function setStatus(label, copy, kind = "") {
    const badge = document.querySelector("#file-state-badge");
    badge.textContent = label;
    badge.className = `status-badge ${kind}`.trim();
    document.querySelector("#file-state-copy").textContent = copy;
  }

  function resetAnalysis() {
    fileState.fileId = null;
    fileState.issues = [];
    fileState.selectedIssueId = null;
    fileState.diff = "";
    issuesList.replaceChildren();
    issueCount.textContent = "Not analyzed";
    fixButton.disabled = true;
    chatInput.disabled = true;
    chatSend.disabled = true;
    chatMessages.innerHTML = '<div class="chat-welcome"><span>✦</span><h3>Ask about this file</h3><p>Analyze a file, then ask about findings, risky lines, or validated changes.</p></div>';
    document.querySelector("#repair-summary").hidden = true;
    document.querySelector("#view-file-diff").hidden = true;
    document.querySelector("#download-fixed-file").hidden = true;
    emptyState.hidden = false;
    emptyState.className = "empty-state";
    emptyState.querySelector("h3").textContent = "Ready to analyze";
    emptyState.querySelector("p").textContent = "The file is loaded. Analyze it to find syntax, security, and logic issues.";
    marker.hidden = true;
  }

  async function loadLocalFile(file) {
    if (!file.name.toLowerCase().endsWith(".py")) {
      notify("Only Python (.py) files are supported", "error");
      return;
    }
    if (file.size > 1_000_000) {
      notify("The file exceeds the 1 MB limit", "error");
      return;
    }
    let source;
    try {
      source = await file.text();
    } catch {
      notify("The file could not be read", "error");
      return;
    }
    if (source.includes("\0")) {
      notify("Binary files are not supported", "error");
      return;
    }
    fileState.filename = file.name;
    sourceEditor.value = source;
    sourceEditor.disabled = false;
    document.querySelector("#file-name").textContent = file.name;
    document.querySelector("#editor-filename").textContent = file.name;
    analyzeFileButton.disabled = false;
    resetAnalysis();
    renderEditor();
    setStatus("FILE LOADED", "Ready to analyze", "running");
    notify(`${file.name} loaded`, "success");
  }

  function showAnalyzingState() {
    emptyState.hidden = false;
    emptyState.className = "empty-state";
    emptyState.querySelector("h3").textContent = "Scanning file…";
    emptyState.querySelector("p").textContent = "Running syntax, static, security, and Gemini review signals.";
    issuesList.replaceChildren();
    issueCount.textContent = "Analyzing…";
    setStatus("ANALYZING", "Scanning file…", "running");
  }

  function renderFileIssues(issues) {
    fileState.issues = issues || [];
    fileState.selectedIssueId = fileState.issues[0]?.issue_id || null;
    issuesList.replaceChildren();
    issueCount.textContent = `${fileState.issues.length} ${fileState.issues.length === 1 ? "issue" : "issues"}`;
    if (!fileState.issues.length) {
      emptyState.hidden = false;
      emptyState.className = "empty-state success";
      emptyState.querySelector("h3").textContent = "No issues detected";
      emptyState.querySelector("p").textContent = "The current source passed all available analysis signals.";
    } else {
      emptyState.hidden = true;
      fileState.issues.forEach((issue, index) => {
        const card = document.createElement("article");
        card.className = "file-issue-card";
        card.dataset.issueId = issue.issue_id;
        if (index === 0) card.classList.add("selected");
        card.dataset.severity = issue.severity;
        card.style.animationDelay = `${Math.min(index * 35, 210)}ms`;
        const body = document.createElement("div");
        const meta = document.createElement("div");
        meta.className = "file-issue-meta";
        const severity = document.createElement("span");
        severity.textContent = issue.severity;
        const category = document.createElement("span");
        category.textContent = issue.type.replaceAll("_", " ");
        meta.append(severity, category);
        const title = document.createElement("h3");
        title.textContent = issue.title;
        body.append(meta, title);
        const location = document.createElement("small");
        location.textContent = issue.end_line > issue.line ? `Lines ${issue.line}–${issue.end_line}` : `Line ${issue.line}`;
        const description = document.createElement("p");
        description.textContent = `${issue.description} ${issue.suggestion}`;
        card.append(body, location, description);
        card.addEventListener("click", () => focusIssue(issue, card));
        issuesList.append(card);
      });
    }
    renderEditor();
  }

  function focusIssue(issue, card) {
    fileState.selectedIssueId = issue.issue_id;
    document.querySelectorAll(".file-issue-card.selected").forEach((item) => item.classList.remove("selected"));
    card?.classList.add("selected");
    const lines = sourceEditor.value.split("\n");
    const start = lines.slice(0, issue.line - 1).reduce((total, line) => total + line.length + 1, 0);
    const end = start + (lines[issue.line - 1]?.length || 0);
    sourceEditor.focus();
    sourceEditor.setSelectionRange(start, end);
    sourceEditor.scrollTop = Math.max(0, (issue.line - 4) * 20);
    marker.hidden = false;
    marker.style.top = `${(issue.line - 1) * 20}px`;
    syncEditorScroll();
    updateCursor();
  }

  async function analyzeCurrentFile() {
    if (fileState.busy || !fileState.filename) return;
    fileState.busy = true;
    analyzeFileButton.disabled = true;
    analyzeFileButton.classList.add("loading");
    analyzeFileButton.querySelector("span").textContent = "Analyzing…";
    showAnalyzingState();
    const formData = new FormData();
    formData.append("file", new Blob([sourceEditor.value], { type: "text/x-python" }), fileState.filename);
    try {
      const response = await fetch("/api/files/analyze", { method: "POST", body: formData });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorMessage(payload, response));
      fileState.fileId = payload.file_id;
      fileState.filename = payload.filename;
      sourceEditor.value = payload.source;
      document.querySelector("#file-name").textContent = payload.filename;
      document.querySelector("#editor-filename").textContent = payload.filename;
      renderFileIssues(payload.issues);
      fixButton.disabled = payload.issues.length === 0;
      chatInput.disabled = false;
      chatSend.disabled = false;
      setStatus(
        payload.issues.length ? "ISSUES FOUND" : "CLEAN",
        payload.issues.length ? `${payload.issues.length} issues detected` : "0 issues detected",
        payload.issues.length ? "error" : "success",
      );
      if (payload.gemini_status === "unavailable") notify("Local analysis complete; Gemini was unavailable", "info");
      else notify("File analysis complete", "success");
    } catch (error) {
      emptyState.hidden = false;
      emptyState.className = "empty-state error";
      emptyState.querySelector("h3").textContent = "Analysis could not be completed";
      emptyState.querySelector("p").textContent = error.message;
      issueCount.textContent = "Analysis failed";
      setStatus("ERROR", "Analysis could not be completed", "error");
      notify(error.message, "error");
    } finally {
      fileState.busy = false;
      analyzeFileButton.disabled = false;
      analyzeFileButton.classList.remove("loading");
      analyzeFileButton.querySelector("span").textContent = "Analyze file";
    }
  }

  async function fixCurrentFile() {
    if (fileState.busy || !fileState.fileId || !fileState.selectedIssueId) return;
    const requestedIssueId = fileState.selectedIssueId;
    fileState.busy = true;
    fixButton.disabled = true;
    fixButton.classList.add("loading");
    fixButton.querySelector("span").textContent = "Repairing…";
    setStatus("FIXING", "FixFlow is repairing…", "running");
    try {
      const response = await fetch(`/api/files/${fileState.fileId}/fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ issue_id: requestedIssueId }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorMessage(payload, response));
      if (payload.success === false) throw new Error(payload.reason || "The selected issue was not repaired.");
      sourceEditor.value = payload.fixed_code || payload.source;
      fileState.diff = payload.diff;
      renderFileIssues(payload.analysis?.issues || payload.issues);
      document.querySelector("#repair-summary").hidden = false;
      document.querySelector("#before-issues").textContent = payload.before_count;
      document.querySelector("#after-issues").textContent = payload.after_count;
      document.querySelector("#fixed-issues").textContent = payload.issues_fixed;
      document.querySelector("#changed-lines").textContent = payload.lines_changed;
      document.querySelector("#file-diff").textContent = payload.diff || "No source lines changed.";
      document.querySelector("#view-file-diff").hidden = false;
      const download = document.querySelector("#download-fixed-file");
      download.href = `/api/files/${fileState.fileId}/download`;
      download.hidden = false;
      fixButton.disabled = payload.issues.length === 0;
      setStatus(
        payload.after_count === 0 ? "FIXED" : "PARTIALLY FIXED",
        payload.after_count === 0 ? "0 issues remaining ✓" : `${payload.after_count} issues remaining`,
        payload.after_count === 0 ? "success" : "error",
      );
      notify(payload.after_count === 0 ? "Repair validated" : "Repair completed with remaining issues", payload.after_count === 0 ? "success" : "info");
    } catch (error) {
      console.error("FixFlow file repair failed", { fileId: fileState.fileId, issueId: requestedIssueId, error });
      setStatus("ERROR", "Repair could not be completed", "error");
      notify(error.message, "error");
      fixButton.disabled = false;
    } finally {
      fileState.busy = false;
      fixButton.classList.remove("loading");
      fixButton.querySelector("span").textContent = "Fix Now";
      fixButton.disabled = fileState.issues.length === 0 || !fileState.selectedIssueId;
    }
  }

  function appendChat(role, message) {
    chatMessages.querySelector(".chat-welcome")?.remove();
    const bubble = document.createElement("div");
    bubble.className = `chat-message ${role}`;
    const label = document.createElement("b");
    label.textContent = role === "user" ? "You" : "FixFlow";
    bubble.append(label, document.createTextNode(message));
    chatMessages.append(bubble);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return bubble;
  }

  async function sendChat(event) {
    event.preventDefault();
    const message = chatInput.value.trim();
    if (!message || !fileState.fileId || fileState.busy) return;
    appendChat("user", message);
    chatInput.value = "";
    chatInput.disabled = true;
    chatSend.disabled = true;
    const pending = appendChat("assistant", "Thinking…");
    try {
      const response = await fetch(`/api/files/${fileState.fileId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorMessage(payload, response));
      pending.lastChild.textContent = payload.answer;
    } catch (error) {
      pending.lastChild.textContent = `I couldn't answer that: ${error.message}`;
    } finally {
      chatInput.disabled = false;
      chatSend.disabled = false;
      chatInput.focus();
    }
  }

  uploadButton.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) loadLocalFile(fileInput.files[0]);
    fileInput.value = "";
  });
  analyzeFileButton.addEventListener("click", analyzeCurrentFile);
  fixButton.addEventListener("click", fixCurrentFile);
  sourceEditor.addEventListener("input", () => {
    if (fileState.fileId) {
      resetAnalysis();
      setStatus("FILE LOADED", "Code changed · analyze again", "running");
    }
    renderEditor();
  });
  sourceEditor.addEventListener("scroll", syncEditorScroll);
  sourceEditor.addEventListener("click", updateCursor);
  sourceEditor.addEventListener("keyup", updateCursor);
  sourceEditor.addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
      event.preventDefault();
      const start = sourceEditor.selectionStart;
      const end = sourceEditor.selectionEnd;
      sourceEditor.setRangeText("    ", start, end, "end");
      sourceEditor.dispatchEvent(new Event("input"));
    }
  });
  document.querySelector("#view-file-diff").addEventListener("click", () => {
    const panel = document.querySelector("#file-diff-panel");
    panel.hidden = !panel.hidden;
    if (!panel.hidden) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  document.querySelector("#copy-file-diff").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(fileState.diff);
      notify("Diff copied", "success");
    } catch {
      notify("Clipboard access was unavailable", "error");
    }
  });
  chatForm.addEventListener("submit", sendChat);
  chatInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      chatForm.requestSubmit();
    }
  });

  if (window.location.hash === "#file-analyzer") activateWorkspace("file");
  renderEditor();
  return { activateWorkspace };
})();
