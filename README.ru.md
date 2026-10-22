 [English](README.md) | Русский

# agent-orchestrator-lite

Облегчённая публичная версия `agent-orchestrator`: только базовый контур
исполнения готового JSON-плана через OpenHands под управлением Temporal.

## Что внутри

- **FastAPI Gateway** (`orchestrator/main.py`) — `GET /health` и `POST /api/v1/plan/direct`.
- **Temporal Server + Postgres** (`temporal`, `temporal-db`) — оркестрация и история workflow.
- **OpenHands** (`openhands`) — агент, исполняющий фазы DAG-плана (terminal + file_editor).
- **Единый воркер** (`orchestrator/worker.py`) — регистрирует только
  `EnterpriseHITLWorkflow` и базовые активности.
- **Streamlit Dashboard** (`agent-ui`) — ввод JSON-плана, auto-approve и
  мониторинг/сигналы workflow.

## Что удалено относительно полной версии

- Воркеры и очереди `agent-aider`, `agent-aitm`, `agent-dogfood`, `agent-swarm`.
- Совет Старейшин (Council), `council_panel.json`,
  `scripts/multi_model_review_dispatcher.py` и все зависимости от них.
- LLM Planner (автодекомпозиция через DeepSeek) и интеграции
  OpenRouter / SiliconFlow / DashScope / Resend.
- Canvas fast-path (`canvas_claude_streamer.py`, `canvas_bridge.py`).
- Папка `scripts/` и лишние спецификации/blueprints.

## Быстрый старт

```bash
cd /home/kartemiev/projects/agent-orchestrator-lite
cp .env.example .env        # заполните LLM_API_KEY и GITHUB_TOKEN
docker compose up -d
```

После старта:

| Сервис | Адрес |
|---|---|
| Gateway API | http://127.0.0.1:8085 (health: `/health`) |
| Streamlit UI | http://127.0.0.1:8501 |
| Temporal UI | http://127.0.0.1:8233 |
| OpenHands | http://127.0.0.1:8000 |

Остановка: `docker compose down`.

## Запуск workflow

### Через REST API

```bash
curl -X POST http://127.0.0.1:8085/api/v1/plan/direct \
  -H 'Content-Type: application/json' \
  -d '{"raw_json": "{\"tasks\": [{\"id\": \"step_1\", \"title\": \"Hello\", \"prompt\": \"Создай файл hello.txt с текстом hi\", \"depends_on\": [], \"engine\": \"openhands\"}]}", "auto_approve": true}'
```

Ответ: `{"status": "triggered", "workflow_id": "direct-plan-...", ...}`.

### Через UI

1. Вставьте готовый JSON-план в поле «Готовый JSON-план».
2. Отметьте `Auto-approve` (или утвердите план вручную в панели мониторинга).
3. Нажмите «Запустить».

## Формат JSON-плана

```json
{
  "reasoning": "Необязательное обоснование",
  "tasks": [
    {
      "id": "step_1",
      "title": "Git Sync",
      "prompt": "Склонируй репозиторий https://github.com/owner/repo и подготовь окружение.",
      "depends_on": [],
      "model": "deepseek-v4-pro",
      "engine": "openhands"
    },
    {
      "id": "step_2",
      "title": "Создать модель",
      "prompt": "Создай Pydantic модель User в файле app/models.py",
      "depends_on": ["step_1"],
      "engine": "openhands"
    }
  ]
}
```

- `id` — уникальный идентификатор шага (если не задан, генерируется `task_N`).
- `depends_on` — список id шагов, от которых зависит шаг. Параллелизм задаётся
  только через `depends_on`: шаги с одинаковым набором зависимостей исполняются
  одной волной через `asyncio.gather`.
- `prompt` — инструкция для агента OpenHands.
- `engine` — в Lite-версии единственный движок `openhands` (все шаги, включая
  git-sync, выполняются OpenHands).
