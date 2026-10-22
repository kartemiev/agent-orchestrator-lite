 English | [Русский](AGENTS.ru.md)

# AGENTS.md — JSON plan specification for agent-orchestrator-lite

> **Purpose:** this document is a system instruction (system prompt) for
> external LLMs (ChatGPT, Claude, DeepSeek, etc.). Attach it to a feature-design
> request so that the model returns a strictly valid `ExecutionPlan` that can be
> run in `agent-orchestrator-lite`.

## What agent-orchestrator-lite is

`agent-orchestrator-lite` executes DAG plans (the `ExecutionPlan` schema) in
isolated workspaces through OpenHands under Temporal control. One workflow
= one JSON plan; the tasks of the plan are executed in waves via `asyncio.gather`,
where parallelism is defined solely by the `depends_on` field.

---

## DAG construction rules

1. **Mandatory first wave (Wave 1) — git-sync.**
   The very first task MUST ALWAYS be a task that clones/prepares the
   git repository, with `"id": "task_0_git_sync"` and `"depends_on": []`.
   Its `prompt` must contain clone/update markers
   (`git clone`, `git pull`, "clone", "pull", "update the repository") —
   otherwise the phase fails with `EmptyWorkspaceError` on an empty workspace.

2. **`depends_on` — strictly a DAG, no cycles.**
   Every subsequent task must reference the `id` of tasks that actually exist.
   The graph must be acyclic: a cycle or a reference to a non-existent `id`
   terminates the workflow with a `RuntimeError`.

3. **Parallelism is defined only by `depends_on`, not by `priority`.**
   Tasks with an identical `depends_on` array are executed in one wave in
   parallel via `asyncio.gather`. Do not build an artificial "ladder"
   (`step_1 → step_2 → step_3`) when the tasks are physically independent —
   group them into a single level.

4. **The `engine` executor is strictly `"openhands"`.**
   The only engine is OpenHands (terminal + file_editor).
   Every node must contain `"engine": "openhands"`.

5. **The final wave — verification and/or delivery.**
   The last wave must contain a verification step (tests/linter) and/or a step
   that commits + opens a Pull Request. In addition to an explicit delivery step,
   the orchestrator also calls the built-in `git_push_and_create_pr` activity
   (a safety net against losing uncommitted/unpushed changes).

