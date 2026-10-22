 English | [Русский](openhands-setup.ru.md)

# OpenHands and LLM provider setup

A detailed guide to configuring OpenHands (agent-canvas) for work in
`agent-orchestrator-lite`: how the orchestrator passes the LLM configuration into
the `openhands` container, which environment variables are required, and how the
shared workspace is arranged.

---

## Concept

In `agent-orchestrator-lite` the `openhands` container (image
`ghcr.io/openhands/agent-canvas:1.10.0`) is a **phase executor**, not a planner.
It knows nothing about the DAG plan: it is given the ready-made `prompt` of each
task.

The LLM configuration is passed to OpenHands in two ways:

1. **Through environment variables** (in `docker-compose.yml`) — the
   `agent-orchestrator` container receives `LLM_API_KEY`, `LLM_API_URL`, `LLM_MODEL` and
   `GITHUB_TOKEN` from `.env`.
2. **Through the API request that creates a conversation** — the
   `execute_openhands_phase` activity in `orchestrator/activities.py` sends a
   payload to `POST /api/conversations` with `agent.llm = {model, api_key, base_url}`.
   It is this payload that determines which model and which provider are used
   for a specific phase.

The orchestrator is thus the **single LLM configuration point**: it reads the
environment variables and passes them to OpenHands for every conversation. This
makes it possible to set a per-node model directly in the JSON plan (the `model`
field) and the global model through `LLM_MODEL`.

---

## LLM configuration in `.env`

Key variables (read by `agent-orchestrator` and passed to OpenHands):

| Variable | Purpose |
|---|---|
| `LLM_API_KEY` | Access key for the LLM provider. |
| `LLM_API_URL` | Provider base endpoint. Any OpenAI-compatible API is supported. |
| `LLM_MODEL` | Global default model (read from .env; base name or LiteLLM prefix). |
| `GITHUB_TOKEN` | GitHub token for clone/pull/push/PR from the `openhands` container. |

### Supported providers

The orchestrator passes `base_url` to OpenHands as is (normalized to `/v1`),
so any OpenAI-compatible gateway will do. The model name is set through
`LLM_MODEL` in `.env` and forwarded unchanged; both a provider base name and a
LiteLLM prefix are acceptable.

**DeepSeek** (the primary reference environment)

```dotenv
LLM_API_KEY=sk-...
LLM_API_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-pro
```

**OpenAI**

```dotenv
LLM_API_KEY=sk-...
LLM_API_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o
```

**OpenRouter**

```dotenv
LLM_API_KEY=sk-or-v1-...
LLM_API_URL=https://openrouter.ai/api/v1
LLM_MODEL=openrouter/openai/gpt-4o-mini
```

> The model name (`LLM_MODEL` and the per-node `model`) is taken from `.env` and
> passed to OpenHands unchanged. Both forms are allowed: a base name
> (`deepseek-v4-pro`, `gpt-4o`, `claude-3-5-sonnet`) and a LiteLLM prefix
> (`openrouter/...`, `deepseek/...`). The only thing that matters is that the name
> matches the selected `LLM_API_URL`.

## Passing variables in `docker-compose.yml`

### The `agent-orchestrator` section (the LLM configuration point)

```yaml
  agent-orchestrator:
    environment:
      - TEMPORAL_HOST=temporal:7233
      - OPENHANDS_API_URL=http://openhands:8000
      - OPENHANDS_API_KEY=${OPENHANDS_SESSION_API_KEY:-dev}
      - GITHUB_TOKEN=${GITHUB_TOKEN}
      - LLM_API_URL=${LLM_API_URL:-https://api.deepseek.com}
      - LLM_API_KEY=${LLM_API_KEY}
      - LLM_MODEL=${LLM_MODEL:-deepseek-v4-pro}
      - CLEANUP_WORKSPACE=${CLEANUP_WORKSPACE:-true}
      - WAVE_MAX_CONCURRENCY=${WAVE_MAX_CONCURRENCY:-4}
```

Here `LLM_API_KEY` / `LLM_API_URL` / `LLM_MODEL` land in the `agent-orchestrator`
process, from where the `execute_openhands_phase` activity takes them for every
OpenHands conversation.

### The `openhands` section

```yaml
  openhands:
    image: ghcr.io/openhands/agent-canvas:1.10.0
    entrypoint:
      - /bin/bash
      - /usr/local/bin/openhands-entrypoint.sh
    ports:
      - "${BIND_IP:-127.0.0.1}:8000:8000"
    environment:
      - WORKSPACE_BASE=/app/workspace
      - WORKSPACE_DIR=/app/workspace
      - SERVE_WITHOUT_AUTH=true
      - DISABLE_AUTH=true
      - AGENT_CANVAS_BASE_PATH=/canvas
      - AGENT_CANVAS_API_PREFIX=/api
      - LOCAL_BACKEND_API_KEY=${OPENHANDS_SESSION_API_KEY:-dev}
      - LLM_API_KEY=${LLM_API_KEY}
      - LLM_API_URL=${LLM_API_URL:-https://api.deepseek.com}
      - LLM_MODEL=${LLM_MODEL:-deepseek-v4-pro}
      - GITHUB_TOKEN=${GITHUB_TOKEN}
      - GIT_CONFIG_COUNT=1
      - GIT_CONFIG_KEY_0=safe.directory
      - GIT_CONFIG_VALUE_0=*
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /etc/openhands/config:/home/openhands/.openhands
      - ./workspace:/app/workspace
      - ./docker/openhands-entrypoint.sh:/usr/local/bin/openhands-entrypoint.sh:ro
```

