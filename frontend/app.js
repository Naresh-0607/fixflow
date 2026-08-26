const state = {
  running: false,
  result: null,
  selectedIssue: null,
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
const toast = document.querySelector("#toast");

let waitingTimer;
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
}

function beginPipeline(repositoryUrl) {
  resetPipeline();
  pipelineSteps[0].classList.add("running");
  progressTrack.classList.add("running");
  progressBadge.className = "status-badge running";
  progressBadge.textContent = "RUNNING";
  progressTitle.textContent = "Analysis in progress";
  setRuntime("busy", "Analysis running", "Waiting for backend workflow");
  document.querySelector("#terminal-title").textContent = "fixflow / current-job.log";
  log(`POST /api/analyze → ${repositoryUrl}`, "proc");
  log("The backend is cloning, containerizing, testing, and analyzing.", "note");
  log("Phase 1 does not stream stage events; this request may take several minutes.", "note");

  clearInterval(waitingTimer);
  waitingTimer = setInterval(() => {
    log("Still waiting for the isolated backend workflow to finish…", "proc");
  }, 20000);
}

function completePipeline(result) {
  clearInterval(waitingTimer);
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
  clearInterval(waitingTimer);
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

function setMetrics(tests) {
  document.querySelector("#metric-total").textContent = tests?.total ?? "—";
  document.querySelector("#metric-passed").textContent = tests?.passed ?? "—";
  document.querySelector("#metric-failed").textContent = tests?.failed ?? "—";
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
    card.style.animationDelay = `${Math.min(index * 35, 210)}ms`;
    card.querySelector(".confidence-badge").textContent = `${confidence} confidence`;
    card.querySelector(".issue-title").textContent = issueTitle(issue);
    card.querySelector(".issue-location").textContent = `${issue.file || "Unknown file"} · ${issue.symbol || "Unknown symbol"}`;
    card.querySelector(".issue-summary").textContent = issue.root_cause || issue.what_failed;
    card.querySelector(".view-issue").addEventListener("click", () => selectIssue(issue, card));
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

function renderResult(result) {
  state.result = result;
  document.querySelector("#repository-name").textContent = `${result.repository} · ${result.job_id.slice(0, 8)}`;
  setMetrics(result.tests);
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
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repository_url: repositoryUrl }),
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
    clearInterval(waitingTimer);
    state.running = false;
    analyzeButton.disabled = false;
    analyzeButton.classList.remove("loading");
    analyzeButton.querySelector("span").textContent = "Analyze repository";
  }
}

form.addEventListener("submit", analyzeRepository);

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
