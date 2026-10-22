"""git_provider — единый интерфейс для создания PR (GitHub) / MR (GitLab).

Провайдер определяется автоматически по URL репозитория или явной переменной
``GIT_PROVIDER`` (``github`` | ``gitlab``). Модуль инкапсулирует
провайдер-специфичную логику:

  - парсинг remote URL;
  - построение auth-URL для ``git push`` (с токеном);
  - определение default branch через REST API;
  - создание Pull Request (GitHub API v3) или Merge Request (GitLab API v4).

GitHub использует ``GITHUB_TOKEN`` и ``https://api.github.com``. GitLab использует
``GITLAB_TOKEN`` и ``GITLAB_URL`` (по умолчанию ``https://gitlab.com``, для
self-hosted — укажите свой base URL).
"""

import logging
import os
import re
import shutil
import subprocess
from typing import Optional, Tuple
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

PROVIDER_GITHUB = "github"
PROVIDER_GITLAB = "gitlab"

GIT_PROVIDER = os.getenv("GIT_PROVIDER", "").strip().lower() or None
GITLAB_URL = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")

_TIMEOUT_S = 30

_GITHUB_REMOTE_RE = re.compile(
    r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$"
)


def detect_provider(remote_url: str, explicit: Optional[str] = None) -> Optional[str]:
    """Возвращает ``github``/``gitlab`` по явному значению или по URL репозитория."""
    value = (explicit or GIT_PROVIDER or "").strip().lower()
    if value in (PROVIDER_GITHUB, PROVIDER_GITLAB):
        return value
    url = (remote_url or "").strip().lower()
    if not url:
        return None
    if "github.com" in url:
        return PROVIDER_GITHUB
    if "gitlab" in url:
        return PROVIDER_GITLAB
    return None


def _gitlab_project_path(remote_url: str) -> Optional[str]:
    """Извлекает путь проекта GitLab (``group/subgroup/project``) из remote URL.

    Поддерживает ``https://``, ``ssh://`` и scp-подобный ``git@host:path`` формат,
    включая self-hosted с портом.
    """
    url = (remote_url or "").strip()
    if url.startswith(("http://", "https://", "ssh://")):
        without_scheme = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url, count=1)
        if "/" not in without_scheme:
            return None
        path = without_scheme.split("/", 1)[1]
    else:
        # scp-like: git@host:group/project.git
        if ":" not in url:
            return None
        path = url.split(":", 1)[1]
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    return path or None


def parse_remote(remote_url: str, provider: str) -> Tuple[str, str]:
    """Возвращает ``(project_path, project_name)`` для провайдера.

    - GitHub: ``(owner, repo)``;
    - GitLab: ``(group/subgroup/project, project)``.
    """
    url = (remote_url or "").strip()
    if provider == PROVIDER_GITHUB:
        match = _GITHUB_REMOTE_RE.search(url)
        if match:
            return match.group(1), match.group(2)
        raise ValueError(f"Не удалось распознать GitHub remote: {url!r}")
    if provider == PROVIDER_GITLAB:
        path = _gitlab_project_path(url)
        if path:
            return path, path.rsplit("/", 1)[-1]
        raise ValueError(f"Не удалось распознать GitLab remote: {url!r}")
    raise ValueError(f"Неизвестный провайдер: {provider!r}")


def build_auth_url(
    provider: str, project_path: str, project_name: str, token: str,
) -> Optional[str]:
    """Auth-URL для ``git push`` (с токеном). ``None``, если токен не задан."""
    if not token:
        return None
    if provider == PROVIDER_GITHUB:
        return f"https://x-access-token:{token}@github.com/{project_path}/{project_name}.git"
    if provider == PROVIDER_GITLAB:
        host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", GITLAB_URL).split("/", 1)[0]
        return f"https://oauth2:{token}@{host}/{project_path}.git"
    return None

def _github_default_branch(owner: str, repo: str) -> Optional[str]:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        return None
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=_TIMEOUT_S,
        )
        if resp.status_code == 200:
            branch = resp.json().get("default_branch")
            return str(branch) if branch else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Не удалось получить default_branch %s/%s: %s", owner, repo, exc,
        )
    return None