Explanation of the key variables of the `openhands` container:

| Variable | Purpose |
|---|---|
| `LOCAL_BACKEND_API_KEY` | Pins the `X-Session-API-Key` of the OpenHands service (otherwise it is random on every start). |
| `LLM_API_KEY` | Fallback LLM key inside OpenHands (the primary key is passed in the conversation payload). |
| `LLM_API_URL` | LLM base endpoint (single source of truth from `.env`). |
| `LLM_MODEL` | Global default model (single source of truth from `.env`). |
| `GITHUB_TOKEN` | Needed by the OpenHands agent in `terminal` for `git clone/pull/push` of private repositories. |
| `GIT_CONFIG_*` | Allows `git` to work in the mounted workspace ("dubious ownership"). |

### How the configuration reaches the model (runtime)

The `execute_openhands_phase` activity builds the conversation payload:

```python
"agent": {
    "kind": "Agent",
    "llm": {
        "model": phase_data.get("model") or LLM_MODEL,
        "api_key": LLM_API_KEY,
        "base_url": _openhands_llm_base_url(),  # LLM_API_URL → .../v1
    },
    "tools": [
        {"name": "terminal", "params": {}},
        {"name": "file_editor", "params": {}},
    ],
}
```

- `phase_data.get("model")` — the per-node model from the JSON plan; if it is
  absent, the global `LLM_MODEL` is used (in code — `os.getenv("LLM_MODEL",
  "deepseek-v4-pro")`, `orchestrator/activities.py`).
- `_openhands_llm_base_url()` normalizes `LLM_API_URL` to `/v1`
  (`https://api.deepseek.com` → `https://api.deepseek.com/v1`).

## Access rights and Volumes

The shared workspace is mounted into several containers (`agent-orchestrator` and
`openhands`), but each container runs under its own UID:

| Container | UID |
|---|---|
| `agent-orchestrator` | `root` (0) |
| `openhands` | `10001` |
| Host | `1000` |

To make files created by one container available to the others, the following
combination is used:

- **`./workspace:/app/workspace`** — the shared bind-mount of the working directory;
- **`docker/openhands-entrypoint.sh`** — an entrypoint wrapper that, on start of
  the `openhands` container, runs:
  ```bash
  umask 000
  chmod -R a+rwX /app/workspace
  mkdir -p /app/workspace/runs
  chmod 0777 /app/workspace/runs
  ```
  This guarantees that new files are created world-readable/writable (666/777)
  and that the `runs/` subdirectories stay accessible to the OpenHands process
  (UID 10001) even after the worker removes the directory.

In addition, the `agent-orchestrator` container is started with `umask 000`
(`command: sh -c "umask 000; ..."`), and `orchestrator/worker.py` calls
`ensure_runs_dir_writable()` on startup to set `0777` on `runs/`.

### The role of `GIT_CONFIG_*`

OpenHands runs as UID 10001, while the mounted workspace belongs to the host
user (UID 1000). Without
`GIT_CONFIG_KEY_0=safe.directory` / `GIT_CONFIG_VALUE_0=*` git inside OpenHands
would fail with "dubious ownership" on `git diff` / `git log` / `git add`.

## OpenHands API authentication

The orchestrator calls OpenHands with the `X-Session-API-Key` header, whose value
comes from `OPENHANDS_API_KEY` (in docker-compose —
`${OPENHANDS_SESSION_API_KEY:-dev}`). This value must match the
`LOCAL_BACKEND_API_KEY` of the `openhands` container, otherwise the API returns `401`.

```dotenv
OPENHANDS_SESSION_API_KEY=dev
```

In demo mode (`DISABLE_AUTH=true`) authentication is disabled, and the default
value `dev` works. For a real environment, set your own secret and pass it into
both variables.

## Verifying the setup

1. Make sure that `LLM_API_KEY`, `LLM_API_URL`, `LLM_MODEL`
   and `GITHUB_TOKEN` are set in `.env`.
2. Bring the stack up:
   ```bash
   docker compose up -d
   ```
3. Check the orchestrator healthcheck:
   ```bash
   curl http://127.0.0.1:8085/health
   ```
   → `{"status": "ok"}`
4. Run a minimal plan and inspect the phase logs in `docker compose logs
   agent-orchestrator` (the line `OpenHands conversation created: ...` means
   that the conversation with the selected model was created successfully).
