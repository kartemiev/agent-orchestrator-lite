 English | [Русский](tree-refactoring-blueprint.ru.md)

# Blueprint: tree and file naming refactoring

> Status: **done** (the migration has been carried out). The document describes
> the target structure and the plan by which it was implemented.

---

## 1. Current repository map (Current Tree)

```text
agent-orchestrator-lite/
├── .dockerignore            # exclusions for the Docker build context
├── .env.example             # environment variable template
├── .gitignore               # Git exclusions
├── AGENTS.md                # JSON plan spec (system prompt for LLMs)
├── BIG_EXAMPLE.md           # "Notification Service" tutorial
├── EXAMPLE-SPRINT.md        # self-update example walkthrough
├── OPENHANDS_SETUP.md       # OpenHands + LLM provider setup
├── README.md                # main README
├── docker/
│   └── openhands-entrypoint.sh   # OpenHands entrypoint wrapper
├── docker-compose.yml       # stack (temporal, openhands, orchestrator, ui)
├── example_big_sprint.json  # JSON plan "Notification Service"
├── example_sprint.json      # JSON plan "repository self-update"
├── orchestrator/
│   ├── Dockerfile           # FastAPI Gateway + Temporal Worker image
│   ├── activities.py        # base activities (971 lines)
│   ├── main.py              # FastAPI Gateway
│   ├── requirements.txt
│   ├── worker.py            # Temporal Worker
│   └── workflows.py         # HITLWorkflow (wave-based DAG)
├── ui/
│   ├── Dockerfile
│   ├── app.py               # Streamlit Dashboard
│   └── requirements.txt
└── workspace/
    └── .gitkeep             # empty runtime directory (bind-mounted into containers)
```

---

## 2. Identified problem areas (Gaps & Smells)

1. **Overloaded root.** The root mixes documentation, examples, config and
   service files. For a public repository the root should contain only the
   entry point (`README.md`), conventional files and code directories.
2. **Inconsistent casing and naming style of the documentation.** Styles are mixed:
   - `README.md`, `AGENTS.md` — UPPER (a widespread convention, keep);
   - `OPENHANDS_SETUP.md` — UPPER_SNAKE_CASE;
   - `EXAMPLE-SPRINT.md` — UPPER-KEBAB-CASE (hyphen);
   - `BIG_EXAMPLE.md` — UPPER_SNAKE + a non-informative `BIG` prefix
     (it is unclear what exactly is "big").
3. **Non-informative and asymmetric example names.** `example_sprint.json`
   vs `example_big_sprint.json` do not reflect the content ("self-update" vs
   "Notification Service"); the `big` prefix explains nothing. The JSON plans
   are not grouped into `examples/`.
4. **No `docs/` directory.** Documentation is scattered across the root. The new
   `docs/blueprints/` appears "from scratch" without a single convention for the
   other documents.
5. **God-module `orchestrator/activities.py` (971 lines).** All 4 activities +
   inline git/PR helpers + policy text in a single file. Not "naming" as such,
   but it directly affects the directory organization (a candidate for an
   `activities/` package).
6. **`ui/Dockerfile` is built from the root context (`context: .`).** It copies
   only `ui/`, but the build context includes docs/examples/workspace (a minor
   inefficiency; `.dockerignore` cuts off only part of it).
7. **`.gitignore`/`.dockerignore` are incomplete for the Python ecosystem.** Missing
   `.mypy_cache/`, `.ruff_cache/`, `dist/`, `build/`, `*.egg-info/`
   (`.pytest_cache/`, `__pycache__/`, `.venv/` are already there).
8. **No `LICENSE`.** A mandatory file for a public Open Source agreement.
9. **`AGENTS.md` is a deliberate exception.** It is a system prompt that is
   attached to a request for external LLMs; a file with this name in the root
   is auto-discovered by agent tools (Cursor/Claude Code). **We keep it in
   the root** — it is a convention, not a "smell".
10. **`docker-compose.yml`** — an outdated name versus the newer `compose.yaml`
    (not critical, `docker compose` understands both). Optional.

---

## 3. Proposed target tree (Target Tree)

A single naming convention:

- **Root**: only conventional UPPER files (`README.md`, `AGENTS.md`,
  `LICENSE`) and service dot-files (`.env.example`, `.gitignore`, `.dockerignore`).
- **Documentation** (`docs/`) — **kebab-case** (lowercase with hyphens).
- **Runnable examples** (`examples/`) — **kebab-case**, the name reflects the content.

```text
agent-orchestrator-lite/
├── LICENSE                         # NEW: MIT or Apache-2.0
├── README.md                       # main README (UPPER — convention)
├── AGENTS.md                       # JSON plan spec (UPPER — convention, in the root)
├── .env.example
├── .gitignore
├── .dockerignore
├── docker-compose.yml              # (optional → compose.yaml)
├── docker/
│   └── openhands-entrypoint.sh
├── orchestrator/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                     # (optional → gateway.py)
│   ├── worker.py
│   ├── workflows.py
│   └── activities.py               # (optional → activities/ package)
├── ui/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py
├── examples/                       # NEW: runnable JSON artifacts
│   ├── self-update.json            #  ← example_sprint.json
│   └── notification-service.json   #  ← example_big_sprint.json
├── docs/                           # NEW: all documentation
│   ├── openhands-setup.md          #  ← OPENHANDS_SETUP.md
│   ├── examples/
│   │   ├── self-update.md          #  ← EXAMPLE-SPRINT.md
│   │   └── notification-service.md #  ← BIG_EXAMPLE.md
│   └── blueprints/
│       └── tree-refactoring-blueprint.md  # ← TREE_REFACTORING_BLUEPRINT.md
└── workspace/
    └── .gitkeep
```

