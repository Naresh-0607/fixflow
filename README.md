# FixFlow

FixFlow is a backend service for diagnosing and autonomously repairing failing
tests in public Python repositories. Give it a GitHub repository URL and it
creates an isolated job, runs the complete pytest suite in Docker, asks Gemini
to understand the failures, and optionally applies and validates minimal source
patches.

FixFlow never pushes changes, creates commits, or modifies the remote GitHub
repository. Every proposed repair remains as an inspectable, uncommitted diff
inside that job's local workspace.

## What the application does

FixFlow exposes two workflows:

- **Analyze** runs the repository's tests and returns structured root-cause
  analysis for every failure without changing source files.
- **Repair** runs the same analysis, asks Gemini for bounded unified-diff
  changes, retests the entire suite, rolls back regressions, and reflects on
  remaining failures until the tests pass or a safe stop condition is reached.

Only public Python repositories hosted on GitHub are supported.

## Application flow

### Repository analysis

```text
GitHub repository URL
        ↓
Create isolated job workspace
        ↓
Clone repository and detect Python project
        ↓
Generate controlled Docker environment
        ↓
Install dependencies and run complete pytest suite
        ↓
Read authoritative counts from JUnit XML
        ↓
Collect failure names, messages, and traces from pytest output
        ↓
Select relevant tests and source files
        ↓
Gemini root-cause analysis
        ↓
Structured API response
```

### Autonomous repair

```text
Initial analysis
        ↓
Gemini proposes minimal unified diffs
        ↓
Validate paths, file types, patch size, and patch structure
        ↓
Apply changes transactionally inside the job repository
        ↓
Rebuild and run the complete pytest suite in Docker
        ↓
Compare previous and current results
        ↓
  ┌─────────────┬───────────────┬──────────────┐
  │ tests pass  │ result better │ regression   │
  ↓             ↓               ↓
Complete      Reflect and      Restore exact pre-iteration files
              continue         and request a different repair
```

The loop stops when all tests pass, the configured iteration limit is reached,
Gemini remains unavailable after retries and fallback, or a proposed change is
unsafe.

## Job workspace

Each request receives a unique job ID and uses this layout:

```text
workspaces/<job-id>/
├── repository/                  cloned repository and accepted working changes
├── artifacts/junit.xml         pytest result counts
├── FixFlow.Dockerfile           generated test environment
└── FixFlow.Dockerfile.dockerignore
```

The Docker and artifact files stay outside `repository/`, so they cannot affect
Git diffs or modified-file tracking. Cloned code is never imported or executed
directly by the FixFlow host; dependency installation and pytest run in Docker.

## Prerequisites

- Python 3.10 or newer
- Git on `PATH`
- Docker CLI with a running Linux-container Docker daemon
- A Google Gemini API key

## Setup

From the `FixFlow` directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `GEMINI_API_KEY` in `.env`. FixFlow loads the project-level file once when
its central configuration module initializes. Existing process environment
variables take precedence over values from `.env`.

`GEMINI_MODEL_PRIMARY` selects the normal Gemini model and
`GEMINI_MODEL_FALLBACK` optionally selects a second model. Temporary provider
failures exhaust bounded retries on the primary before the shared Gemini client
switches to the fallback. An empty fallback keeps single-model behavior.

You may still override the value for one PowerShell session when needed:

```powershell
$env:GEMINI_API_KEY = "your-api-key"
```

Never commit the key; `.env` is ignored.

## Run in development

From WSL/Linux, restrict reload watching to the application source directory:

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --reload-dir app
```

Do not use an unrestricted `--reload` from the FixFlow root. Analysis clones
Python files into `workspaces/`, and watching that directory can restart the
server while `/api/analyze` is still running.

For production or a stable local run, disable reload entirely:

```bash
uvicorn app.main:app
```

On Windows PowerShell, the equivalent development command is:

```powershell
.\.venv\Scripts\uvicorn.exe app.main:app --reload --reload-dir app
```

Open `http://127.0.0.1:8000/docs` or send a request:

```powershell
$body = @{ repository_url = "https://github.com/user/project.git" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/analyze" -ContentType "application/json" -Body $body
```

