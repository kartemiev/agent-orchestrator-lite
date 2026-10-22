 English | [Русский](README.ru.md)

# agent-orchestrator-lite

A lightweight public version of `agent-orchestrator`: only the core contour for
executing a pre-built JSON plan through OpenHands under Temporal control.

## Key Concepts & Glossary

Four terms explain almost everything about how the orchestrator thinks. Read
this before the architecture section — everything below builds on these ideas.

| Term | In one sentence | Everyday analogy |
|---|---|---|
| **Sprint** (Execution Plan) | One JSON document describes the whole feature. | A recipe for the entire dinner, not one chopped onion. |
| **DAG** | A step graph where `depends_on` sets the arrows. | A to-do list where some items block others. |
| **Wave Execution** | All unblocked steps of one level run in parallel. | Several cooks, each starting their own dish. |
| **Human-in-the-Loop (HITL)** | A human approves the plan before the first agent starts. | The pre-flight checklist signed by the pilot. |

### Sprint (Execution Plan)

A **sprint** is the unit of work: one JSON plan = one workflow = one isolated
workspace `/app/workspace/runs/{workflow_id}`. The plan does not describe *how*
to write the code — it describes *which* steps to run, with which model and in
which order. Example: [`self-update.json`](./examples/self-update.json) is a
sprint of 5 steps.

```json
{
  "graph_name": "Self-Update Sprint",
  "tasks": [{ "id": "task_0_git_sync", "depends_on": [], "engine": "openhands" }]
}
```

### DAG (Directed Acyclic Graph)

Steps form a graph, not a flat list. Each step declares its prerequisites in
`depends_on`, and may start only when every id listed there is done.

- **Directed** — dependencies have a direction: `git-sync → backend`, never back.
- **Acyclic** — no loops. If A waits for B and B waits for A, the workflow stops
  with `RuntimeError: cyclic dependency or unreachable step`.

```
Wave 1          Wave 2                      Wave 3     Wave 4
git-sync ──┬──→ backend/docs ──┐
           └──→ ui           ──┴──────────→ tests ───→ deliver (PR)
```

### Wave Execution

A **wave** is one "floor" of the DAG: every step that is ready at the same
moment. The executor collects the ready steps and launches them concurrently
with `asyncio.gather`, waits for the whole wave, then computes the next one.

- Parallelism is defined **only** by `depends_on`. `priority` changes nothing
  here and never makes independent steps wait for each other.
- The wave width is capped by `wave_max_concurrency` (per plan / per request) or
  by `WAVE_MAX_CONCURRENCY` (default `4`, ceiling `32`); extra ready steps are
  started in batches.
- A failure is isolated: the failed step is dropped, steps depending on it are
  skipped, and independent steps of the same wave still finish.
- The final `git push` + Pull Request delivery runs only if no step failed.

### Human-in-the-Loop (HITL)

Before Wave 1 runs, the workflow parks at an approval gate: it publishes the
parsed plan and waits for a signal.

| Signal / flag | Effect |
|---|---|
| `approve_plan_signal` | The plan is accepted — and may even be corrected by the human before launch. |
| `reject_plan_signal` | The plan is rejected with a reason; nothing is executed. |
| `auto_approve` | The gate is skipped for trusted, ready-made plans (Streamlit checkbox or `"auto_approve": true` in the API call). |

The gate is what makes it safe to hand a whole feature to the orchestrator: the
human reviews the decomposition, not every generated line of code.

## What's inside

- **FastAPI Gateway** (`orchestrator/main.py`) — `GET /health` and `POST /api/v1/plan/direct`.
- **Temporal Server + Postgres** (`temporal`, `temporal-db`) — orchestration and workflow history.
- **OpenHands** (`openhands`) — the agent that executes the phases of a DAG plan (terminal + file_editor).
- **Single worker** (`orchestrator/worker.py`) — registers only
  `EnterpriseHITLWorkflow` and the base activities.
- **Streamlit Dashboard** (`agent-ui`) — JSON plan input, auto-approve and
  workflow monitoring/signals.

## What was removed compared to the full version

