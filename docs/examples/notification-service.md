# Сквозной туториал: разработка фичи с ChatGPT + agent-orchestrator-lite

Полный сценарий «от и до»: как с помощью внешнего LLM-чата (ChatGPT / Claude)
спроектировать DAG-план для фичи и выполнить его в `agent-orchestrator-lite`
через OpenHands + Temporal.

Готовый JSON-план из этого сценария лежит в файле
[`notification-service.json`](../../examples/notification-service.json) — его можно запустить
напрямую.

---

## Сценарий: система уведомлений (Notification Service)

Добавляем в абстрактное Web-приложение сервис уведомлений по **WebSocket** и
**Email**. Фича затрагивает три слоя:

- **БД** — новая таблица `notifications`;
- **Сервисный слой** — модуль отправки Email;
- **API/Transport** — WebSocket-эндпоинт + интеграция Email и БД.

---

## Шаг 1. Подготовка документации проекта

Разработчик берёт четыре файла из своего проекта и из оркестратора:

| Файл | Что содержит |
|---|---|
| `ARCHITECTURE.md` | Архитектура приложения: слои, модули, точки расширения. |
| `API_SPEC.md` | Спецификация REST API (OpenAPI) и существующие эндпоинты. |
| `DATABASE.md` | Схема БД: модели SQLAlchemy / Prisma, миграции. |
| `AGENTS.md` | Правила построения DAG-графов для оркестратора (волны, `depends_on`, изоляция workspaces, ограничения OpenHands). |

> В `agent-orchestrator-lite` правила построения DAG-графов описаны в
> [`README.md`](../../README.md) → раздел «Формат JSON-плана» и в
> [`self-update.md`](self-update.md) (в полной версии проекта они живут
> в `AGENTS.md`). Именно их нужно приложить к запросу, чтобы LLM сгенерировал
> план в правильном формате.

---

## Шаг 2. Запрос в ChatGPT / Claude

Копируем промпт ниже вместе с прикреплёнными `ARCHITECTURE.md`, `API_SPEC.md`,
`DATABASE.md` и правилами DAG-графов:

```text
Я хочу добавить в проект сервис уведомлений по WebSocket и Email.

Изучи прикреплённые ARCHITECTURE.md, API_SPEC.md, DATABASE.md и правила
построения DAG-графов из AGENTS.md.

Спроектируй фичу и выдай готовый ExecutionPlan в формате ВАЛИДНОГО JSON
(без markdown-фенсов), пригодный для отправки в agent-orchestrator-lite.

Требования к плану:
1. Первый шаг — git-sync: клонирование/обновление репозитория
   https://github.com/your-org/your-web-app (depends_on: []).
2. Разбей работу на 4 волны:
   - Wave 1 (Init): клонирование репозитория.
   - Wave 2 (параллельно):
       * Задача А — миграция БД + модель Notification;
       * Задача Б — выделенный модуль Email-отправки.
   - Wave 3 (Integration & API): WebSocket-эндпоинт /ws/notifications и
     интеграция с Email + БД.
   - Wave 4 (Tests & Delivery): pytest-тесты, проверка сборки, commit,
     push в ветку feature/notification-service и создание Pull Request.
3. depends_on — строго по DAG: только на реально существующие id, без циклов.
   Задачи с одинаковым набором depends_on исполняются одной волной (параллельно).
4. Все шаги используют engine: "openhands".
5. Верни только JSON-объект с ключами "graph_name", "reasoning" и "tasks".
```

---

## Шаг 3. Ответ ChatGPT — готовый JSON-план

Реалистичный ответ модели (тот же JSON, что и в
[`notification-service.json`](../../examples/notification-service.json)):