6. **Constraints for OpenHands prompts.**
   - Every workflow runs in an isolated workspace
     `/app/workspace/runs/{workflow_id}`; modify files only inside it.
   - Destructive git commands are forbidden: `git checkout`, `git restore`,
     `git reset`, `git stash`, `git clean`. To publish a branch use
     `git push origin HEAD:refs/heads/<branch>` instead of switching branches.
   - Tasks that modify existing files require a populated workspace
     (see rule #1). Tasks that create files from scratch are allowed even in an
     empty workspace — but starting with git-sync is safer anyway.
   - File paths in `prompt` are real repository-relative paths, not
     abstract names.

---

## JSON schema (`ExecutionPlan`)

Top-level object:

| Field | Type | Required | Description |
|---|---|---|---|
| `reasoning` | string | no | Rationale for the architectural decision/decomposition. |
| `tasks` | list of object | yes | List of DAG tasks in dependency order. |

> Optional: `wave_max_concurrency` (int) — wave width (how many tasks start in
> parallel). Defaults to `WAVE_MAX_CONCURRENCY` (4).

### Task fields (`tasks[i]`)

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Unique identifier (`task_0_git_sync`, `task_a_...`). |
| `title` | string | yes | Short task title. |
| `prompt` | string | yes | Detailed instruction for OpenHands: what to do, which files to create/modify, how to verify the result. |
| `depends_on` | list of string | yes | Array of task `id`s the task depends on. For the first task — `[]`. |
| `engine` | string | yes | Strictly `"openhands"`. |
| `priority` | int (1..5) | no | Task priority. Does NOT affect order/parallelism (parallelism is defined only by `depends_on`). |
| `model` | string | no | Per-node LLM model. If omitted — `LLM_MODEL` from `.env` is used. The name can be a provider base name (`deepseek-v4-pro`, `gpt-4o`) or a LiteLLM prefix (`openrouter/...`, `deepseek/...`). |

## Reference example (2 tasks: git-sync → feature)

```json
{
  "reasoning": "Minimal plan: clone the repository and add a file.",
  "tasks": [
    {
      "id": "task_0_git_sync",
      "title": "Git Sync: clone the repository",
      "prompt": "Clone or update (git pull) the repository https://github.com/your-org/your-web-app into the current working directory and prepare the working environment.",
      "priority": 1,
      "depends_on": [],
      "model": "deepseek-v4-flash",
      "engine": "openhands"
    },
    {
      "id": "task_a_feature",
      "title": "Add a feature",
      "prompt": "Create a README.md file with a brief project description and add a GET /ping endpoint to app/main.py that returns {\"status\": \"ok\"}.",
      "priority": 2,
      "depends_on": ["task_0_git_sync"],
      "model": "deepseek-v4-pro",
      "engine": "openhands"
    }
  ]
}
```

---

## How to use

1. Copy the text below into a chat with an LLM together with the project files
   (`ARCHITECTURE.md`, `API_SPEC.md`, `DATABASE.md`).

```text
Design the feature "<describe the feature>" following the rules in the attached AGENTS.md.
Return only a valid ExecutionPlan JSON object (no markdown fences and no
explanations outside the JSON): with the mandatory git-sync first task, an acyclic
depends_on, and a split into parallel waves.
```

2. Paste the resulting JSON into the Streamlit UI (`http://127.0.0.1:${STREAMLIT_PORT:-8501}`) or send
   it to `POST /api/v1/plan/direct` (see [`README.md`](./README.md)).

```bash
curl -X POST http://127.0.0.1:${ORCHESTRATOR_GATEWAY_PORT:-8085}/api/v1/plan/direct \
  -H "Content-Type: application/json" \
  -d @examples/notification-service.json
```

---

## CLI Diagnostics & Control Tool (`scripts/agent-cli.py`)

> For AI agents and maintainers working **on this repository** (distinct from the
> external LLMs that only design plans): before editing code or JSON plans, and
> whenever a sprint fails, use the bundled CLI to inspect the live system.

`scripts/agent-cli.py` runs both from the host and inside the orchestrator
container. From the host, Temporal-heavy commands re-run themselves inside the
container via `docker exec`; `logs` reads `docker logs` directly.

| Command | Purpose |
|---|---|
| `python3 scripts/agent-cli.py list --since 24h --limit 20` | List sprints (workflows) for the last 24h — statuses `RUNNING` / `COMPLETED` / `FAILED` / `TERMINATED`. |
| `python3 scripts/agent-cli.py status <workflow_id>` | Brief status of the waves and tasks of one sprint. |
| `python3 scripts/agent-cli.py inspect <workflow_id>` | Deep inspection: DAG steps → OpenHands sessions, container states, last `AgentErrorEvent`s. |
| `python3 scripts/agent-cli.py logs <workflow_id> [--phase <task_id>]` | Targeted orchestrator + OpenHands logs for a workflow / phase. |
| `python3 scripts/agent-cli.py diagnose <workflow_id>` | Self-diagnose a failed sprint: collects the full error dump (Temporal traceback + OpenHands events) and asks DeepSeek for a short root-cause + fix. |
| `python3 scripts/agent-cli.py submit <plan.json> [--auto-approve] [-w]` | Launch a sprint from a JSON plan. |

**Rules for agents:**

1. **Check status first.** Before changing code or JSON plans, run
   `python3 scripts/agent-cli.py list --since 24h` (or `status <workflow_id>`)
   to see what is already running or failing — do not blindly re-submit.
2. **Diagnose failures, don't guess.** For a failed sprint, run
   `python3 scripts/agent-cli.py diagnose <workflow_id>` and read the root cause
   and suggested fix before editing.
3. **Read logs before editing.** Run
   `python3 scripts/agent-cli.py logs <workflow_id> [--phase <task_id>]` (and
   `inspect <workflow_id>` for the DAG→session/container mapping) to locate the
   exact failing step before touching files.
4. **Never commit secrets.** The CLI reads `.env` (LLM keys, `GITHUB_TOKEN`) from
   the environment; do not print or commit them.
5. **Never hardcode service URIs.** Service ports (Gateway, Temporal, Agent
   Canvas/OpenHands, Streamlit) may be non-standard and are set via `.env`
   (`ORCHESTRATOR_GATEWAY_PORT`, `TEMPORAL_PORT`, `AGENT_CANVAS_PORT`,
   `STREAMLIT_PORT`). Read the ports from `.env` or use `scripts/agent-cli.py`
   (which resolves them automatically) instead of hardcoding URIs.
