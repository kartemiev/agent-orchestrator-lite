"""Базовые активности оркестратора (Lite-версия).

Самодостаточный модуль: только Temporal SDK + stdlib + requests. Никаких
зависимостей от Совета Старейшин, LLM Planner, Aider, email/alerting-интеграций.

Зарегистрированные в воркере активности:
  - execute_openhands_phase  — выполнение фазы через OpenHands agent-canvas API;
  - prepare_workspace        — создание изолированного workspace и копирование кода;
  - cleanup_workspace        — удаление/сохранение workspace после workflow;
  - git_push_and_create_pr   — финальная доставка: push + Pull Request / Merge Request.
"""

import os
import re
import shutil
import subprocess
import time
from typing import Optional

import requests
from temporalio import activity
from temporalio.exceptions import ApplicationError

import git_provider
import notifier

# --- Конфигурация LLM (передаётся в OpenHands как api_key/base_url) ---
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.deepseek.com")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-pro")

# --- Конфигурация OpenHands (agent-canvas) ---
OPENHANDS_API_URL = os.getenv("OPENHANDS_API_URL", "http://openhands:8000")
OPENHANDS_API_KEY = os.getenv("OPENHANDS_API_KEY", "dev")
OPENHANDS_LOG_COMPLETIONS_DIR = os.getenv(
    "OPENHANDS_LOG_COMPLETIONS_DIR",
    "/home/openhands/.openhands/agent-canvas/logs/completions",
)
_OPENHANDS_HTTP_TIMEOUT_DEFAULT = int(os.getenv("OPENHANDS_HTTP_TIMEOUT_S", "30"))
_OPENHANDS_HTTP_TIMEOUT_VERIFY = int(os.getenv("OPENHANDS_VERIFY_HTTP_TIMEOUT_S", "600"))

