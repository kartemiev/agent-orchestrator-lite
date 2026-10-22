 [English](openhands-setup.md) | Русский

# Настройка OpenHands и LLM-провайдеров

Детальная инструкция по конфигурации OpenHands (agent-canvas) для работы в
`agent-orchestrator-lite`: как оркестратор передаёт LLM-конфигурацию в контейнер
`openhands`, какие переменные окружения нужны, и как устроен общий workspace.

---

## Концепция

В `agent-orchestrator-lite` контейнер `openhands` (образ
`ghcr.io/openhands/agent-canvas:1.10.0`) — это **исполнитель фаз**, а не
планировщик. Он не знает о DAG-плане: ему отдаётся уже готовый `prompt` каждой
задачи.

Конфигурация LLM передаётся в OpenHands двумя путями:

1. **Через переменные окружения** (в `docker-compose.yml`) — контейнер
   `agent-orchestrator` получает `LLM_API_KEY`, `LLM_API_URL`, `LLM_MODEL` и
   `GITHUB_TOKEN` из `.env`.
2. **Через API-запрос при создании разговора** — активность
   `execute_openhands_phase` в `orchestrator/activities.py` отправляет на
   `POST /api/conversations` payload с `agent.llm = {model, api_key, base_url}`.
   Именно этот payload определяет, какая модель и какой провайдер используются
   для конкретной фазы.

Таким образом, **оркестратор является единой точкой конфигурации LLM**: он
читает переменные окружения и прокидывает их в OpenHands на каждый разговор.
Это позволяет задавать per-node модель прямо в JSON-плане (поле `model`), а
глобальную модель — через `LLM_MODEL`.

---

## Конфигурация LLM в `.env`

Ключевые переменные (читаются `agent-orchestrator` и прокидываются в OpenHands):

| Переменная | Назначение |
|---|---|
| `LLM_API_KEY` | Ключ доступа к LLM-провайдеру. |
| `LLM_API_URL` | Базовый эндпоинт провайдера. Поддерживается любой OpenAI-совместимый API. |
| `LLM_MODEL` | Глобальная модель по умолчанию (читается из .env; базовое имя или LiteLLM-префикс). |
| `GITHUB_TOKEN` | Токен GitHub для clone/pull/push/PR из контейнера `openhands`. |

### Поддерживаемые провайдеры

Оркестратор передаёт `base_url` в OpenHands как есть (с приведением к `/v1`),
поэтому подходит любой OpenAI-совместимый шлюз. Имя модели задаётся через
`LLM_MODEL` в `.env` и пробрасывается без изменений; допустимо как базовое имя
провайдера, так и LiteLLM-префикс.

**DeepSeek** (основной пример рабочей среды)

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

> Имя модели (`LLM_MODEL` и per-node `model`) берётся из `.env` и передаётся в
> OpenHands без изменений. Допустимы оба варианта: базовое имя
> (`deepseek-v4-pro`, `gpt-4o`, `claude-3-5-sonnet`) и LiteLLM-префикс
> (`openrouter/...`, `deepseek/...`). Главное — чтобы имя соответствовало
> выбранному `LLM_API_URL`.

## Проброс переменных в `docker-compose.yml`

### Секция `agent-orchestrator` (точка конфигурации LLM)

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

Здесь `LLM_API_KEY` / `LLM_API_URL` / `LLM_MODEL` попадают в процесс
`agent-orchestrator`, откуда активность `execute_openhands_phase` забирает их
для каждого разговора OpenHands.

### Секция `openhands`

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

Пояснение ключевых переменных контейнера `openhands`:

| Переменная | Назначение |
|---|---|
| `LOCAL_BACKEND_API_KEY` | Фиксирует `X-Session-API-Key` сервиса OpenHands (иначе он рандомен при каждом старте). |
| `LLM_API_KEY` | Fallback-ключ LLM внутри OpenHands (основной ключ передаётся в payload разговора). |
| `LLM_API_URL` | Базовый эндпоинт LLM (единый источник правды из `.env`). |
| `LLM_MODEL` | Глобальная модель по умолчанию (единый источник правды из `.env`). |
| `GITHUB_TOKEN` | Нужен агенту OpenHands в `terminal` для `git clone/pull/push` приватных репозиториев. |
| `GIT_CONFIG_*` | Разрешает `git` работать в смонтированном workspace («dubious ownership»). |

### Как конфигурация доходит до модели (runtime)

Активность `execute_openhands_phase` формирует payload разговора:

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

- `phase_data.get("model")` — per-node модель из JSON-плана; если её нет,
  берётся глобальная `LLM_MODEL` (в коде — `os.getenv("LLM_MODEL",
  "deepseek-v4-pro")`, `orchestrator/activities.py`).
- `_openhands_llm_base_url()` приводит `LLM_API_URL` к `/v1`
  (`https://api.deepseek.com` → `https://api.deepseek.com/v1`).

## Права доступа и Volumes

Общий workspace монтируется в несколько контейнеров (`agent-orchestrator` и
`openhands`), но каждый контейнер работает под своим UID:

| Контейнер | UID |
|---|---|
| `agent-orchestrator` | `root` (0) |
| `openhands` | `10001` |
| Хост | `1000` |

Чтобы файлы, созданные одним контейнером, были доступны другим, используется
связка:

- **`./workspace:/app/workspace`** — общий bind-mount рабочего каталога;
- **`docker/openhands-entrypoint.sh`** — обёртка entrypoint, которая при старте
  контейнера `openhands` выполняет:
  ```bash
  umask 000
  chmod -R a+rwX /app/workspace
  mkdir -p /app/workspace/runs
  chmod 0777 /app/workspace/runs
  ```
  Это гарантирует, что новые файлы создаются world-readable/writable (666/777),
  а подкаталоги `runs/` доступны процессу OpenHands (UID 10001) даже после
  удаления каталога воркером.

Кроме того, контейнер `agent-orchestrator` запускается с `umask 000`
(`command: sh -c "umask 000; ..."`), а `orchestrator/worker.py` при старте
вызывает `ensure_runs_dir_writable()` для установки `0777` на `runs/`.

### Роль `GIT_CONFIG_*`

OpenHands работает под UID 10001, а смонтированный workspace принадлежит
пользователю хоста (UID 1000). Без
`GIT_CONFIG_KEY_0=safe.directory` / `GIT_CONFIG_VALUE_0=*` git внутри OpenHands
падал бы с «dubious ownership» при `git diff` / `git log` / `git add`.

## Аутентификация OpenHands API

Оркестратор обращается к OpenHands с заголовком `X-Session-API-Key`, значение
которого берётся из `OPENHANDS_API_KEY` (в docker-compose —
`${OPENHANDS_SESSION_API_KEY:-dev}`). Это значение должно совпадать с
`LOCAL_BACKEND_API_KEY` контейнера `openhands`, иначе API вернёт `401`.

```dotenv
OPENHANDS_SESSION_API_KEY=dev
```

В демо-режиме (`DISABLE_AUTH=true`) аутентификация отключена, и значение по
умолчанию `dev` подходит. Для реального окружения задайте свой секрет и
передайте его в обе переменные.

## Проверка настройки

1. Убедитесь, что в `.env` заданы `LLM_API_KEY`, `LLM_API_URL`, `LLM_MODEL`
   и `GITHUB_TOKEN`.
2. Поднимите стек:
   ```bash
   docker compose up -d
   ```
3. Проверьте healthcheck оркестратора:
   ```bash
   curl http://127.0.0.1:8085/health
   ```
   → `{"status": "ok"}`
4. Запустите минимальный план и проверьте логи фазы в `docker compose logs
   agent-orchestrator` (строка `OpenHands conversation created: ...` означает,
   что разговор с выбранной моделью создан успешно).


