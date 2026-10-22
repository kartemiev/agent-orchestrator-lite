 English | [Русский](README.ru.md)

# agent-orchestrator-lite

A lightweight public version of `agent-orchestrator`: only the core contour for
executing a pre-built JSON plan through OpenHands under Temporal control.

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
cd /home/kartemiev/projects/agent-orchestrator-lite
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
(OpenAI / OpenRouter / DeepSeek) and of the OpenHands container is described in
[`openhands-setup.md`](./docs/openhands-setup.md). The key ones:

| Variable | Description | Default |
|---|---|---|
| `LLM_API_KEY` | LLM key for OpenHands | — |
| `LLM_MODEL` | Global model (from `.env`; base name or LiteLLM prefix) | `deepseek-v4-pro` |
| `GITHUB_TOKEN` | For cloning and creating PRs | — |
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
AGENTS.md                     JSON plan specification for an LLM architect
AGENTS.ru.md                  Same in Russian
README.md                     Main README (English)
README.ru.md                  Main README (Russian)
LICENSE                       MIT
```