def _gitlab_default_branch(project_path: str) -> Optional[str]:
    token = os.getenv("GITLAB_TOKEN", "").strip()
    if not token:
        return None
    project_id = quote(project_path, safe="")
    try:
        resp = requests.get(
            f"{GITLAB_URL}/api/v4/projects/{project_id}",
            headers={"PRIVATE-TOKEN": token},
            timeout=_TIMEOUT_S,
        )
        if resp.status_code == 200:
            branch = resp.json().get("default_branch")
            return str(branch) if branch else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Не удалось получить default_branch %s: %s", project_path, exc,
        )
    return None


def default_branch(
    provider: str, project_path: str, project_name: str,
) -> Optional[str]:
    """Default branch через REST API провайдера (``None``, если не определён)."""
    if provider == PROVIDER_GITHUB:
        return _github_default_branch(project_path, project_name)
    if provider == PROVIDER_GITLAB:
        return _gitlab_default_branch(project_path)
    return None


def _create_github_pr(
    owner: str, repo: str, source: str, target: str, title: str, body: str,
) -> dict:
    if shutil.which("gh"):
        try:
            out = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--repo", f"{owner}/{repo}",
                    "--base", target,
                    "--head", source,
                    "--title", title,
                    "--body", body,
                ],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode == 0 and out.stdout.strip():
                url = out.stdout.strip().splitlines()[-1]
                return {"status": "pr-created", "url": url}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "gh pr create не сработал — переключаюсь на REST API: %s", exc,
            )

    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        return {"status": "pr-failed", "url": None, "error": "GITHUB_TOKEN is not set"}
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    api = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    try:
        resp = requests.post(
            api,
            headers=headers,
            json={"title": title, "head": source, "base": target, "body": body},
            timeout=_TIMEOUT_S,
        )
        if resp.status_code == 201:
            data = resp.json()
            return {"status": "pr-created", "url": data.get("html_url")}
        if resp.status_code == 422:
            existing = requests.get(
                api,
                headers=headers,
                params={"head": f"{owner}:{source}", "state": "open"},
                timeout=_TIMEOUT_S,
            )
            if existing.status_code == 200:
                items = existing.json()
                if items:
                    return {"status": "pr-exists", "url": items[0].get("html_url")}
        return {"status": "pr-failed", "url": None, "error": resp.text[:500]}
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "pr-failed", "url": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _create_gitlab_mr(
    project_path: str, source: str, target: str, title: str, description: str,
) -> dict:
    token = os.getenv("GITLAB_TOKEN", "").strip()
    if not token:
        return {"status": "mr-failed", "url": None, "error": "GITLAB_TOKEN is not set"}
    project_id = quote(project_path, safe="")
    api = f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests"
    headers = {"PRIVATE-TOKEN": token}
    try:
        resp = requests.post(
            api,
            headers=headers,
            json={
                "source_branch": source,
                "target_branch": target,
                "title": title,
                "description": description,
            },
            timeout=_TIMEOUT_S,
        )
        if resp.status_code == 201:
            data = resp.json()
            return {"status": "mr-created", "url": data.get("web_url")}
        if resp.status_code == 409:
            existing = requests.get(
                api,
                headers=headers,
                params={
                    "source_branch": source,
                    "target_branch": target,
                    "state": "opened",
                },
                timeout=_TIMEOUT_S,
            )
            if existing.status_code == 200:
                items = existing.json()
                if items:
                    return {"status": "mr-exists", "url": items[0].get("web_url")}
        return {"status": "mr-failed", "url": None, "error": resp.text[:500]}
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "mr-failed", "url": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def create_merge_request(
    provider: str,
    project_path: str,
    project_name: str,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
) -> dict:
    """Создаёт PR (GitHub) или MR (GitLab). Возвращает ``{status, url, error}``."""
    if provider == PROVIDER_GITHUB:
        return _create_github_pr(
            project_path, project_name, source_branch, target_branch,
            title, description,
        )
    if provider == PROVIDER_GITLAB:
        return _create_gitlab_mr(
            project_path, source_branch, target_branch, title, description,
        )
    return {"status": "failed", "url": None, "error": f"Unknown provider: {provider}"}


def commit_url(
    provider: str, project_path: str, project_name: str, sha: str,
) -> Optional[str]:
    """URL коммита на вебе провайдера."""
    if not sha:
        return None
    if provider == PROVIDER_GITHUB:
        return f"https://github.com/{project_path}/{project_name}/commit/{sha}"
    if provider == PROVIDER_GITLAB:
        return f"{GITLAB_URL}/{project_path}/-/commit/{sha}"
    return None