Note: this blueprint itself was created on request under the name
`TREE_REFACTORING_BLUEPRINT.md` (UPPER_SNAKE). In the final migration step it is
brought to the kebab-case convention → `tree-refactoring-blueprint.md`.
---

## 4. Step-by-step migration plan (Migration Steps)

### Phase 0 — rollback point and license
1. `git init` + the first commit of the current state (the repository was not a
   git repository). From then on `git mv` was used — the file history is preserved.
2. `LICENSE` (MIT) was added.

### Phase 1 — create the target directories
```bash
mkdir -p examples docs/examples docs/blueprints
```

### Phase 2 — move the JSON examples (`git mv`)
```bash
git mv example_sprint.json      examples/self-update.json
git mv example_big_sprint.json  examples/notification-service.json
```

### Phase 3 — move the documentation (`git mv`)
```bash
git mv OPENHANDS_SETUP.md docs/openhands-setup.md
git mv EXAMPLE-SPRINT.md  docs/examples/self-update.md
git mv BIG_EXAMPLE.md     docs/examples/notification-service.md
# final step — normalize the blueprint itself:
git mv docs/blueprints/TREE_REFACTORING_BLUEPRINT.md \
       docs/blueprints/tree-refactoring-blueprint.md
```

### Phase 4 — update the internal links

| File | Old link | New link |
|---|---|---|
| `README.md` | `BIG_EXAMPLE.md` | `docs/examples/notification-service.md` |
| `README.md` | `./example_big_sprint.json` | `./examples/notification-service.json` |
| `README.md` | `./example_sprint.json` | `./examples/self-update.json` |
| `README.md` | `./EXAMPLE-SPRINT.md` | `./docs/examples/self-update.md` |
| `README.md` | `-d @example_sprint.json` (curl) | `-d @examples/self-update.json` |
| `README.md` | `open("example_sprint.json")` (python) | `open("examples/self-update.json")` |
| `README.md` | `./OPENHANDS_SETUP.md` | `./docs/openhands-setup.md` |
| `README.md` | the "Structure" section (5 lines about docs/examples) | rewrite for the new tree |
| `AGENTS.md` | `-d @example_big_sprint.json` (curl) | `-d @examples/notification-service.json` |
| `AGENTS.md` | `./README.md` | unchanged (README is in the root) |
| `docs/examples/self-update.md` | `./example_sprint.json` | `../../examples/self-update.json` |
| `docs/examples/self-update.md` | `-d @example_sprint.json` (curl) | `-d @examples/self-update.json` |
| `docs/examples/self-update.md` | `open("example_sprint.json")` (python) | `open("examples/self-update.json")` |
| `docs/examples/notification-service.md` | `./example_big_sprint.json` | `../../examples/notification-service.json` |
| `docs/examples/notification-service.md` | `./README.md` | `../../README.md` |
| `docs/examples/notification-service.md` | `./EXAMPLE-SPRINT.md` | `self-update.md` |
| `docs/examples/notification-service.md` | `-d @example_big_sprint.json` (curl) | `-d @examples/notification-service.json` |
| `docs/examples/notification-service.md` | `open("example_big_sprint.json")` (python) | `open("examples/notification-service.json")` |

Files **without** link changes: `docs/openhands-setup.md` (references
`docker-compose.yml`, `docker/openhands-entrypoint.sh`, `orchestrator/activities.py`
only as inline code, not as hyperlinks), `docker-compose.yml`,
`orchestrator/*`, `ui/*`, `.env.example`.
### Phase 5 — extend `.gitignore` / `.dockerignore`
Add to both: `.mypy_cache/`, `.ruff_cache/`, `dist/`, `build/`, `*.egg-info/`.
(Moving docs/examples requires no edits — they are neither ignored nor copied
into the images.)

### Phase 6 — validation
```bash
python3 -m json.tool examples/self-update.json >/dev/null
python3 -m json.tool examples/notification-service.json >/dev/null
docker compose config -q
# + a script that checks for broken Markdown links and code-fence balance
```

### Phase 7 (optional, separate from the "safe" migration) — code reorganization
- Split `orchestrator/activities.py` → the `orchestrator/activities/` package
  (`__init__.py`, `openhands.py`, `workspace.py`, `git.py`, `policy.py`).
- Rename `orchestrator/main.py` → `gateway.py` (+ update `CMD` in
  `orchestrator/Dockerfile` and `command` in `docker-compose.yml`: `uvicorn gateway:app`).
- Rename `docker-compose.yml` → `compose.yaml`.

---

## Solution principles (in brief)

- Root = only the "showcase" (README/AGENTS/LICENSE + dot-files); code — in
  `orchestrator/` and `ui/`; documentation — in `docs/`; artifacts — in `examples/`.
- Documents and examples — kebab-case with meaningful names.
- `README.md` and `AGENTS.md` stay UPPER in the root as a convention (AGENTS.md is
  a system prompt auto-discovered by agent tools).
- The migration consists only of renames/moves + link fixes, without changing the
  logic (safe, reversible via `git mv`).