# --- Изоляция workspace ---
WORKSPACE_BASE = os.getenv("WORKSPACE_BASE", "/app/workspace")
DEFAULT_WORKSPACE = WORKSPACE_BASE
SOURCE_REPO_PATH = os.getenv("SOURCE_REPO_PATH", f"{WORKSPACE_BASE}/repo")
WORKSPACE_RUNS_DIR = os.getenv("WORKSPACE_RUNS_DIR", f"{WORKSPACE_BASE}/runs")
CLEANUP_WORKSPACE = os.getenv("CLEANUP_WORKSPACE", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

_FINISHED_STATUSES = {"finished"}

# Anti-Fragility & Hygiene Policy — инжектится в промпт OpenHands-фаз.
ANTI_FRAGILITY_POLICY = (
    "\n\n<ANTI_FRAGILITY_POLICY>"
    "Anti-Fragility & Hygiene Policy:\n"
    "1) PHANTOM HUNT BAN: если в ТЗ упомянут файл-артефакт, которого нет в "
    "workspace — сделай РОВНО ОДНУ попытку найти его и иди дальше (запрещён "
    "обход соседних run-каталогов).\n"
    "2) PRE-FLIGHT / SKIP-IF-DONE: перед правками проверь целевые файлы. Если "
    "требуемое УЖЕ сделано — не меняй код, зафиксируй «already done».\n"
    "3) TOKEN / OVERTHINKING BUDGET: максимум 3 цикла размышлений на одну "
    "неопределённость, затем прагматичное решение.\n"
    "4) GIT HYGIENE: НИКОГДА не выполняй слепой `git add -A` или `git commit -a`. "
    "Только точечный staging конкретных путей; мусор/артефакты не коммитим.\n"
    "</ANTI_FRAGILITY_POLICY>"
)


def _safe_int_env(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _openhands_http_timeout(phase_data: dict) -> int:
    task_type = str((phase_data or {}).get("task_type") or "").strip().lower()
    if task_type == "verify":
        return _OPENHANDS_HTTP_TIMEOUT_VERIFY
    return _OPENHANDS_HTTP_TIMEOUT_DEFAULT


# ---------------------------------------------------------------------------
# Вспомогательные функции (workspace)
# ---------------------------------------------------------------------------

def _copy_repo_contents(src_dir: str, dst_dir: str) -> int:
    """Копирует содержимое src_dir в dst_dir, пропуская ``.git``."""
    copied = 0
    for entry in os.scandir(src_dir):
        if entry.name == ".git":
            continue
        src = entry.path
        dst = os.path.join(dst_dir, entry.name)
        if entry.is_dir(follow_symlinks=False):
            shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(src, dst)
        copied += 1
    return copied


def _make_writable(path: str) -> None:
    """Каталогам 0777, файлам 0666 (доступ для UID 1000 и root)."""
    for root, dirs, files in os.walk(path):
        for name in dirs:
            try:
                os.chmod(os.path.join(root, name), 0o777)
            except OSError:
                pass
        for name in files:
            try:
                os.chmod(os.path.join(root, name), 0o666)
            except OSError:
                pass


def ensure_runs_dir_writable() -> str:
    """Создаёт корень изолированных workspace (runs/) с правами 0777."""
    try:
        os.makedirs(WORKSPACE_RUNS_DIR, exist_ok=True)
        os.chmod(WORKSPACE_RUNS_DIR, 0o777)
    except OSError as exc:
        activity.logger.warning(
            "Cannot ensure %s is world-writable: %s: %s",
            WORKSPACE_RUNS_DIR, type(exc).__name__, exc,
        )
    return WORKSPACE_RUNS_DIR


# ---------------------------------------------------------------------------
# Активности: prepare_workspace / cleanup_workspace
# ---------------------------------------------------------------------------

@activity.defn
def prepare_workspace(workspace_dir: str) -> dict:
    """Создаёт изолированный workspace и наполняет его исходным кодом.

    1. ``mkdir -p`` workspace_dir;
    2. если задан базовый репозиторий (SOURCE_REPO_PATH) — копирует содержимое;
    3. выставляет права для процессов контейнеров (UID 1000 и root).
    """
    activity.heartbeat(f"prepare_workspace:{workspace_dir}:creating")
    workspace_dir = os.path.abspath(workspace_dir)
    try:
        runs_parent = os.path.dirname(workspace_dir)
        os.makedirs(runs_parent, exist_ok=True)
        try:
            os.chmod(runs_parent, 0o777)
        except OSError:
            pass

        os.makedirs(workspace_dir, exist_ok=True)
        try:
            os.chmod(workspace_dir, 0o777)
        except OSError:
            pass

        source = None
        if os.path.isdir(SOURCE_REPO_PATH):
            source = SOURCE_REPO_PATH
            activity.heartbeat(f"prepare_workspace:{workspace_dir}:copying_repo")
            copied = _copy_repo_contents(SOURCE_REPO_PATH, workspace_dir)
            activity.logger.info(
                "Workspace %s наполнен исходным кодом из %s (элементов: %d)",
                workspace_dir, SOURCE_REPO_PATH, copied,
            )
        else:
            activity.logger.info(
                "Базовый репозиторий %s не найден — workspace %s создан пустым",
                SOURCE_REPO_PATH, workspace_dir,
            )

        _make_writable(workspace_dir)
        try:
            os.chown(workspace_dir, 1000, 1000)
        except OSError:
            pass

        activity.heartbeat(f"prepare_workspace:{workspace_dir}:done")
        return {"status": "prepared", "workspace_dir": workspace_dir, "source": source}
    except Exception as exc:
        activity.logger.error(
            "prepare_workspace failed: workspace_dir=%s error_type=%s error=%s",
            workspace_dir, type(exc).__name__, exc,
        )
        shutil.rmtree(workspace_dir, ignore_errors=True)
        raise


def _git_state(workspace_dir: str) -> dict:
    """Диагностика git-состояния workspace перед удалением."""
    if not os.path.isdir(os.path.join(workspace_dir, ".git")):
        return {"is_repo": False, "status": "", "unpushed": []}

    state: dict = {"is_repo": True, "status": "", "unpushed": []}
    try:
        status = subprocess.run(
            ["git", "-C", workspace_dir, "status", "--porcelain"],
            capture_output=True, text=True, timeout=30,
        )
        state["status"] = status.stdout.strip()
    except Exception as exc:
        activity.logger.warning(
            "git status failed in %s: %s: %s",
            workspace_dir, type(exc).__name__, exc,
        )

    try:
        log = subprocess.run(
            ["git", "-C", workspace_dir, "log", "--oneline", "origin/main..HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if log.returncode == 0:
            state["unpushed"] = [
                line for line in log.stdout.splitlines() if line.strip()
            ]
    except Exception as exc:
        activity.logger.warning(
            "git log origin/main..HEAD failed in %s: %s: %s",
            workspace_dir, type(exc).__name__, exc,
        )

    return state


@activity.defn
def cleanup_workspace(workspace_dir: str, preserve: bool = False) -> str:
    """Удаляет изолированный workspace после завершения workflow.

    Удаление настраивается флагом CLEANUP_WORKSPACE. При незакоммиченных /
    неотправленных изменениях каталог сохраняется (защита от потери кода).
    """
    if not CLEANUP_WORKSPACE:
        activity.logger.info(
            "CLEANUP_WORKSPACE=false — workspace %s сохранён", workspace_dir,
        )
        return "skipped"

    workspace_dir = os.path.abspath(workspace_dir)
    if not os.path.isdir(workspace_dir):
        return "absent"

    if preserve:
        activity.logger.info(
            "preserve=True — workspace %s сохранён (в волне были упавшие задачи)",
            workspace_dir,
        )
        return "preserved"

    git_state = _git_state(workspace_dir)
    if git_state["is_repo"]:
        if git_state["status"]:
            activity.logger.warning(
                "Workspace %s: незакоммиченные изменения — удаление отменяется:\n%s",
                workspace_dir, git_state["status"],
            )
            return "dirty"
        if git_state["unpushed"]:
            activity.logger.warning(
                "Workspace %s: обнаружены НЕОТПРАВЛЕННЫЕ коммиты — удаление отменяется",
                workspace_dir,
            )
            return "unpushed"

    shutil.rmtree(workspace_dir, ignore_errors=True)
    activity.logger.info("Workspace %s удалён после завершения workflow", workspace_dir)
    return "removed"


# ---------------------------------------------------------------------------
# Активность: execute_openhands_phase
# ---------------------------------------------------------------------------

def _openhands_headers() -> dict:
    return {
        "X-Session-API-Key": OPENHANDS_API_KEY or "dev",
        "Content-Type": "application/json",
    }


def _openhands_llm_base_url() -> str:
    url = LLM_API_URL.rstrip("/")
    if url.endswith("/v1/chat/completions"):
        url = url[: -len("/chat/completions")]
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return url


def _deepseek_user_id(params: dict) -> str:
    """Валидный DeepSeek user_id = workflow_id (изоляция KVCache)."""
    raw = str(params.get("workflow_id") or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", raw)[:512]
    return cleaned or "unknown"


def _build_openhands_prompt(prompt: str, workspace_dir: str) -> str:
    contract = (
        "\n\n<TOOL_CONTRACT>"
        "\nCRITICAL FOR TOOLS: Always provide all required parameters."
        "\nWhen using file_editor with any command (create, str_replace, insert, "
        f"etc.), you MUST explicitly pass the absolute path parameter (e.g., "
        f"{workspace_dir}/path/to/file.py). Never omit the path field."
        "\nJSON SYNTAX RULE: tool arguments MUST be a single strict JSON object. "
        "Use double quotes for ALL keys and string values. Do NOT use single "
        "quotes, trailing commas, or unescaped newlines inside strings."
        "\nSECURITY RISK RULE: pass security_risk as a JSON string in double "
        "quotes (e.g. \"security_risk\": \"LOW\"), never as a bare literal."
        "\nWORKSPACE PRESERVATION RULE: ЗАПРЕЩЕНЫ деструктивные git-команды "
        "(checkout/restore/reset/stash/clean). Строй решение ПОВЕРХ существующего "
        "диффа; разрешены только read-only команды (status/diff/log/show)."
        f"\nThe actual working directory for this session is: {workspace_dir}. "
        f"Resolve all relative paths against it and prefer absolute paths inside it."
        f"\nWORKSPACE ISOLATION RULE: You MUST work ONLY inside {workspace_dir}. "
        f"NEVER modify, read from, or navigate into sibling run directories under "
        f"{os.path.dirname(workspace_dir)}/. If the working directory is empty, "
        f"create the files here or report that the workspace is empty."
        + ANTI_FRAGILITY_POLICY
        + "\n</TOOL_CONTRACT>"
    )
    return f"{prompt}{contract}"


def _fetch_last_agent_error(openhands_url: str, conv_id: str, headers: dict) -> str:
    try:
        resp = requests.get(
            f"{openhands_url}/api/conversations/{conv_id}/events/search",
            params={"limit": 20, "kind": "AgentErrorEvent"},
            headers=headers,
            timeout=30,
        )
        if resp.status_code != 200:
            return ""
        items = (resp.json() or {}).get("items") or []
        if not items:
            return ""
        error = items[-1].get("error")
        return str(error)[:1000] if error else ""
    except Exception:
        return ""


_OPENHANDS_CREATION_HINTS = (
    "создай файл", "создать файл", "создайте файл", "создай новый файл",
    "новый файл", "create file", "create a file", "create a new file",
    "create new file", "new file", "создай скрипт", "создать скрипт",
    "create script", "создай модуль", "создать модуль", "create module",
    "создай проект", "create project", "создай сервис", "create service",
)

_OPENHANDS_GIT_SYNC_HINTS = (
    "склонируй", "склонировать", "клонируй", "клонировать",
    "git clone", "git pull", "обнови репозиторий", "обновить репозиторий",
)


def _is_openhands_creation_or_sync_prompt(prompt: str) -> bool:
    lowered = (prompt or "").lower()
    return any(
        hint in lowered
        for hint in _OPENHANDS_CREATION_HINTS + _OPENHANDS_GIT_SYNC_HINTS
    )


def _guard_openhands_empty_workspace(workspace_dir: str, phase_data: dict) -> None:
    abs_path = os.path.abspath(workspace_dir)
    runs_root = os.path.dirname(abs_path)
    if os.path.basename(runs_root) != "runs":
        return
    if not os.path.isdir(abs_path) or os.listdir(abs_path):
        return
    if _is_openhands_creation_or_sync_prompt(phase_data.get("prompt", "")):
        return
    raise ApplicationError(
        "OpenHands phase: workspace пуст, а фаза требует существующих файлов: "
        f"{abs_path}. Работа в соседних каталогах runs/* запрещена. "
        "Убедитесь, что первый шаг плана — git-sync, либо задан SOURCE_REPO_PATH.",
        type="EmptyWorkspaceError",
        non_retryable=True,
    )


@activity.defn
def execute_openhands_phase(phase_data: dict) -> dict:
    """Выполнение фазы через OpenHands agent-canvas API.

    Контракт agent-canvas 1.10.0:
      1. POST /api/conversations → 201, conversation в статусе "idle";
      2. POST /api/conversations/{id}/events (run: true) → запускает агента;
      3. GET  /api/conversations/{id} → execution_status (polling);
      4. GET  /api/conversations/{id}/agent_final_response → {response}.
    """
    openhands_url = OPENHANDS_API_URL.rstrip("/")
    task_id = phase_data["id"]
    headers = _openhands_headers()
    user_id = _deepseek_user_id(phase_data)
    workspace_dir = phase_data.get("workspace_dir") or DEFAULT_WORKSPACE
    http_timeout = _openhands_http_timeout(phase_data)

    _guard_openhands_empty_workspace(workspace_dir, phase_data)

    prompt = _build_openhands_prompt(phase_data.get("prompt", ""), workspace_dir)

    activity.logger.info(
        "OpenHands phase start: task_id=%s title=%s url=%s workspace_dir=%s",
        task_id, phase_data.get("title"), openhands_url, workspace_dir,
    )
    activity.heartbeat(f"phase:{task_id}:starting")

    conv_id = None
    try:
        create_payload = {
            "workspace": {"kind": "LocalWorkspace", "working_dir": workspace_dir},
            "agent": {
                "kind": "Agent",
                "llm": {
                    "model": phase_data.get("model") or LLM_MODEL,
                    "api_key": LLM_API_KEY,
                    "base_url": _openhands_llm_base_url(),
                    "timeout": _safe_int_env("OPENHANDS_LLM_TIMEOUT", 120),
                    "num_retries": _safe_int_env("OPENHANDS_LLM_NUM_RETRIES", 3),
                    "retry_max_wait": _safe_int_env("OPENHANDS_LLM_RETRY_MAX_WAIT", 32),
                    "drop_params": True,
                    "log_completions": os.getenv("OPENHANDS_LOG_COMPLETIONS", "0") == "1",
                    "log_completions_folder": OPENHANDS_LOG_COMPLETIONS_DIR,
                    "reasoning_effort": "medium",
                    "litellm_extra_body": {"user_id": user_id},
                },
                "tools": [
                    {"name": "terminal", "params": {}},
                    {"name": "file_editor", "params": {}},
                ],
            },
            "confirmation_policy": {"kind": "NeverConfirm"},
        }
        resp = requests.post(
            f"{openhands_url}/api/conversations",
            json=create_payload,
            headers=headers,
            timeout=http_timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"OpenHands create conversation failed: HTTP {resp.status_code} "
                f"body={resp.text[:500]}"
            )
        conv_id = resp.json().get("id")
        if not conv_id:
            raise RuntimeError(
                f"OpenHands did not return conversation id: {resp.text[:500]}"
            )

        resp = requests.post(
            f"{openhands_url}/api/conversations/{conv_id}/events",
            json={
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
                "run": True,
            },
            headers=headers,
            timeout=http_timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"OpenHands send message failed: HTTP {resp.status_code} "
                f"body={resp.text[:500]}"
            )

        final_text = _openhands_poll(openhands_url, conv_id, task_id, headers, http_timeout)
        return {
            "status": "success",
            "task_id": task_id,
            "conv_id": conv_id,
            "response": final_text,
        }
    except Exception as e:
        if conv_id:
            try:
                requests.post(
                    f"{openhands_url}/api/conversations/{conv_id}/pause",
                    headers=headers,
                    timeout=10,
                )
            except Exception:
                pass
        activity.logger.error(
            "OpenHands activity error: task_id=%s error_type=%s error=%s",
            task_id, type(e).__name__, e,
        )
        raise


def _openhands_poll(openhands_url, conv_id, task_id, headers, http_timeout):
    """Опрашивает execution_status до завершения; возвращает финальный ответ."""
    max_polls = 2880
    for poll in range(1, max_polls + 1):
        activity.heartbeat(f"phase:{task_id}:polling:{poll}/{max_polls}")
        time.sleep(10)
        status_resp = requests.get(
            f"{openhands_url}/api/conversations/{conv_id}",
            headers=headers,
            timeout=http_timeout,
        )
        if status_resp.status_code != 200:
            continue
        status = status_resp.json().get("execution_status", "").lower()
        if status in _FINISHED_STATUSES:
            final_text = ""
            final_resp = requests.get(
                f"{openhands_url}/api/conversations/{conv_id}/agent_final_response",
                headers=headers,
                timeout=http_timeout,
            )
            if final_resp.status_code == 200:
                final_text = final_resp.json().get("response", "")
            try:
                requests.post(
                    f"{openhands_url}/api/conversations/{conv_id}/pause",
                    headers=headers,
                    timeout=10,
                )
            except Exception:
                pass
            activity.logger.info(
                "OpenHands phase done: task_id=%s conv_id=%s", task_id, conv_id,
            )
            return final_text
        if status in ["error", "stuck"]:
            detail = _fetch_last_agent_error(openhands_url, conv_id, headers)
            message = (
                f"OpenHands failed on phase {task_id}: execution_status={status}"
            )
            if detail:
                message += f" agent_error={detail}"
            raise RuntimeError(message)

    raise TimeoutError(f"OpenHands timeout on phase {task_id} after {max_polls * 10}s")


# ---------------------------------------------------------------------------
# Активность: git_push_and_create_pr (финальная доставка)
# ---------------------------------------------------------------------------

_TITLE_KEYS = ("graph_name", "title", "name", "plan_name")
_PR_TITLE_MAX = 72
_BRACE_ONLY_RE = re.compile(r"^\s*[{}\[\],};]+\s*$")
_JSON_KEY_LINE_RE = re.compile(r"^\s*[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?\s*:")

_GIT_JUNK_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    "dist", "build", ".gradle", ".idea", ".vscode", ".pytest_cache",
    ".mypy_cache", ".coverage", "coverage", "target", ".cache", "logs",
    ".terraform", "vendor", "site-packages", "var", "tmp",
}
_GIT_JUNK_EXT = {
    ".pyc", ".pyo", ".log", ".tmp", ".swp", ".class",
    ".o", ".a", ".so", ".dll", ".exe",
}


def _git_run(repo_dir: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo_dir, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _remote_url(repo_dir: str) -> Optional[str]:
    try:
        out = _git_run(repo_dir, "config", "--get", "remote.origin.url")
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _provider_token(provider: str) -> str:
    key = "GITHUB_TOKEN" if provider == git_provider.PROVIDER_GITHUB else "GITLAB_TOKEN"
    return os.getenv(key, "").strip()


def _default_branch(
    repo_dir: str, provider: str, project_path: str, project_name: str,
) -> str:
    branch = git_provider.default_branch(provider, project_path, project_name)
    if branch:
        return branch
    try:
        out = _git_run(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD", "--short")
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().rsplit("/", 1)[-1]
    except Exception:
        pass
    for candidate in ("main", "master"):
        check = _git_run(repo_dir, "rev-parse", "--verify", f"origin/{candidate}")
        if check.returncode == 0:
            return candidate
    return "main"


def _unpushed_commits(repo_dir: str, default_branch: str, auth_url: Optional[str]) -> list:
    out = _git_run(repo_dir, "log", "--oneline", f"origin/{default_branch}..HEAD")
    if out.returncode == 0:
        return [line for line in out.stdout.splitlines() if line.strip()]
    if auth_url:
        fetch = _git_run(repo_dir, "fetch", "-q", auth_url, default_branch, timeout=300)
        if fetch.returncode == 0:
            out = _git_run(repo_dir, "log", "--oneline", "FETCH_HEAD..HEAD")
            if out.returncode == 0:
                return [line for line in out.stdout.splitlines() if line.strip()]
    return []


def _sanitize_branch_part(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._/-]", "-", name)
    name = name.replace("..", "-")
    name = re.sub(r"-{2,}", "-", name)
    name = name.strip(".-/")
    return (name[:120].rstrip("./") or "autopilot")


def _push_branch(repo_dir: str, auth_url: str, branch: str) -> tuple:
    out = _git_run(repo_dir, "push", auth_url, f"HEAD:refs/heads/{branch}", timeout=300)
    if out.returncode == 0:
        return True, ""
    first_err = (out.stderr or out.stdout).strip()[:800]
    _git_run(
        repo_dir, "fetch", "-q", auth_url,
        f"refs/heads/{branch}:refs/remotes/origin/{branch}", timeout=300,
    )
    retry = _git_run(
        repo_dir, "push", "--force-with-lease", auth_url,
        f"HEAD:refs/heads/{branch}", timeout=300,
    )
    if retry.returncode == 0:
        return True, first_err
    return False, (retry.stderr or retry.stdout or first_err).strip()[:800]


# Создание PR/MR вынесено в git_provider.create_merge_request (GitHub + GitLab).


def _clean_title(value: str) -> str:
    line = " ".join((value or "").split())
    if len(line) > _PR_TITLE_MAX:
        line = line[:_PR_TITLE_MAX].rstrip()
    return line


def _extract_pr_title(prompt: str, default: str = "chore(autopilot): automated update") -> str:
    import json as _json

    text = (prompt or "").strip()
    if not text:
        return default
    try:
        parsed = _json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        for key in _TITLE_KEYS:
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return _clean_title(value)
        return default
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or _BRACE_ONLY_RE.match(candidate):
            continue
        if _JSON_KEY_LINE_RE.match(candidate):
            continue
        return _clean_title(candidate)
    return default


def _summarize_prompt(prompt: str, limit: int = 800) -> str:
    import json as _json

    text = (prompt or "").strip()
    if not text:
        return ""
    try:
        parsed = _json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        lines = []
        plan_title = next(
            (parsed.get(k) for k in _TITLE_KEYS
             if isinstance(parsed.get(k), str) and parsed[k].strip()),
            None,
        )
        if plan_title:
            lines.append(f"**План:** {plan_title}")
        tasks = parsed.get("tasks")
        if isinstance(tasks, list) and tasks:
            lines.append(f"**Задач:** {len(tasks)}")
            for task in tasks[:5]:
                if not isinstance(task, dict):
                    continue
                lines.append(f"- `{task.get('id') or ''}` {task.get('title') or ''}".strip())
            if len(tasks) > 5:
                lines.append(f"- … и ещё {len(tasks) - 5} задач")
        summary = "\n".join(lines)
        return summary[:limit] if summary else ""
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or _BRACE_ONLY_RE.match(candidate):
            continue
        if _JSON_KEY_LINE_RE.match(candidate):
            continue
        return _clean_title(candidate)[:limit]
    return ""


def _is_junk(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    if any(part in _GIT_JUNK_DIRS for part in parts):
        return True
    return os.path.splitext(path)[1].lower() in _GIT_JUNK_EXT


def _stage_targeted(repo_dir: str) -> list:
    tracked = _git_run(repo_dir, "diff", "--name-only")
    untracked = _git_run(repo_dir, "ls-files", "--others", "--exclude-standard")
    if tracked.returncode != 0 or untracked.returncode != 0:
        err = (tracked.stderr or untracked.stderr or "unknown git error").strip()
        raise RuntimeError(f"stage_targeted: git status failed: {err[:300]}")
    paths = []
    for line in (tracked.stdout + "\n" + untracked.stdout).splitlines():
        p = line.strip()
        if not p or _is_junk(p) or p in paths:
            continue
        paths.append(p)
    if not paths:
        return []
    add = _git_run(repo_dir, "add", "--", *paths)
    if add.returncode != 0:
        raise RuntimeError(f"stage_targeted: git add failed: {add.stderr.strip()[:300]}")
    return paths


@activity.defn
def git_push_and_create_pr(params: dict) -> dict:
    """Финальная доставка: push и создание PR (GitHub) / MR (GitLab)."""
    repo_dir = os.path.abspath(params.get("workspace_dir") or "")
    workflow_id = params.get("workflow_id") or "unknown"
    activity.heartbeat(f"git_push_and_create_pr:{repo_dir}:start")

    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        activity.logger.info(
            "git_push_and_create_pr: %s — не git-репозиторий, доставка не нужна",
            repo_dir,
        )
        return {"status": "no-git", "pull_request_url": None, "commit_url": None}

    remote_url = _remote_url(repo_dir)
    provider = git_provider.detect_provider(remote_url or "", params.get("provider"))
    if not provider:
        activity.logger.warning(
            "git_push_and_create_pr: %s — провайдер не распознан (%s), доставка пропущена",
            repo_dir, remote_url,
        )
        return {"status": "no-git", "pull_request_url": None, "commit_url": None}

    try:
        project_path, project_name = git_provider.parse_remote(remote_url or "", provider)
    except ValueError as exc:
        activity.logger.warning("git_push_and_create_pr: %s", exc)
        return {"status": "no-git", "pull_request_url": None, "commit_url": None}

    default_branch = _default_branch(repo_dir, provider, project_path, project_name)
    token = _provider_token(provider)
    auth_url = git_provider.build_auth_url(provider, project_path, project_name, token)

    # 1) Fallback-коммит незакоммиченных изменений.
    try:
        staged = _stage_targeted(repo_dir)
    except Exception as exc:
        activity.logger.warning("Fallback staging в %s не удался: %s", repo_dir, exc)
        staged = []
    if staged:
        commit = _git_run(
            repo_dir,
            "-c", "user.name=agent-orchestrator",
            "-c", "user.email=agent-orchestrator@localhost",
            "commit", "-m",
            f"chore(autopilot): fallback commit for workflow {workflow_id} "
            f"(files: {', '.join(staged[:20])}{'...' if len(staged) > 20 else ''})",
        )
        if commit.returncode != 0:
            activity.logger.warning(
                "Fallback-коммит в %s не создан: %s",
                repo_dir, (commit.stderr or commit.stdout).strip()[:300],
            )

    # 2) Неотправленные коммиты.
    unpushed = _unpushed_commits(repo_dir, default_branch, auth_url)
    if not unpushed:
        activity.logger.info(
            "git_push_and_create_pr: %s — неотправленных коммитов нет, push/MR не нужны",
            repo_dir,
        )
        return {"status": "up-to-date", "pull_request_url": None, "commit_url": None}

    # 3) Push HEAD в ветку feature/{workflow_id}.
    branch = _sanitize_branch_part(
        str(params.get("branch") or f"feature/{workflow_id}")
    )
    head_sha = ""
    rev = _git_run(repo_dir, "rev-parse", "HEAD")
    if rev.returncode == 0:
        head_sha = rev.stdout.strip()
    commit_url = git_provider.commit_url(provider, project_path, project_name, head_sha)

    if not auth_url:
        token_key = (
            "GITHUB_TOKEN" if provider == git_provider.PROVIDER_GITHUB else "GITLAB_TOKEN"
        )
        activity.logger.warning(
            "git_push_and_create_pr: %s — %s не задан, push невозможен",
            repo_dir, token_key,
        )
        return {
            "status": "push-failed", "error": f"{token_key} is not set",
            "pull_request_url": None, "commit_url": commit_url,
        }

    activity.heartbeat(f"git_push_and_create_pr:{repo_dir}:push:{branch}")
    pushed, push_err = _push_branch(repo_dir, auth_url, branch)
    if not pushed:
        activity.logger.error(
            "git_push_and_create_pr: push в %s (%s) не удался: %s",
            project_path, branch, push_err,
        )
        return {
            "status": "push-failed", "error": push_err,
            "pull_request_url": None, "commit_url": commit_url,
        }

    # 4) Pull Request / Merge Request.
    prompt = str(params.get("prompt") or "").strip()
    title = _extract_pr_title(prompt)
    kind = (
        "Pull Request"
        if provider == git_provider.PROVIDER_GITHUB
        else "Merge Request"
    )
    body_lines = [
        f"Automated {kind} from **agent-orchestrator-lite** "
        f"(workflow `{workflow_id}`).",
        "",
        f"Target branch: `{default_branch}` · Source branch: `{branch}`",
        "",
        "### Commits (local → remote):",
    ]
    body_lines += [f"- {line}" for line in unpushed[:20]]
    if prompt:
        summary = _summarize_prompt(prompt)
        if summary:
            body_lines += ["", "### Summary:", summary]

    mr = git_provider.create_merge_request(
        provider, project_path, project_name, branch, default_branch,
        title, "\n".join(body_lines),
    )
    result = {
        "status": mr["status"],
        "provider": provider,
        "pull_request_url": mr.get("url"),
        "merge_request_url": (
            mr.get("url") if provider == git_provider.PROVIDER_GITLAB else None
        ),
        "commit_url": commit_url,
        "branch": branch,
        "base_branch": default_branch,
        "commits": unpushed,
    }
    if mr.get("error"):
        result["error"] = mr["error"]
    activity.logger.info(
        "git_push_and_create_pr: %s:%s -> %s (%s)",
        provider, branch, mr["status"], mr.get("url"),
    )
    return result


@activity.defn
async def send_notification_activity(payload: dict) -> dict:
    """Отправляет уведомление о событии workflow (HITL / SUCCESS / FAILURE).

    Fire-and-forget: не блокирует workflow (фоновые HTTP-запросы уходят в таски
    notifier). Если каналы не настроены или что-то упало — молча продолжает работу.
    """
    event = str(payload.get("event") or "unknown")
    title = str(payload.get("title") or "agent-orchestrator")
    message = str(payload.get("message") or "")
    html = payload.get("html")
    priority = str(payload.get("priority") or "default")
    try:
        await notifier.send_notification(title, message, html=html, priority=priority)
    except Exception as exc:  # noqa: BLE001
        activity.logger.warning("send_notification_activity(%s): %s", event, exc)
    return {"status": "scheduled", "event": event}