The browser dashboard is available at `http://127.0.0.1:8000/`. It is plain
HTML, local CSS, and vanilla JavaScript served by FastAPI, so there is no Node
runtime, package installation, build command, or separate frontend server.

Analysis and repair requests are synchronous. Every request receives a unique
job ID and is stored under:

```text
workspaces/<job-id>/repository/
```

## Response behavior

- `issues_found`: pytest failed and Gemini returned structured analysis.
- `tests_passed`: all collected tests passed; Gemini is not called.
- `no_tests`: pytest collected no tests.
- `analysis_failed`: pytest results are returned, but Gemini was unavailable or
  returned invalid data.

The response includes complete pytest stdout, stderr, combined output, exit
code, counts, failed test identifiers, concise errors, and captured traces.
Counts come from job-scoped JUnit XML artifacts outside the cloned repository;
raw pytest output is retained only for failure identifiers and details.

Repair responses use these statuses:

- `fixed`: all collected tests pass after one or more accepted repairs.
- `already_passing`: the initial suite passed and Gemini was not called.
- `partial`: the configurable iteration limit was reached with failures left.
- `unsafe_change`: a path or patch failed safety validation before modification.
- `gemini_unavailable`: bounded provider retries were exhausted.
- `no_tests`: pytest collected no tests and no repair was attempted.

Every repair response includes the initial commit/status/diff, initial and final
test results, complete iteration history, accepted and rejected changes,
modified files, additions, deletions, and the final uncommitted Git diff.

Clone errors, unsupported repositories, Docker failures, dependency build
failures, missing pytest, timeouts, and Gemini failures return explicit error
messages. Workspaces are retained for Phase 1 inspection; generated Docker
images and timed-out test containers are removed.

Docker startup errors are deliberately distinct:

- `docker_cli_unavailable`: no Docker executable was found on `PATH`.
- `docker_daemon_unavailable`: the CLI exists, but `docker info` cannot reach
  the daemon.
- `docker_execution_failed`: pull, build, or container startup failed.
- `docker_timeout`: a bounded Docker operation exceeded its timeout.

## Security boundaries

- Only HTTPS URLs on `github.com` with `owner/repository` paths are accepted.
- Git prompts, Git LFS downloads, and local/file Git protocols are disabled.
- Existing repository Dockerfiles are ignored; FixFlow generates its own.
- Tests run with no network, bounded CPU/memory/PIDs, all Linux capabilities
  dropped, and `no-new-privileges` enabled.
- The repository is copied into an ephemeral image and is never host-executed.
- Gemini receives complete pytest output plus a bounded, traceback-driven set
  of relevant files rather than the whole repository.
- Gemini never receives shell access. FixFlow exposes only bounded file listing,
  reading, literal code search, patching, restoration, Git diff, and Docker test
  operations confined to `workspaces/<job-id>/repository/`.
- Absolute paths, traversal, symlinks, binary files, file creation/deletion,
  renames, mode changes, oversized patches, and too many files per iteration are
  rejected. Regressions restore exact pre-iteration file bytes.
- FixFlow never commits, pushes, or opens a pull request.

## Repair request

```powershell
$body = @{ repository_url = "https://github.com/user/buggy-project.git" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/repair" -ContentType "application/json" -Body $body
```

The default repair limit is five iterations. Relevant environment settings are
`MAX_ITERATIONS`, `MAX_FILES_PER_ITERATION`, `MAX_PATCH_CHARACTERS`,
`GEMINI_MAX_RETRIES`, and `GEMINI_RETRY_BACKOFF_SECONDS`.

## Test FixFlow itself

```powershell
.\.venv\Scripts\pytest.exe -v
```

The tests mock remote cloning, Docker execution, and Gemini. Phase 2 patch tests
use temporary local Git repositories to verify real diff application and exact
rollback; they do not contact GitHub or invoke Docker.

## Current exclusions

There is no JavaScript/TypeScript or Java repository support, ZIP generation,
GitHub push, automatic pull request, Ruff, or Bandit integration. The existing
dashboard remains unchanged; autonomous repair is available through the API.