- The `agent-aider`, `agent-aitm`, `agent-dogfood`, `agent-swarm` workers and queues.
- The Council of Elders (Council), `council_panel.json`,
  `scripts/multi_model_review_dispatcher.py` and all their dependencies.
- The LLM Planner (auto-decomposition via DeepSeek) and the
  OpenRouter / SiliconFlow / DashScope / Resend integrations.
- The Canvas fast-path (`canvas_claude_streamer.py`, `canvas_bridge.py`).
- The `scripts/` folder and the extra specifications/blueprints.

## Quick start

```bash
cd agent-orchestrator-lite
cp .env.example .env        # fill in LLM_API_KEY and GITHUB_TOKEN
docker compose up -d
```

Once everything is up:

| Service | Address |
|---|---|
| Gateway API | http://127.0.0.1:8085 (health: `/health`) |
| Streamlit UI | http://127.0.0.1:8501 |
| Temporal UI | http://127.0.0.1:8233 |
| OpenHands | http://127.0.0.1:8000 |

Shutdown: `docker compose down`.

## Production Deployment & Security

The stack above listens on `127.0.0.1` only. Before exposing it to the internet
(Streamlit UI, Gateway API, webhooks), follow the dedicated guide:

📘 **[Securing a public deployment with Nginx Proxy Manager and Cloudflare Access](docs/deployment/securing-with-npm-and-cloudflare.md)**

It covers, step by step:

- **Nginx Proxy Manager** — proxy hosts for `agent-ui:8501` and
  `agent-orchestrator:8000`, a reusable **Access List** (HTTP Basic Auth), and
  importing a **custom SSL certificate** (StartSSL / Custom CRT + Key, or a
  Cloudflare Origin CA certificate).
- **⚠️ `Websockets Support`** — the toggle that must be ON for the Streamlit
  host; without it the dashboard loads but hangs on “Please wait…” /
  “Connection error”.
- **Cloudflare Zero Trust / Access** — an Access application with **Email PIN /
  OTP** login for the UI, plus **Bypass / service-token rules** for `/api/*` and
  `/webhook/*` so CI and GitHub webhooks are not blocked by the identity gate.
- Verification commands (`curl`, `openssl`, WebSocket probe), a troubleshooting
  table and a security checklist.

Russian version: [`securing-with-npm-and-cloudflare.ru.md`](docs/deployment/securing-with-npm-and-cloudflare.ru.md).

## Running a workflow

### Via the REST API

```bash
curl -X POST http://127.0.0.1:8085/api/v1/plan/direct \
  -H 'Content-Type: application/json' \
  -d '{"raw_json": "{\"tasks\": [{\"id\": \"step_1\", \"title\": \"Hello\", \"prompt\": \"Create a hello.txt file containing hi\", \"depends_on\": [], \"engine\": \"openhands\"}]}", "auto_approve": true}'
```

Response: `{"status": "triggered", "workflow_id": "direct-plan-...", ...}`.

### Via the UI

1. Paste a ready-made JSON plan into the "Ready-made JSON plan" field.
2. Tick `Auto-approve` (or approve the plan manually in the monitoring panel).
3. Press "Run".

## JSON plan format

```json
{
  "reasoning": "Optional rationale",
  "tasks": [
    {
      "id": "step_1",
      "title": "Git Sync",
      "prompt": "Clone the repository https://github.com/owner/repo and prepare the environment.",
      "depends_on": [],
      "model": "deepseek-v4-pro",
      "engine": "openhands"
    },
    {
      "id": "step_2",
      "title": "Create the model",
      "prompt": "Create the Pydantic model User in the file app/models.py",
      "depends_on": ["step_1"],
      "engine": "openhands"
    }
  ]
}
```

- `id` — unique step identifier (generated as `task_N` when omitted).
- `depends_on` — list of step ids the step depends on. Parallelism is driven
  solely by `depends_on`: steps with the same set of dependencies are executed
  in one wave via `asyncio.gather`.
- `prompt` — instruction for the OpenHands agent.
- `engine` — in the Lite version the only engine is `openhands` (all steps,
  including git-sync, are executed by OpenHands).
- `model` — optional per-node LLM model (otherwise `LLM_MODEL` from `.env`). The name can be a base name (`deepseek-v4-pro`, `gpt-4o`) or carry a LiteLLM prefix (`openrouter/...`, `deepseek/...`).