- `model` — опциональная per-node LLM-модель (иначе `LLM_MODEL` из `.env`). Имя может быть базовым (`deepseek-v4-pro`, `gpt-4o`) или с LiteLLM-префиксом (`openrouter/...`, `deepseek/...`).

Полная спецификация формата и правила построения DAG для LLM-архитектора —
в [`AGENTS.md`](./AGENTS.ru.md) (системный промпт для ChatGPT / Claude / DeepSeek).

Волновой DAG-исполнитель исполняет шаги строго по переданному плану: волна
готовых задач запускается параллельно, падение одной задачи изолируется
(зависимые шаги пропускаются), в конце — финальная доставка `git push` + PR
и очистка workspace.

## Примеры использования

📘 **[Сквозной туториал: разработка фичи с ChatGPT + Streamlit](docs/examples/notification-service.ru.md)** —
сценарий «от и до»: проектируем DAG-план через внешний LLM-чат и исполняем его
в оркестраторе (готовый план: [`notification-service.json`](./examples/notification-service.json)).

### Самообновление репозитория

Готовый пример «оркестратор обновляет сам себя» находится в
[`self-update.json`](./examples/self-update.json), а его подробное описание —
в [`self-update.md`](./docs/examples/self-update.ru.md).

Короткий запуск через cURL (план берётся из файла напрямую):

```bash
curl -X POST http://localhost:8085/api/v1/plan/direct \
  -H "Content-Type: application/json" \
  -d @examples/self-update.json
```

То же самое с Auto-approve (без ручного подтверждения):

```bash
curl -X POST http://localhost:8085/api/v1/plan/direct \
  -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json; d=json.load(open("examples/self-update.json")); print(json.dumps({"raw_json": json.dumps(d), "auto_approve": True}))')"
```

План содержит 5 задач в 4 волнах: `git-sync` → параллельные
`backend/docs` + `ui` → `tests` → `deliver` (commit + push + PR).

## Переменные окружения

См. [`.env.example`](./.env.example). Подробная настройка LLM-провайдеров
(OpenAI / OpenRouter / DeepSeek) и контейнера OpenHands — в
[`openhands-setup.md`](./docs/openhands-setup.ru.md). Ключевые:

| Переменная | Описание | По умолчанию |
|---|---|---|
| `LLM_API_KEY` | Ключ LLM для OpenHands | — |
| `LLM_MODEL` | Глобальная модель (из `.env`; базовое имя или LiteLLM-префикс) | `deepseek-v4-pro` |
| `GITHUB_TOKEN` | Для клонирования и создания PR | — |
| `BIND_IP` | IP публикации портов | `127.0.0.1` |
| `CLEANUP_WORKSPACE` | Удалять workspace после workflow | `true` |
| `WAVE_MAX_CONCURRENCY` | Ширина волны DAG | `4` |

## Структура

```
orchestrator/
  main.py        FastAPI Gateway
  worker.py      Temporal Worker
  workflows.py   EnterpriseHITLWorkflow (волновой DAG-исполнитель)
  activities.py  execute_openhands_phase / prepare_workspace /
                 cleanup_workspace / git_push_and_create_pr
ui/
  app.py         Streamlit Dashboard
docker/
  openhands-entrypoint.sh
docker-compose.yml
examples/
  self-update.json            JSON-план «самообновление репозитория»
  notification-service.json   JSON-план «Notification Service»
docs/
  openhands-setup.md          Настройка OpenHands и LLM-провайдеров
  openhands-setup.ru.md       То же на русском
  examples/
    self-update.md            Описание примера самообновления
    self-update.ru.md         То же на русском
    notification-service.md   Сквозной туториал (ChatGPT → DAG → запуск)
    notification-service.ru.md  То же на русском
  blueprints/
    tree-refactoring-blueprint.md     Blueprint рефакторинга дерева
    tree-refactoring-blueprint.ru.md  То же на русском
AGENTS.md                     Спецификация JSON-планов для LLM-архитектора
AGENTS.ru.md                  То же на русском
README.md                     Главный README (English)
README.ru.md                  Главный README (Русский)
LICENSE                       MIT
```
