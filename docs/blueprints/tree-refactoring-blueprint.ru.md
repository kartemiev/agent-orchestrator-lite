 [English](tree-refactoring-blueprint.md) | Русский

# Blueprint: Рефакторинг дерева и наименований файлов

> Статус: **выполнено** (миграция проведена). Документ описывает целевую
> структуру и план, по которому она была реализована.

---

## 1. Текущая карта репозитория (Current Tree)

```text
agent-orchestrator-lite/
├── .dockerignore            # исключения для Docker build-context
├── .env.example             # шаблон переменных окружения
├── .gitignore               # исключения Git
├── AGENTS.md                # спека JSON-планов (системный промпт для LLM)
├── BIG_EXAMPLE.md           # туториал «Notification Service»
├── EXAMPLE-SPRINT.md        # описание примера самообновления
├── OPENHANDS_SETUP.md       # настройка OpenHands + LLM-провайдеров
├── README.md                # главный README
├── docker/
│   └── openhands-entrypoint.sh   # обёртка entrypoint OpenHands
├── docker-compose.yml       # стек (temporal, openhands, orchestrator, ui)
├── example_big_sprint.json  # JSON-план «Notification Service»
├── example_sprint.json      # JSON-план «самообновление репозитория»
├── orchestrator/
│   ├── Dockerfile           # образ FastAPI Gateway + Temporal Worker
│   ├── activities.py        # базовые активности (971 строка)
│   ├── main.py              # FastAPI Gateway
│   ├── requirements.txt
│   ├── worker.py            # Temporal Worker
│   └── workflows.py         # HITLWorkflow (волновой DAG)
├── ui/
│   ├── Dockerfile
│   ├── app.py               # Streamlit Dashboard
│   └── requirements.txt
└── workspace/
    └── .gitkeep             # пустой рантайм-каталог (bind-mount в контейнеры)
```

---

## 2. Выявленные проблемные места (Gaps & Smells)

1. **Перегруженный корень.** В корне смешаны документация, примеры, конфиг и
   служебные файлы. Для публичного репозитория корень должен содержать только
   точку входа (`README.md`), конвенционные файлы и каталоги кода.
2. **Разнобой регистра и стиля имён документации.** Смешаны стили:
   - `README.md`, `AGENTS.md` — UPPER (общепринятая конвенция, оставить);
   - `OPENHANDS_SETUP.md` — UPPER_SNAKE_CASE;
   - `EXAMPLE-SPRINT.md` — UPPER-KEBAB-CASE (дефис);
   - `BIG_EXAMPLE.md` — UPPER_SNAKE + неинформативный префикс `BIG`
     (непонятно, что именно «большой»).
3. **Неинформативные и несимметричные имена примеров.** `example_sprint.json`
   vs `example_big_sprint.json` не отражают содержание («самообновление» vs
   «Notification Service»); приставка `big` ничего не объясняет. JSON-планы
   не сгруппированы в `examples/`.
4. **Нет каталога `docs/`.** Документация раскидана по корню. Новый
   `docs/blueprints/` появляется «с нуля» без единой конвенции для остальных
   документов.
5. **God-module `orchestrator/activities.py` (971 строка).** Все 4 активности +
   inline-хелперы git/PR + policy-текст в одном файле. Не «нейминг» как таковой,
   но напрямую влияет на организацию каталога (кандидат на пакет `activities/`).
6. **`ui/Dockerfile` собирается из контекста корня (`context: .`).** Копирует
   только `ui/`, но build-context включает docs/examples/workspace (мелкая
   неэффективность; `.dockerignore` отсекает лишь часть).
7. **`.gitignore`/`.dockerignore` неполны для Python-экосистемы.** Нет
   `.mypy_cache/`, `.ruff_cache/`, `dist/`, `build/`, `*.egg-info/`
   (`.pytest_cache/`, `__pycache__/`, `.venv/` уже есть).
8. **Нет `LICENSE`.** Обязательный файл для публичного Open Source-оглашения.
9. **`AGENTS.md` — осознанное исключение.** Это системный промпт, который
   прикладывается к запросу для внешних LLM; файл с таким именем в корне
   авто-обнаруживается агентными тулзами (Cursor/Claude Code). **Оставляем в
   корне** — это конвенция, а не «smell».
10. **`docker-compose.yml`** — устаревшее имя против нового `compose.yaml`
    (не критично, `docker compose` понимает оба). Опционально.

---

## 3. Предлагаемое целевое дерево (Target Tree)

Единая конвенция наименований:

- **Корень**: только конвенционные UPPER-файлы (`README.md`, `AGENTS.md`,
  `LICENSE`) и служебные dot-файлы (`.env.example`, `.gitignore`, `.dockerignore`).
- **Документация** (`docs/`) — **kebab-case** (нижний регистр через дефис).
- **Запускаемые примеры** (`examples/`) — **kebab-case**, имя отражает содержание.

```text
agent-orchestrator-lite/
├── LICENSE                         # NEW: MIT или Apache-2.0
├── README.md                       # главный README (UPPER — конвенция)
├── AGENTS.md                       # спека JSON-планов (UPPER — конвенция, в корне)
├── .env.example
├── .gitignore
├── .dockerignore
├── docker-compose.yml              # (опционально → compose.yaml)
├── docker/
│   └── openhands-entrypoint.sh
├── orchestrator/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                     # (опционально → gateway.py)
│   ├── worker.py
│   ├── workflows.py
│   └── activities.py               # (опционально → пакет activities/)
├── ui/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py
├── examples/                       # NEW: запускаемые JSON-артефакты
│   ├── self-update.json            #  ← example_sprint.json
│   └── notification-service.json   #  ← example_big_sprint.json
├── docs/                           # NEW: вся документация
│   ├── openhands-setup.md          #  ← OPENHANDS_SETUP.md
│   ├── examples/
│   │   ├── self-update.md          #  ← EXAMPLE-SPRINT.md
│   │   └── notification-service.md #  ← BIG_EXAMPLE.md
│   └── blueprints/
│       └── tree-refactoring-blueprint.md  # ← TREE_REFACTORING_BLUEPRINT.md
└── workspace/
    └── .gitkeep
```

