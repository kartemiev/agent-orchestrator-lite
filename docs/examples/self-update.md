# Пример спринта: самообновление репозитория

В этом примере оркестратор `agent-orchestrator-lite` обновляет **свой собственный
код**. Готовый JSON-план лежит в файле [`self-update.json`](../../examples/self-update.json) —
его можно вставить в Streamlit UI или отправить напрямую на `POST /api/v1/plan/direct`.

## Концепция

Агент OpenHands под управлением Temporal-воркфлоу получает DAG-план из 5 задач,
разбитых на 4 волны. Каждая задача исполняется в изолированном workspace
`/app/workspace/runs/{workflow_id}`, а параллелизм задаётся исключительно полем
`depends_on`: задачи с одинаковым набором зависимостей запускаются одной волной
через `asyncio.gather`.

Волновой исполнитель работает строго по переданному плану:
- сначала стартует только `task_0_git_sync` (Wave 1);
- после его успеха параллельно запускаются `task_a_backend_docs` и
  `task_b_ui_statuses` (Wave 2);
- после обоих — `task_2_tests` (Wave 3);
- и только после успешных тестов — `task_3_deliver` (Wave 4).

Если какая-то задача падает, зависящие от неё задачи пропускаются (fault
isolation), а упавшая фиксируется в сводке workflow.

## DAG-граф по волнам

```
Wave 1  (Init)             task_0_git_sync
                                 │
                ┌────────────────┴────────────────┐
Wave 2  (Dev)   task_a_backend_docs        task_b_ui_statuses
                └────────────────┬────────────────┘
Wave 3  (Verify)              task_2_tests
                                 │
Wave 4  (Delivery)          task_3_deliver
```

### Wave 1 — Init

| Поле | Значение |
|---|---|
| `id` | `task_0_git_sync` |
| `depends_on` | `[]` |
| Суть | Клонирует/обновляет `https://github.com/kartemiev/agent-orchestrator-lite` в workspace и готовит окружение. |

Первый шаг всегда выполняется в одиночку: без склонированного репозитория
последующие задачи-правки упали бы на пустом workspace.

### Wave 2 — Параллельная разработка

Две независимые задачи делят один родитель `task_0_git_sync` и поэтому
исполняются **одновременно** одной волной.

| `id` | Задача | Суть |
|---|---|---|
| `task_a_backend_docs` | Backend/Docs | Добавляет `GET /health/details` в `orchestrator/main.py` и описывает его в `README.md`. |
| `task_b_ui_statuses` | Frontend/UI | Улучшает отображение статуса workflow и сводки задач в `ui/app.py`. |

### Wave 3 — Тесты & Проверка

| Поле | Значение |
|---|---|
| `id` | `task_2_tests` |
| `depends_on` | `["task_a_backend_docs", "task_b_ui_statuses"]` |
| Суть | Прогоняет `python -m py_compile` по всем Python-файлам и чинит найденные проблемы. |

Зависит от **обеих** задач Wave 2 — проверка стартует только когда закончены
и бэкенд, и UI.

### Wave 4 — Доставка

| Поле | Значение |
|---|---|
| `id` | `task_3_deliver` |
| `depends_on` | `["task_2_tests"]` |
| Суть | Коммитит изменения, пушит ветку `feature/self-update` и создаёт Pull Request в `main`. |

Доставка идёт последней и только при зелёных тестах.

> **Примечание про встроенную доставку.** Помимо явного шага `task_3_deliver`,
> воркер оркестратора в конце любого workflow дополнительно вызывает активность
> `git_push_and_create_pr` — это страховка от потери незакоммиченных/неотправленных
> изменений. Если явный шаг доставки уже создал PR, встроенная активность может
> создать ещё один PR на ветке `feature/{workflow_id}`. Для чистой доставки можно
> либо убрать `task_3_deliver` и положиться на встроенный механизм, либо оставить
> явный шаг и вручную закрыть дублирующий PR.

## Запуск

### Вариант 1 — Streamlit UI

1. Открой `http://localhost:8501`.
2. Открой файл [`self-update.json`](../../examples/self-update.json) и скопируй его
   содержимое целиком.
3. Вставь JSON в поле «**Готовый JSON-план**».
4. Отметь чекбокс «**⚡ Auto-approve**» — план утвердится автоматически, и
   Human-in-the-Loop гейт будет пропущен.
5. Нажми «**⚡ Запустить с готовым JSON-планом**».
6. В панели «**Мониторинг и управление workflow**» введи появившийся
   `workflow_id` и наблюдай за статусом (при выключенном Auto-approve там же
   появятся кнопки `APPROVE & DISPATCH` / `Отклонить запуск`).

### Вариант 2 — cURL / REST API

```bash
curl -X POST http://localhost:8085/api/v1/plan/direct \
  -H "Content-Type: application/json" \
  -d @examples/self-update.json
```

Ожидаемый ответ:

```json
{
  "status": "triggered",
  "workflow_id": "direct-plan-...",
  "mode": "direct_json_plan",
  "task_count": 5,
  "auto_approve": false
}
```

Запуск сразу без ручного подтверждения (Auto-approve):

```bash
curl -X POST http://localhost:8085/api/v1/plan/direct \
  -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json; d=json.load(open("examples/self-update.json")); print(json.dumps({"raw_json": json.dumps(d), "auto_approve": True}))')"
```

Либо вручную обернуть план в ключ `raw_json`:

```bash
curl -X POST http://localhost:8085/api/v1/plan/direct \
  -H "Content-Type: application/json" \
  -d '{"raw_json": "<содержимое examples/self-update.json одной строкой>", "auto_approve": true}'
```

После запуска за прогрессом можно следить в Temporal UI (`http://localhost:8233`)
или через `GET /api/v1/...`/клиент Temporal по `workflow_id`.

## Чего ожидать в результате

- В репозитории `kartemiev/agent-orchestrator-lite` появится Pull Request из
  ветки `feature/self-update` в `main` с изменениями `orchestrator/main.py`,
  `ui/app.py` и `README.md`.
- В Temporal UI будет видна история воркфлоу: 4 волны, параллельное исполнение
  Wave 2, изоляция сбоев между задачами.
