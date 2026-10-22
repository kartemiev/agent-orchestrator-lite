#!/usr/bin/env python3
"""agent-cli — CLI для запуска и глубокой диагностики agent-orchestrator-lite.

Работает в двух режимах (определяется автоматически):

1. **С хоста** (из терминала разработчика):
   - `submit` — чистый HTTP в Gateway (http://127.0.0.1:8085);
   - `status` / `inspect` / `diagnose` / `list` — перезапускают себя внутри контейнера
     оркестратора через ``docker exec`` (там есть temporalio и прямой доступ
     к Temporal/OpenHands);
   - `logs` — читает ``docker logs`` напрямую через subprocess.

2. **Внутри контейнера оркестратора**
   (``docker exec agent-orchestrator-lite python /app/scripts/agent-cli.py``):
   - Temporal — через temporalio (temporal:7233);
   - OpenHands — через HTTP (openhands:8000);
   - логи/состояние контейнеров — через Docker Engine API поверх
     ``/var/run/docker.sock`` (docker CLI внутри контейнера отсутствует).

Только стандартная библиотека Python 3.11 (temporalio подключается лишь внутри
контейнера). Никаких сторонних зависимостей.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.1.0"

# ---------------------------------------------------------------------------
# Константы / определение окружения
# ---------------------------------------------------------------------------

SCRIPT_PATH = os.path.abspath(__file__)
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_PATH))

# Признак запуска внутри контейнера оркестратора: там лежит /app/main.py
# (COPY . . из orchestrator/) и примонтирован /app/workspace.
IN_CONTAINER = os.path.exists("/app/main.py")

ORCHESTRATOR_CONTAINER_DEFAULT = "agent-orchestrator-lite"
OPENHANDS_CONTAINER = "openhands"

TERMINAL_STATUSES = {
    "COMPLETED", "FAILED", "TERMINATED", "CANCELED", "CANCELLED",
    "TIMED_OUT", "CONTINUED_AS_NEW",
}

STATUS_MARKERS = {
    "completed": "✔",
    "failed": "✘",
    "running": "…",
    "pending": "·",
}

# ---------------------------------------------------------------------------
# Парсинг логов оркестратора
# ---------------------------------------------------------------------------

_RE_PHASE_START = re.compile(
    r"OpenHands phase start: task_id=(\S+) title=(.*?) url=\S+ workspace_dir=(\S+)"
)
_RE_PHASE_DONE = re.compile(r"OpenHands phase done: task_id=(\S+) conv_id=(\S+)")
_RE_ACTIVITY_ERROR = re.compile(
    r"OpenHands activity error: task_id=(\S+) error_type=(\S+) error=(.*)"
)
_RE_TASK_DONE = re.compile(r"Задача (\S+) завершена")
_RE_TASK_FAILED = re.compile(r"Задача (\S+) упала: (.*)")
_RE_WAVE = re.compile(r"Волна: запуск \d+ параллельных задач: (\[.*\])")


def parse_orchestrator_logs(logs_text: str, workflow_id: str) -> dict:
    """Разбирает логи оркестратора в карту «задача → статус/сессия»."""
    analysis = {
        "task_ids": set(),
        "started": set(),
        "conv": {},
        "finished": set(),
        "failed": {},
        "errors": {},
        "waves": [],
    }

    lines = (logs_text or "").splitlines()

    # Первый проход: задачи данного workflow (по workspace_dir=.../runs/<id>).
    for line in lines:
        m = _RE_PHASE_START.search(line)
        if not m:
            continue
        tid, _title, workspace_dir = m.groups()
        if workflow_id and workflow_id not in workspace_dir:
            continue
        analysis["task_ids"].add(tid)
        analysis["started"].add(tid)

    # Второй проход: статусы/сессии, скоуп по task_ids данного workflow.
    scope = analysis["task_ids"]
    for line in lines:
        m = _RE_PHASE_DONE.search(line)
        if m:
            tid, cid = m.groups()
            if tid in scope:
                analysis["conv"][tid] = cid
            continue
        m = _RE_TASK_DONE.search(line)
        if m and m.group(1) in scope:
            analysis["finished"].add(m.group(1))
            continue
        m = _RE_TASK_FAILED.search(line)
        if m and m.group(1) in scope:
            analysis["failed"][m.group(1)] = m.group(2).strip()
            continue
        m = _RE_ACTIVITY_ERROR.search(line)
        if m and m.group(1) in scope:
            tid, etype, err = m.groups()
            analysis["errors"][tid] = f"{etype}: {err.strip()}"
            continue
        m = _RE_WAVE.search(line)
        if m:
            try:
                wave = json.loads(m.group(1))
            except (ValueError, TypeError):
                continue
            analysis["waves"].append([w for w in wave if w in scope])

    return analysis


def compute_wave_levels(tasks: list) -> dict:
    """Считает «уровень волны» (0-based) для каждой задачи по depends_on."""
    ids = [t.get("id") for t in tasks]
    id_set = set(ids)
    depends = {}
    for t in tasks:
        tid = t.get("id")
        depends[tid] = [d for d in (t.get("depends_on") or []) if d in id_set]

    levels: dict = {}
    visiting: set = set()

    def level(tid: str) -> int:
        if tid in levels:
            return levels[tid]
        if tid in visiting:
            return 0  # защита от цикла (невалидный план)
        visiting.add(tid)
        deps = depends.get(tid, [])
        lvl = 0 if not deps else 1 + max(level(d) for d in deps)
        visiting.discard(tid)
        levels[tid] = lvl
        return lvl

    for tid in ids:
        level(tid)
    return levels

# ---------------------------------------------------------------------------
# Загрузка .env (только для хоста; не перезаписывает уже заданные переменные)
# ---------------------------------------------------------------------------

def load_dotenv(path=None) -> None:
    path = path or os.path.join(REPO_ROOT, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


# ---------------------------------------------------------------------------
# HTTP (stdlib)
# ---------------------------------------------------------------------------

def http_request(method, url, headers=None, json_body=None, timeout=30):
    headers = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ---------------------------------------------------------------------------
# Docker Engine API поверх unix-сокета (для режима «внутри контейнера»)
# ---------------------------------------------------------------------------

def docker_socket_path():
    host = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    if host.startswith("unix://"):
        return host[len("unix://"):]
    return "/var/run/docker.sock"


def _dechunk(data):
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        j = data.find(b"\r\n", i)
        if j == -1:
            break
        try:
            size = int(data[i:j].strip().split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        start = j + 2
        out += data[start:start + size]
        i = start + size + 2
    return bytes(out)


def docker_api(method, path, params=None, timeout=60):
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    raw_req = (
        f"{method} {path}{qs} HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1")
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(docker_socket_path())
    except OSError as exc:
        raise RuntimeError(
            f"cannot reach Docker daemon at {docker_socket_path()}: {exc}"
        ) from exc
    try:
        sock.sendall(raw_req)
        data = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()

    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_line = lines[0].decode("latin-1") if lines else ""
    try:
        code = int(status_line.split()[1])
    except (IndexError, ValueError):
        code = 500
    resp_headers = {}
    for line in lines[1:]:
        if b":" in line:
            k, _, v = line.partition(b":")
            resp_headers[k.decode().strip().lower()] = v.decode().strip()
    if resp_headers.get("transfer-encoding", "").lower() == "chunked":
        body = _dechunk(body)
    return code, resp_headers, body


def demux_docker_stream(raw):
    out = bytearray()
    i = 0
    n = len(raw)
    while i + 8 <= n:
        length = int.from_bytes(raw[i + 4:i + 8], "big")
        if length > (n - i - 8):
            out += raw[i:]
            i = n
            break
        out += raw[i + 8:i + 8 + length]
        i += 8 + length
    if i < n:
        out += raw[i:]
    return bytes(out)


def have_docker_cli():
    return shutil.which("docker") is not None


def docker_logs(container, tail=500):
    if have_docker_cli():
        try:
            proc = subprocess.run(
                ["docker", "logs", "--tail", str(tail), container],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode == 0:
                return (proc.stdout or "") + (proc.stderr or "")
        except (OSError, subprocess.TimeoutExpired):
            pass

    code, _, body = docker_api(
        "GET", f"/containers/{container}/logs",
        params={"stdout": "1", "stderr": "1", "tail": str(tail)},
    )
    if code != 200:
        raise RuntimeError(
            f"docker logs '{container}' failed: HTTP {code} {body[:300]!r}"
        )
    return demux_docker_stream(body).decode("utf-8", "replace")


def docker_ps():
    if have_docker_cli():
        try:
            proc = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{json .}}"],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode == 0:
                out = []
                for line in proc.stdout.splitlines():
                    try:
                        d = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    names = d.get("Names", "")
                    name = names.split(",")[0] if names else d.get("ID", "")[:12]
                    out.append({
                        "name": name,
                        "state": d.get("State", ""),
                        "status": d.get("Status", ""),
                        "image": d.get("Image", ""),
                    })
                if out:
                    return out
        except (OSError, subprocess.TimeoutExpired):
            pass

    code, _, body = docker_api("GET", "/containers/json", params={"all": "1"})
    if code != 200:
        return []
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return []
    out = []
    for c in data:
        names = c.get("Names") or []
        out.append({
            "name": names[0].lstrip("/") if names else (c.get("Id", "")[:12]),
            "state": c.get("State", ""),
            "status": c.get("Status", ""),
            "image": c.get("Image", ""),
        })
    return out

# ---------------------------------------------------------------------------
# OpenHands agent-canvas API
# ---------------------------------------------------------------------------

def openhands_headers(cfg):
    return {
        "X-Session-API-Key": cfg["openhands_key"],
        "Content-Type": "application/json",
    }


def openhands_conversation_status(cfg, conv_id):
    status, raw = http_request(
        "GET", f"{cfg['openhands']}/api/conversations/{conv_id}",
        headers=openhands_headers(cfg), timeout=30,
    )
    if status != 200:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    return str(data.get("execution_status") or data.get("status") or "")


def openhands_agent_error(cfg, conv_id, limit=20):
    params = urllib.parse.urlencode({"limit": limit, "kind": "AgentErrorEvent"})
    status, raw = http_request(
        "GET",
        f"{cfg['openhands']}/api/conversations/{conv_id}/events/search?{params}",
        headers=openhands_headers(cfg), timeout=30,
    )
    if status != 200:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    items = data.get("items") or []
    if not items:
        return ""
    error = items[-1].get("error")
    return str(error)[:1000] if error else ""


def openhands_list_conversations(cfg):
    status, raw = http_request(
        "GET", f"{cfg['openhands']}/api/conversations",
        headers=openhands_headers(cfg), timeout=30,
    )
    if status != 200:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, list):
        return data
    for key in ("items", "conversations", "results", "data"):
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return data[key]
    return []


# ---------------------------------------------------------------------------
# Temporal
# ---------------------------------------------------------------------------

def extract_failures(events):
    """Достаёт из истории workflow сообщения/трейсбэки об ошибках."""
    failures = []
    scheduled = {}
    try:
        for ev in events:
            which = ev.WhichOneof("attributes")
            if which == "activity_task_scheduled_event_attributes":
                scheduled[ev.event_id] = (
                    ev.activity_task_scheduled_event_attributes.activity_type.name
                )
            elif which == "activity_task_failed_event_attributes":
                attr = ev.activity_task_failed_event_attributes
                f = attr.failure
                failures.append({
                    "type": f"activity:{scheduled.get(attr.scheduled_event_id, 'unknown')}",
                    "message": (f.message if f else "") or "",
                    "stack_trace": (f.stack_trace if f else "") or "",
                })
            elif which == "workflow_execution_failed_event_attributes":
                attr = ev.workflow_execution_failed_event_attributes
                f = attr.failure
                failures.append({
                    "type": "workflow",
                    "message": (f.message if f else "") or "",
                    "stack_trace": (f.stack_trace if f else "") or "",
                })
    except Exception as exc:  # noqa: BLE001
        failures.append({"type": "parse_error", "message": str(exc), "stack_trace": ""})
    return failures


async def _get_plan(handle):
    try:
        plan = await handle.query("get_generated_plan")
    except Exception:
        return {}
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except (ValueError, TypeError):
            return {}
    return plan if isinstance(plan, dict) else {}


async def wait_workflow(client, workflow_id, timeout_s=8 * 3600, poll_s=5):
    start = time.time()
    while time.time() - start < timeout_s:
        handle = client.get_workflow_handle(workflow_id)
        desc = await handle.describe()
        if desc.status.name in TERMINAL_STATUSES:
            return desc
        await asyncio.sleep(poll_s)
    return None


# ---------------------------------------------------------------------------
# Утилиты отображения
# ---------------------------------------------------------------------------

def render_table(rows, headers):
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    lines = []
    lines.append("  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))
    return "\n".join(lines)


def build_task_status(tasks, analysis, conv_statuses):
    out = {}
    for t in tasks:
        tid = t.get("id")
        conv_id = analysis["conv"].get(tid)
        cs = conv_statuses.get(conv_id, "") if conv_id else ""
        if tid in analysis["failed"]:
            out[tid] = "failed"
        elif tid in analysis["finished"]:
            out[tid] = "completed"
        elif conv_id and cs == "finished":
            out[tid] = "completed"
        elif conv_id and cs in ("error", "stuck"):
            out[tid] = "failed"
        elif tid in analysis["started"] or conv_id:
            out[tid] = "running"
        else:
            out[tid] = "pending"
    return out


def print_filtered(logs, terms):
    terms = [t for t in terms if t]
    if not terms:
        print("(no filter terms)")
        return
    matched = [ln for ln in logs.splitlines() if any(t in ln for t in terms)]
    if not matched:
        print("(no matching log lines)")
        return
    for ln in matched[-500:]:
        print(ln)


# ---------------------------------------------------------------------------
# Утилиты для команды list
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"^\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*$"
)


def parse_duration(value, default_seconds=24 * 3600):
    """Разбирает '24h', '1h30m', '45m', '2d', '30s' в секунды."""
    value = (value or "").strip().lower()
    if not value:
        return default_seconds
    m = _DURATION_RE.match(value)
    if not m or not any(m.groups()):
        raise ValueError(f"invalid duration: {value!r}")
    d, h, mi, s = (int(x or 0) for x in m.groups())
    total = d * 86400 + h * 3600 + mi * 60 + s
    if total <= 0:
        raise ValueError(f"invalid duration: {value!r}")
    return total


def human_duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _status_name(status) -> str:
    """Нормализует имя статуса (убирает protobuf-префикс, если есть)."""
    name = getattr(status, "name", None)
    if isinstance(name, str) and name:
        return name.replace("WORKFLOW_EXECUTION_STATUS_", "")
    return str(status or "?")


async def _collect_workflows(client, query, page_size=100):
    """Собирает список workflow; поддерживает async-итератор и context-manager."""
    out = []
    iterator = client.list_workflows(query=query, page_size=page_size)
    if hasattr(iterator, "__aenter__"):
        async with iterator as it:
            async for wf in it:
                out.append(wf)
    else:
        async for wf in iterator:
            out.append(wf)
    return out


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

def cmd_submit(cfg, args):
    path = args.plan
    if not os.path.isfile(path):
        print(f"error: plan file not found: {path}", file=sys.stderr)
        return 1
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON plan: {exc}", file=sys.stderr)
        return 1
    if not isinstance(plan.get("tasks"), list):
        print("error: plan must contain a 'tasks' list", file=sys.stderr)
        return 1

    auto = bool(args.auto_approve or plan.get("auto_approve"))
    payload = {"raw_json": raw, "auto_approve": auto}
    if plan.get("wave_max_concurrency") is not None:
        payload["wave_max_concurrency"] = plan["wave_max_concurrency"]

    status, resp_raw = http_request(
        "POST", f"{cfg['gateway']}/api/v1/plan/direct",
        json_body=payload, timeout=30,
    )
    if status >= 400:
        print(
            f"error: gateway returned HTTP {status}: {resp_raw[:500]!r}",
            file=sys.stderr,
        )
        return 1
    try:
        data = json.loads(resp_raw)
    except (ValueError, TypeError):
        print(f"error: unexpected gateway response: {resp_raw[:500]!r}", file=sys.stderr)
        return 1

    wf_id = data.get("workflow_id")
    print(
        f"{STATUS_MARKERS['completed']} Submitted workflow_id={wf_id}  "
        f"tasks={data.get('task_count')}  auto_approve={auto}"
    )
    if args.wait:
        if not wf_id:
            print("error: no workflow_id in response", file=sys.stderr)
            return 1
        print("Waiting for the workflow to finish…")
        if cfg["in_container"]:
            return wait_and_summary(cfg, wf_id)
        return _exec_in_container(cfg, ["status", wf_id, "--wait"])
    return 0


def wait_and_summary(cfg, workflow_id):
    from temporalio.client import Client

    async def _run():
        try:
            client = await Client.connect(cfg["temporal"])
        except Exception as exc:  # noqa: BLE001
            print(f"error: cannot connect to Temporal at {cfg['temporal']}: {exc}",
                  file=sys.stderr)
            return 1
        desc = await wait_workflow(client, workflow_id)
        if desc is None:
            print("⏳ workflow is still running (wait timeout)")
            return 0
        print(f"\nFinal workflow status: {desc.status.name}\n")
        return await render_status(client, workflow_id, cfg)

    return asyncio.run(_run())


async def render_status(client, workflow_id, cfg):
    handle = client.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    plan = await _get_plan(handle)

    orch_logs = ""
    try:
        orch_logs = docker_logs(cfg["container"], tail=3000)
    except Exception:
        pass
    analysis = parse_orchestrator_logs(orch_logs, workflow_id)

    conv_statuses = {}
    for tid, conv_id in analysis["conv"].items():
        cs = openhands_conversation_status(cfg, conv_id)
        if cs:
            conv_statuses[conv_id] = cs

    _print_status(workflow_id, desc, plan, analysis, conv_statuses)
    return 0


def _print_status(workflow_id, desc, plan, analysis, conv_statuses):
    print(f"Workflow : {workflow_id}")
    print(f"Status   : {desc.status.name}"
          + ("" if desc.close_time is None else f" (closed {desc.close_time.isoformat()})"))
    print(f"Started  : {desc.start_time.isoformat() if desc.start_time else 'n/a'}")
    print(f"History  : {desc.history_length} events")

    tasks = (plan or {}).get("tasks") or []
    if not tasks:
        print("\nPlan: not available yet")
        return

    levels = compute_wave_levels(tasks)
    task_status = build_task_status(tasks, analysis, conv_statuses)
    max_level = max(levels.values()) if levels else 0

    for lvl in range(max_level + 1):
        wave = [t for t in tasks if levels.get(t["id"]) == lvl]
        print(f"\nWave {lvl + 1}:")
        for t in wave:
            tid = t["id"]
            st = task_status.get(tid, "pending")
            marker = STATUS_MARKERS.get(st, "·")
            print(f"  {marker} {tid:<30} [{st:<10}] {t.get('title') or ''}")

    n = len(tasks)
    done = sum(1 for s in task_status.values() if s == "completed")
    failed = sum(1 for s in task_status.values() if s == "failed")
    print(f"\nSummary: {done}/{n} completed, {failed} failed")


async def render_inspect(client, workflow_id, cfg):
    handle = client.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    plan = await _get_plan(handle)

    print(f"=== Workflow {workflow_id} ===")
    print(f"Status  : {desc.status.name}")
    print(f"Started : {desc.start_time.isoformat() if desc.start_time else 'n/a'}")
    print(f"Closed  : {desc.close_time.isoformat() if desc.close_time else 'n/a'}")
    print(f"History : {desc.history_length} events")

    print("\n--- Containers ---")
    try:
        containers = docker_ps()
    except Exception as exc:  # noqa: BLE001
        containers = []
        print(f"(docker ps failed: {exc})")
    stack = [
        c for c in containers
        if c["name"].startswith("agent-") or c["name"] == OPENHANDS_CONTAINER
    ]
    if stack:
        print(render_table(
            [[c["name"], c["state"], c["status"], c["image"]] for c in stack],
            ["container", "state", "status", "image"],
        ))
    else:
        print("(no stack containers found)")

    try:
        orch_logs = docker_logs(cfg["container"], tail=3000)
    except Exception as exc:  # noqa: BLE001
        orch_logs = ""
        print(f"(orchestrator logs unavailable: {exc})")
    analysis = parse_orchestrator_logs(orch_logs, workflow_id)

    tasks = plan.get("tasks") or []
    print("\n--- DAG steps -> OpenHands sessions ---")
    rows = []
    for t in tasks:
        tid = t.get("id")
        conv_id = analysis["conv"].get(tid, "")
        cs = openhands_conversation_status(cfg, conv_id) if conv_id else ""
        err = openhands_agent_error(cfg, conv_id) if conv_id else ""
        rows.append([
            tid,
            (t.get("title") or "")[:44],
            conv_id or "—",
            cs or "—",
            "error" if err else "—",
        ])
    if rows:
        print(render_table(rows, ["task_id", "title", "conv_id", "execution_status", "error"]))
    else:
        print("(no tasks in plan)")

    print("\n--- AgentErrorEvent details ---")
    shown = False
    for t in tasks:
        tid = t.get("id")
        conv_id = analysis["conv"].get(tid, "")
        if conv_id:
            err = openhands_agent_error(cfg, conv_id)
            if err:
                shown = True
                print(f"[{tid}] conv={conv_id}")
                print(f"  {err}")
        if tid in analysis["failed"]:
            shown = True
            print(f"[{tid}] orchestrator: {analysis['failed'][tid]}")
        if tid in analysis["errors"]:
            shown = True
            print(f"[{tid}] activity error: {analysis['errors'][tid]}")
    if not shown:
        print("(no errors detected)")

    print("\n--- OpenHands sessions for this workflow ---")
    try:
        convs = openhands_list_conversations(cfg)
        matched = [
            c for c in convs
            if workflow_id in json.dumps(c, ensure_ascii=False)
        ]
    except Exception as exc:  # noqa: BLE001
        matched = []
        print(f"(could not list conversations: {exc})")
    if matched:
        for c in matched:
            cid = c.get("id", "?")
            st = c.get("execution_status") or c.get("status") or "?"
            print(f"  {cid}  [{st}]")
    else:
        print("(none)")

    return 0


async def render_list(client, args, cfg):
    seconds = parse_duration(args.since)
    limit = max(1, int(args.limit))
    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=seconds)

    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = f'StartTime > "{since_str}"'

    rows = []
    try:
        rows = await _collect_workflows(client, query)
    except Exception:
        # Standard visibility может не поддерживать StartTime-фильтр —
        # откатываемся на полный листинг и фильтруем на клиенте.
        try:
            rows = await _collect_workflows(client, None)
        except Exception as exc:  # noqa: BLE001
            print(f"error: cannot list workflows: {exc}", file=sys.stderr)
            return 1

    allowed = {"RUNNING", "COMPLETED", "FAILED", "TERMINATED"}
    filtered = []
    for wf in rows:
        st = _status_name(getattr(wf, "status", None))
        if st not in allowed:
            continue
        if wf.start_time and wf.start_time < since:
            continue
        filtered.append(wf)

    filtered.sort(
        key=lambda w: w.start_time or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    filtered = filtered[:limit]

    if not filtered:
        print(f"No workflows found for the last {human_duration(seconds)} "
              f"(status RUNNING/COMPLETED/FAILED/TERMINATED).")
        return 0

    table_rows = []
    for wf in filtered:
        st = _status_name(getattr(wf, "status", None))
        start = wf.start_time.isoformat(timespec="seconds") if wf.start_time else "n/a"
        if wf.close_time and wf.start_time:
            dur = human_duration((wf.close_time - wf.start_time).total_seconds())
            dur_finished = f"{dur} · closed {wf.close_time.isoformat(timespec='seconds')}"
        elif wf.start_time:
            dur_finished = f"{human_duration((now - wf.start_time).total_seconds())} (running)"
        else:
            dur_finished = "n/a"
        table_rows.append([wf.id, st, start, dur_finished])

    print(f"Workflows (last {human_duration(seconds)}):")
    print(render_table(
        table_rows,
        ["Workflow ID", "Status", "Start Time", "Duration / Finished"],
    ))
    print(f"\nTotal shown: {len(table_rows)}")
    return 0


def cmd_logs(cfg, args):
    workflow_id = args.workflow_id
    tail = args.tail

    print(f"=== Orchestrator logs (container '{cfg['container']}') — workflow {workflow_id} ===")
    try:
        orch_logs = docker_logs(cfg["container"], tail=tail)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1
    analysis = parse_orchestrator_logs(orch_logs, workflow_id)

    terms = [workflow_id]
    if args.phase:
        terms.append(args.phase)
        if args.phase in analysis["conv"]:
            terms.append(analysis["conv"][args.phase])
    else:
        terms.extend(sorted(analysis["task_ids"]))
    print_filtered(orch_logs, terms)

    conv_ids = (
        [analysis["conv"][args.phase]]
        if (args.phase and args.phase in analysis["conv"])
        else list(analysis["conv"].values())
    )
    print(f"\n=== OpenHands logs (container '{OPENHANDS_CONTAINER}') — sessions for {workflow_id} ===")
    if conv_ids:
        try:
            oh_logs = docker_logs(OPENHANDS_CONTAINER, tail=tail)
        except Exception as exc:  # noqa: BLE001
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print_filtered(oh_logs, conv_ids)
    else:
        print("(no OpenHands sessions mapped — nothing to show)")
    return 0


def llm_chat_url(cfg):
    base = (cfg["llm_api_url"] or "https://api.deepseek.com/v1").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions"


def llm_diagnose(cfg, dump):
    system = (
        "You are an expert SRE debugging assistant for the agent-orchestrator-lite "
        "system (Temporal + OpenHands). A sprint (workflow) has failed. Analyze the "
        "provided dump and answer in Russian in a SHORT, actionable form:\n"
        "1) Root cause (1-3 sentences).\n"
        "2) Most likely fix (concrete steps or commands).\n"
        "No markdown headers. Keep it under 250 words."
    )
    payload = {
        "model": cfg["llm_model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": dump[:12000]},
        ],
        "temperature": 0.2,
        # Reasoning-модели (deepseek-v4-pro) тратят токены на reasoning_content,
        # поэтому лимит должен покрывать и рассуждения, и финальный ответ.
        "max_tokens": 4096,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['llm_api_key']}",
    }
    status, raw = http_request(
        "POST", llm_chat_url(cfg), headers=headers, json_body=payload, timeout=120,
    )
    if status != 200:
        raise RuntimeError(f"LLM request failed HTTP {status}: {raw[:500]!r}")
    try:
        data = json.loads(raw)
        choice = data["choices"][0]
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise RuntimeError(f"unexpected LLM response: {raw[:500]!r}") from exc

    message = choice.get("message") or {}
    answer = (message.get("content") or "").strip()
    if not answer:
        # Если модель исчерпала max_tokens на рассуждениях, content пуст —
        # показываем хотя бы reasoning_content, чтобы не терять результат.
        reasoning = (message.get("reasoning_content") or "").strip()
        if reasoning:
            answer = (
                "(LLM не вернула финальный ответ — ниже сырые рассуждения; "
                "увеличьте max_tokens)\n" + reasoning[:1500]
            )
        else:
            finish = choice.get("finish_reason")
            raise RuntimeError(
                f"empty LLM response (finish_reason={finish}); raw={raw[:300]!r}"
            )
    return answer


def build_dump(workflow_id, desc, plan, failures, orch_logs, openhands_errors):
    lines = []
    lines.append(f"Workflow ID: {workflow_id}")
    lines.append(f"Temporal status: {desc.status.name if desc else 'unknown'}")
    if desc is not None:
        lines.append(f"Start: {desc.start_time} | Close: {desc.close_time}")
    tasks = (plan or {}).get("tasks") or []
    if tasks:
        brief = [
            {"id": t.get("id"), "title": t.get("title"),
             "depends_on": t.get("depends_on")}
            for t in tasks
        ]
        lines.append("Plan tasks: " + json.dumps(brief, ensure_ascii=False, indent=2))

    lines.append("\n=== Temporal failures ===")
    if failures:
        for f in failures:
            lines.append(f"- [{f.get('type')}] {f.get('message')}")
            if f.get("stack_trace"):
                lines.append("  stack: " + f["stack_trace"][:1500])
    else:
        lines.append("(none)")

    lines.append("\n=== OpenHands agent errors ===")
    if openhands_errors:
        for e in openhands_errors:
            lines.append(f"- task {e['task_id']} (conv {e['conv_id']}): {e['error']}")
    else:
        lines.append("(none)")

    lines.append("\n=== Orchestrator logs (filtered) ===")
    lines.append((orch_logs or "(none)")[-4000:])
    return "\n".join(lines)


async def render_diagnose(client, workflow_id, cfg):
    handle = client.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    plan = await _get_plan(handle)

    failures = []
    try:
        history = await handle.fetch_history()
        failures = extract_failures(history.events)
    except Exception as exc:  # noqa: BLE001
        failures = [{"type": "history_error", "message": str(exc), "stack_trace": ""}]

    try:
        orch_logs = docker_logs(cfg["container"], tail=3000)
    except Exception as exc:  # noqa: BLE001
        orch_logs = f"(orchestrator logs unavailable: {exc})"
    analysis = parse_orchestrator_logs(orch_logs, workflow_id)

    openhands_errors = []
    for tid, conv_id in analysis["conv"].items():
        err = openhands_agent_error(cfg, conv_id)
        if err:
            openhands_errors.append({"task_id": tid, "conv_id": conv_id, "error": err})

    dump = build_dump(workflow_id, desc, plan, failures, orch_logs, openhands_errors)

    if not cfg["llm_api_key"]:
        print("error: LLM_API_KEY is not set — cannot run LLM diagnosis.\n",
              file=sys.stderr)
        print(dump)
        return 1

    print("Collecting the failure dump and asking the LLM (DeepSeek)…")
    try:
        answer = llm_diagnose(cfg, dump)
    except Exception as exc:  # noqa: BLE001
        print(f"error: LLM diagnosis failed: {exc}", file=sys.stderr)
        print("\n=== Collected dump (for manual analysis) ===")
        print(dump)
        return 1

    print("\n=== Diagnosis (LLM) ===")
    print(answer)
    print("\n=== Raw dump (truncated) ===")
    print(dump[:4000])
    return 0

# ---------------------------------------------------------------------------
# CLI / dispatch
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="agent-cli",
        description="CLI для запуска и глубокой диагностики agent-orchestrator-lite.",
    )
    parser.add_argument("--version", action="version", version=f"agent-cli {VERSION}")
    parser.add_argument(
        "--container", default=None,
        help="имя контейнера оркестратора (по умолчанию agent-orchestrator-lite или "
             "env ORCHESTRATOR_CONTAINER)",
    )
    parser.add_argument("--gateway", default=None, help="URL Gateway API")
    parser.add_argument("--openhands", default=None, help="URL OpenHands agent-canvas API")
    parser.add_argument("--temporal", default=None, help="адрес Temporal (host:port)")
    parser.add_argument(
        "--in-container", action="store_true", help=argparse.SUPPRESS,
    )

    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("submit", help="запустить спринт из JSON-плана")
    s.add_argument("plan", help="путь к JSON-файлу плана")
    s.add_argument("--auto-approve", action="store_true",
                   help="пропустить Human-in-the-Loop гейт")
    s.add_argument("-w", "--wait", action="store_true",
                   help="дождаться завершения workflow и показать статус")

    st = sub.add_parser("status", help="краткий статус волн и задач")
    st.add_argument("workflow_id")
    st.add_argument("-w", "--wait", action="store_true",
                    help="дождаться завершения workflow перед показом статуса")

    i = sub.add_parser("inspect", help="глубокая инспекция workflow")
    i.add_argument("workflow_id")

    l = sub.add_parser("logs", help="точечные логи оркестратора и OpenHands")
    l.add_argument("workflow_id")
    l.add_argument("--phase", default=None, help="фильтр по id задачи (фазы)")
    l.add_argument("--tail", type=int, default=2000,
                   help="сколько последних строк логирования читать (по умолчанию 2000)")

    d = sub.add_parser("diagnose", help="LLM-диагностика упавшего спринта")
    d.add_argument("workflow_id")

    li = sub.add_parser("list", help="список спринтов за указанный интервал")
    li.add_argument("--since", default="24h",
                    help="интервал времени (24h, 1h30m, 45m, 2d; по умолчанию 24h)")
    li.add_argument("--limit", type=int, default=20,
                    help="максимум строк (по умолчанию 20)")

    return parser


def build_config(args):
    load_dotenv()
    in_container = bool(IN_CONTAINER or args.in_container)

    bind_ip = os.environ.get("BIND_IP", "127.0.0.1")
    gateway_port = (
        os.environ.get("ORCHESTRATOR_GATEWAY_PORT")
        or os.environ.get("GATEWAY_PORT")
        or "8085"
    )
    agent_canvas_port = os.environ.get("AGENT_CANVAS_PORT", "8000")
    temporal_port = os.environ.get("TEMPORAL_PORT", "7233")

    if args.gateway:
        gateway = args.gateway
    elif os.environ.get("GATEWAY_URL"):
        gateway = os.environ["GATEWAY_URL"]
    else:
        gateway = (
            "http://127.0.0.1:8000"
            if in_container
            else f"http://{bind_ip}:{gateway_port}"
        )

    if args.openhands:
        openhands = args.openhands
    elif os.environ.get("OPENHANDS_API_URL"):
        openhands = os.environ["OPENHANDS_API_URL"]
    else:
        openhands = (
            "http://openhands:8000"
            if in_container
            else f"http://{bind_ip}:{agent_canvas_port}"
        )

    if args.temporal:
        temporal = args.temporal
    elif os.environ.get("TEMPORAL_HOST"):
        temporal = os.environ["TEMPORAL_HOST"]
    else:
        temporal = "temporal:7233" if in_container else f"{bind_ip}:{temporal_port}"

    container = (
        args.container
        or os.environ.get("ORCHESTRATOR_CONTAINER")
        or ORCHESTRATOR_CONTAINER_DEFAULT
    )

    return {
        "in_container": in_container,
        "bind_ip": bind_ip,
        "gateway_port": gateway_port,
        "gateway": gateway.rstrip("/"),
        "openhands": openhands.rstrip("/"),
        "temporal": temporal,
        "container": container,
        "openhands_key": (
            os.environ.get("OPENHANDS_API_KEY")
            or os.environ.get("OPENHANDS_SESSION_API_KEY")
            or "dev"
        ),
        "llm_api_url": (
            os.environ.get("LLM_BASE_URL")
            or os.environ.get("LLM_API_URL")
            or "https://api.deepseek.com/v1"
        ),
        "llm_api_key": (
            os.environ.get("LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ),
        "llm_model": os.environ.get("LLM_MODEL", "deepseek-v4-pro"),
    }


_GLOBAL_FLAGS = {"--container", "--gateway", "--openhands", "--temporal",
                 "--in-container"}


def strip_globals(argv):
    out = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _GLOBAL_FLAGS:
            i += 1 if a == "--in-container" else 2
            continue
        if any(a.startswith(f"{f}=") for f in _GLOBAL_FLAGS):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def needs_container(args):
    if args.command in ("submit", "logs"):
        return False
    return True


def _exec_in_container(cfg, argv):
    exec_argv = [
        "docker", "exec", cfg["container"],
        "python", "/app/scripts/agent-cli.py",
    ] + argv
    try:
        proc = subprocess.run(exec_argv)
    except FileNotFoundError:
        print("error: 'docker' not found on PATH — install Docker or run the "
              "script inside the container.", file=sys.stderr)
        return 1
    return proc.returncode


def re_invoke(cfg, argv):
    code = _exec_in_container(cfg, strip_globals(argv))
    if code != 0:
        print(f"\n[agent-cli] command failed inside container "
              f"'{cfg['container']}' (exit {code}).", file=sys.stderr)
    return code


def run_status(cfg, args):
    from temporalio.client import Client

    async def _run():
        try:
            client = await Client.connect(cfg["temporal"])
        except Exception as exc:  # noqa: BLE001
            print(f"error: cannot connect to Temporal at {cfg['temporal']}: {exc}",
                  file=sys.stderr)
            return 1
        if args.wait:
            desc = await wait_workflow(client, args.workflow_id)
            if desc is None:
                print("⏳ workflow is still running (wait timeout)")
                return 0
            print(f"\nFinal workflow status: {desc.status.name}\n")
        return await render_status(client, args.workflow_id, cfg)

    return asyncio.run(_run())


def run_list(cfg, args):
    from temporalio.client import Client

    async def _run():
        try:
            client = await Client.connect(cfg["temporal"])
        except Exception as exc:  # noqa: BLE001
            print(f"error: cannot connect to Temporal at {cfg['temporal']}: {exc}",
                  file=sys.stderr)
            return 1
        try:
            return await render_list(client, args, cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"error: {exc}", file=sys.stderr)
            return 1

    return asyncio.run(_run())


def run_async_cmd(fn, cfg, workflow_id):
    from temporalio.client import Client

    async def _run():
        try:
            client = await Client.connect(cfg["temporal"])
        except Exception as exc:  # noqa: BLE001
            print(f"error: cannot connect to Temporal at {cfg['temporal']}: {exc}",
                  file=sys.stderr)
            return 1
        try:
            return await fn(client, workflow_id, cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"error: {exc}", file=sys.stderr)
            return 1

    return asyncio.run(_run())


def dispatch(args, cfg):
    if args.command == "submit":
        return cmd_submit(cfg, args)
    if args.command == "status":
        return run_status(cfg, args)
    if args.command == "inspect":
        return run_async_cmd(render_inspect, cfg, args.workflow_id)
    if args.command == "logs":
        return cmd_logs(cfg, args)
    if args.command == "diagnose":
        return run_async_cmd(render_diagnose, cfg, args.workflow_id)
    if args.command == "list":
        return run_list(cfg, args)
    return 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = build_config(args)

    if not cfg["in_container"] and needs_container(args):
        return re_invoke(cfg, argv)

    try:
        return dispatch(args, cfg)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