Примечание: сам этот blueprint создан по запросу под именем
`TREE_REFACTORING_BLUEPRINT.md` (UPPER_SNAKE). В финальном шаге миграции он
приведён к конвенции kebab-case → `tree-refactoring-blueprint.md`.
---

## 4. Пошаговый план миграции (Migration Steps)

### Фаза 0 — точка отката и лицензия
1. `git init` + первый коммит текущего состояния (репозиторий не был
   git-репозиторием). Дальше использован `git mv` — история файлов сохранена.
2. Добавлен `LICENSE` (MIT).

### Фаза 1 — создать целевые каталоги
```bash
mkdir -p examples docs/examples docs/blueprints
```

### Фаза 2 — перенести JSON-примеры (`git mv`)
```bash
git mv example_sprint.json      examples/self-update.json
git mv example_big_sprint.json  examples/notification-service.json
```

### Фаза 3 — перенести документацию (`git mv`)
```bash
git mv OPENHANDS_SETUP.md docs/openhands-setup.md
git mv EXAMPLE-SPRINT.md  docs/examples/self-update.md
git mv BIG_EXAMPLE.md     docs/examples/notification-service.md
# финальный шаг — нормализовать сам blueprint:
git mv docs/blueprints/TREE_REFACTORING_BLUEPRINT.md \
       docs/blueprints/tree-refactoring-blueprint.md
```

### Фаза 4 — обновить внутренние ссылки

| Файл | Старая ссылка | Новая ссылка |
|---|---|---|
| `README.md` | `BIG_EXAMPLE.md` | `docs/examples/notification-service.md` |
| `README.md` | `./example_big_sprint.json` | `./examples/notification-service.json` |
| `README.md` | `./example_sprint.json` | `./examples/self-update.json` |
| `README.md` | `./EXAMPLE-SPRINT.md` | `./docs/examples/self-update.md` |
| `README.md` | `-d @example_sprint.json` (curl) | `-d @examples/self-update.json` |
| `README.md` | `open("example_sprint.json")` (python) | `open("examples/self-update.json")` |
| `README.md` | `./OPENHANDS_SETUP.md` | `./docs/openhands-setup.md` |
| `README.md` | раздел «Структура» (5 строк про доки/примеры) | переписать под новое дерево |
| `AGENTS.md` | `-d @example_big_sprint.json` (curl) | `-d @examples/notification-service.json` |
| `AGENTS.md` | `./README.md` | без изменений (README в корне) |
| `docs/examples/self-update.md` | `./example_sprint.json` | `../../examples/self-update.json` |
| `docs/examples/self-update.md` | `-d @example_sprint.json` (curl) | `-d @examples/self-update.json` |
| `docs/examples/self-update.md` | `open("example_sprint.json")` (python) | `open("examples/self-update.json")` |
| `docs/examples/notification-service.md` | `./example_big_sprint.json` | `../../examples/notification-service.json` |
| `docs/examples/notification-service.md` | `./README.md` | `../../README.md` |
| `docs/examples/notification-service.md` | `./EXAMPLE-SPRINT.md` | `self-update.md` |
| `docs/examples/notification-service.md` | `-d @example_big_sprint.json` (curl) | `-d @examples/notification-service.json` |
| `docs/examples/notification-service.md` | `open("example_big_sprint.json")` (python) | `open("examples/notification-service.json")` |

Файлы **без** изменений ссылок: `docs/openhands-setup.md` (ссылается на
`docker-compose.yml`, `docker/openhands-entrypoint.sh`, `orchestrator/activities.py`
только как inline-code, не гиперссылки), `docker-compose.yml`,
`orchestrator/*`, `ui/*`, `.env.example`.
### Фаза 5 — донастроить `.gitignore` / `.dockerignore`
Добавить в оба: `.mypy_cache/`, `.ruff_cache/`, `dist/`, `build/`, `*.egg-info/`.
(Перемещение docs/examples не требует правок — они не игнорируются и не копируются
в образы.)

### Фаза 6 — валидация
```bash
python3 -m json.tool examples/self-update.json >/dev/null
python3 -m json.tool examples/notification-service.json >/dev/null
docker compose config -q
# + скрипт проверки битых Markdown-ссылок и баланса code-fences
```

### Фаза 7 (опционально, отдельно от «безопасной» миграции) — реорганизация кода
- Разбить `orchestrator/activities.py` → пакет `orchestrator/activities/`
  (`__init__.py`, `openhands.py`, `workspace.py`, `git.py`, `policy.py`).
- Переименовать `orchestrator/main.py` → `gateway.py` (+ обновить `CMD` в
  `orchestrator/Dockerfile` и `command` в `docker-compose.yml`: `uvicorn gateway:app`).
- Переименовать `docker-compose.yml` → `compose.yaml`.

---

## Принципы решения (кратко)

- Корень = только «витрина» (README/AGENTS/LICENSE + dot-файлы); код — в
  `orchestrator/` и `ui/`; документация — в `docs/`; артефакты — в `examples/`.
- Документы и примеры — kebab-case с содержательными именами.
- `README.md` и `AGENTS.md` остаются UPPER в корне как конвенция (AGENTS.md —
  системный промпт, авто-обнаруживается агентными тулзами).
- Миграция — только переименования/перемещения + правка ссылок, без изменения
  логики (безопасно, обратимо через `git mv`).