```json
{
  "graph_name": "Notification Service: WebSocket + Email",
  "reasoning": "Добавляем сервис уведомлений: миграция БД и модель Notification, выделенный модуль Email-отправки, WebSocket-эндпоинт с интеграцией, pytest-тесты и доставка через Pull Request.",
  "tasks": [
    {
      "id": "task_0_git_sync",
      "title": "Git Sync: clone репозиторий",
      "prompt": "Склонируй или обнови (git pull) репозиторий https://github.com/your-org/your-web-app в текущий рабочий каталог и подготовь рабочее окружение.",
      "priority": 1,
      "depends_on": [],
      "model": "deepseek-v4-flash",
      "engine": "openhands"
    },
    {
      "id": "task_a_db_migration",
      "title": "DB: миграция и модель Notification",
      "prompt": "Создай миграцию базы данных и модель Notification (id, user_id, type, payload, read, created_at). Используй существующий ORM проекта (SQLAlchemy/Prisma) и обнови DATABASE.md описанием новой таблицы.",
      "priority": 2,
      "depends_on": ["task_0_git_sync"],
      "model": "deepseek-v4-pro",
      "engine": "openhands"
    },
    {
      "id": "task_b_email_module",
      "title": "Email: выделенный модуль отправки",
      "prompt": "Создай выделенный модуль email-отправки: функция send_email(to, subject, body) через SMTP/API-провайдер, конфигурация через переменные окружения и простые шаблоны писем. Обнови ARCHITECTURE.md описанием модуля.",
      "priority": 2,
      "depends_on": ["task_0_git_sync"],
      "model": "deepseek-v4-pro",
      "engine": "openhands"
    },
    {
      "id": "task_2_ws_integration",
      "title": "API: WebSocket-эндпоинт и интеграция",
      "prompt": "Добавь WebSocket-эндпоинт /ws/notifications, доставляющий уведомления подписанным клиентам. Интегрируй его с моделью Notification и модулем Email: при создании уведомления одновременно отправляй WebSocket-сообщение и Email. Обнови API_SPEC.md описанием нового эндпоинта.",
      "priority": 3,
      "depends_on": ["task_a_db_migration", "task_b_email_module"],
      "model": "deepseek-v4-pro",
      "engine": "openhands"
    },
    {
      "id": "task_3_tests_and_deliver",
      "title": "Tests & Delivery",
      "prompt": "Напиши pytest-тесты для модели Notification, модуля Email и WebSocket-эндпоинта. Запусти python -m py_compile по изменённым файлам и pytest; исправь найденные проблемы. Затем закоммить изменения понятным сообщением, запушь ветку feature/notification-service и создай Pull Request в main с описанием фичи.",
      "priority": 4,
      "depends_on": ["task_2_ws_integration"],
      "model": "deepseek-v4-pro",
      "engine": "openhands"
    }
  ]
}
```

### Как читать этот DAG

```
Wave 1  (Init)             task_0_git_sync
                                 │
                ┌────────────────┴────────────────┐
Wave 2  (Dev)   task_a_db_migration        task_b_email_module
                └────────────────┬────────────────┘
Wave 3  (API)                task_2_ws_integration
                                 │
Wave 4  (Tests+Delivery)   task_3_tests_and_deliver
```

- `task_0_git_sync` выполняется первым и в одиночку — без клона правки упали бы
  на пустом workspace.
- `task_a_db_migration` и `task_b_email_module` не зависят друг от друга →
  запускаются **параллельно** одной волной через `asyncio.gather`.
- `task_2_ws_integration` зависит от обеих задач Wave 2 → ждёт их завершения.
- `task_3_tests_and_deliver` стартует только после успешной интеграции.

---

## Шаг 4. Запуск в agent-orchestrator-lite

### Вариант 1 — Streamlit UI

1. Открой Streamlit Dashboard: <http://localhost:8501>.
2. Вставь полученный из ChatGPT JSON в поле «**Готовый JSON-план**»
   (или скопируй содержимое [`notification-service.json`](../../examples/notification-service.json)).
3. Отметь чекбокс «**⚡ Auto-approve**», чтобы пропустить ручное подтверждение
   (либо оставь выключенным — тогда план сначала нужно будет утвердить вручную
   в панели мониторинга).
4. Нажми «**⚡ Запустить с готовым JSON-планом**».
5. Наблюдай за выполнением волн DAG в панели «Мониторинг и управление workflow»:
   введи появившийся `workflow_id` и следи за статусом Temporal Workflow.
6. По завершении получи ссылку на готовый Pull Request на GitHub
   (ветка `feature/notification-service` → `main`).

### Вариант 2 — cURL / REST API

```bash
curl -X POST http://localhost:8085/api/v1/plan/direct \
  -H "Content-Type: application/json" \
  -d @examples/notification-service.json
```

Сразу без ручного подтверждения:

```bash
curl -X POST http://localhost:8085/api/v1/plan/direct \
  -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json; d=json.load(open("examples/notification-service.json")); print(json.dumps({"raw_json": json.dumps(d), "auto_approve": True}))')"
```

Прогресс — в Temporal UI (<http://localhost:8233>) по `workflow_id` из ответа
API (`{"status": "triggered", "workflow_id": "direct-plan-..."}`).

> **Про репозиторий-заглушку.** В плане используется
> `https://github.com/your-org/your-web-app` — замени его на URL своего проекта
> перед запуском (в промпте `task_0_git_sync` и в поле `graph_name` при желании).