The full format specification and the DAG-building rules for an LLM architect
live in [`AGENTS.md`](./AGENTS.md) (a system prompt for ChatGPT / Claude / DeepSeek).

The wave-based DAG executor runs the steps strictly according to the submitted
plan: a wave of ready tasks is launched in parallel, a single failing task is
isolated (dependent steps are skipped), and at the end the final `git push` + PR
delivery and workspace cleanup happen.

## Usage examples

📘 **[End-to-end tutorial: building a feature with ChatGPT + Streamlit](docs/examples/notification-service.md)** —
a start-to-finish scenario: design a DAG plan in an external LLM chat and run it
in the orchestrator (ready plan: [`notification-service.json`](./examples/notification-service.json)).

### Repository self-update

The ready-made "the orchestrator updates itself" example lives in
[`self-update.json`](./examples/self-update.json), and its detailed description
is in [`self-update.md`](./docs/examples/self-update.md).

Short run via cURL (the plan is taken from the file directly):

```bash
curl -X POST http://localhost:8085/api/v1/plan/direct \
  -H "Content-Type: application/json" \
  -d @examples/self-update.json
```

The same with Auto-approve (no manual confirmation):

```bash
curl -X POST http://localhost:8085/api/v1/plan/direct \
  -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json; d=json.load(open("examples/self-update.json")); print(json.dumps({"raw_json": json.dumps(d), "auto_approve": True}))')"
```

The plan contains 5 tasks across 4 waves: `git-sync` → parallel
`backend/docs` + `ui` → `tests` → `deliver` (commit + push + PR).

## Environment variables

See [`.env.example`](./.env.example). Detailed configuration of LLM providers
(OpenAI / Anthropic / OpenRouter / DeepSeek) and of the OpenHands container is described in
[`openhands-setup.md`](./docs/openhands-setup.md). The key ones:

| Variable | Description | Default |
|---|---|---|
| `LLM_API_KEY` | LLM key for OpenHands | — |
| `LLM_MODEL` | Global model (from `.env`; base name or LiteLLM prefix) | `deepseek-v4-pro` |
| `GITHUB_TOKEN` | For cloning and creating PRs | — |
| `GIT_TERMINAL_PROMPT` | Disable interactive git prompts (prevents agent hangs) | `0` |
| `BIND_IP` | IP used to publish ports | `127.0.0.1` |
| `CLEANUP_WORKSPACE` | Remove the workspace after a workflow | `true` |
| `WAVE_MAX_CONCURRENCY` | DAG wave width | `4` |

## Structure

```
orchestrator/
  main.py        FastAPI Gateway
  worker.py      Temporal Worker
  workflows.py   EnterpriseHITLWorkflow (wave-based DAG executor)
  activities.py  execute_openhands_phase / prepare_workspace /
                 cleanup_workspace / git_push_and_create_pr
ui/
  app.py         Streamlit Dashboard
docker/
  openhands-entrypoint.sh
docker-compose.yml
examples/
  self-update.json            JSON plan "repository self-update"
  notification-service.json   JSON plan "Notification Service"
docs/
  openhands-setup.md          OpenHands and LLM provider setup
  openhands-setup.ru.md       Same in Russian
  examples/
    self-update.md            Self-update example walkthrough
    self-update.ru.md         Same in Russian
    notification-service.md   End-to-end tutorial (ChatGPT → DAG → run)
    notification-service.ru.md  Same in Russian
  blueprints/
    tree-refactoring-blueprint.md     Tree refactoring blueprint
    tree-refactoring-blueprint.ru.md  Same in Russian
  deployment/
    securing-with-npm-and-cloudflare.md     NPM + Cloudflare Access public deploy
    securing-with-npm-and-cloudflare.ru.md  Same in Russian
AGENTS.md                     JSON plan specification for an LLM architect
AGENTS.ru.md                  Same in Russian
README.md                     Main README (English)
README.ru.md                  Main README (Russian)
.env.example                  Environment variables template (copy to .env)
.gitignore                    Ignored files (secrets, caches, artifacts)
.dockerignore                 Docker build ignore rules
LICENSE                       MIT
```
