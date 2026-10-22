"""Юнит-тесты модуля git_provider (единый интерфейс GitHub PR / GitLab MR).

Проверяют определение провайдера, парсинг remote URL, auth-URL для push,
URL коммита и создание PR/MR через REST API (с замоканными ``requests.get`` /
``requests.post``).
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1] / "orchestrator"
if str(ORCHESTRATOR_DIR) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATOR_DIR))

import git_provider  # noqa: E402


class _FakeResponse:
    """Минимальный заменитель ``requests.Response``."""

    def __init__(self, status_code: int = 200, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


# --- detect_provider -----------------------------------------------------------


def test_detect_provider_github_by_url():
    assert git_provider.detect_provider("https://github.com/o/r.git") == "github"


def test_detect_provider_gitlab_by_url():
    assert git_provider.detect_provider("git@gitlab.com:g/p.git") == "gitlab"


def test_detect_provider_explicit_git_provider(monkeypatch):
    monkeypatch.setattr(git_provider, "GIT_PROVIDER", "gitlab")
    # self-hosted GitLab без слова "gitlab" в домене
    assert git_provider.detect_provider("https://git.example.com/g/p.git") == "gitlab"


def test_detect_provider_unknown():
    assert git_provider.detect_provider("https://bitbucket.org/o/r.git") is None


# --- parse_remote --------------------------------------------------------------


def test_parse_remote_github():
    assert git_provider.parse_remote("https://github.com/owner/repo.git", "github") == ("owner", "repo")


def test_parse_remote_gitlab_scp():
    assert git_provider.parse_remote("git@gitlab.com:group/sub/project.git", "gitlab") == ("group/sub/project", "project")


def test_parse_remote_gitlab_self_hosted():
    assert git_provider.parse_remote("https://git.example.com/group/project.git", "gitlab") == ("group/project", "project")


def test_parse_remote_unknown_provider_raises():
    with pytest.raises(ValueError):
        git_provider.parse_remote("https://github.com/o/r.git", "bitbucket")


# --- push_url / auth_extra_header / mask_token ---------------------------------


def test_push_url_github():
    assert git_provider.push_url("github", "o", "r") == "https://github.com/o/r.git"


def test_push_url_gitlab(monkeypatch):
    monkeypatch.setattr(git_provider, "GITLAB_URL", "https://gitlab.example.com")
    assert git_provider.push_url("gitlab", "g/p", "p") == "https://gitlab.example.com/g/p.git"


def test_push_url_unknown_provider():
    assert git_provider.push_url("bitbucket", "o", "r") is None


def test_auth_extra_header_github():
    import base64

    expected = "AUTHORIZATION: basic " + base64.b64encode(
        b"x-access-token:tok"
    ).decode("ascii")
    assert git_provider.auth_extra_header("github", "tok") == expected


def test_auth_extra_header_no_token():
    assert git_provider.auth_extra_header("github", "") is None


def test_auth_extra_header_unknown_provider():
    assert git_provider.auth_extra_header("bitbucket", "tok") is None


def test_mask_token_removes_token_and_credentials():
    text = "remote: https://x-access-token:SECRET@github.com/o/r.git failed"
    out = git_provider.mask_token(text, "SECRET")
    assert "SECRET" not in out
    assert "<redacted>" in out


def test_mask_token_no_token():
    assert git_provider.mask_token("some text", "") == "some text"


# --- commit_url ----------------------------------------------------------------


def test_commit_url_github():
    assert git_provider.commit_url("github", "o", "r", "abc123") == "https://github.com/o/r/commit/abc123"


def test_commit_url_gitlab(monkeypatch):
    monkeypatch.setattr(git_provider, "GITLAB_URL", "https://gitlab.example.com")
    assert git_provider.commit_url("gitlab", "g/p", "p", "abc123") == "https://gitlab.example.com/g/p/-/commit/abc123"

# --- default_branch (REST API) -------------------------------------------------


def test_default_branch_gitlab(monkeypatch):
    monkeypatch.setattr(git_provider, "GITLAB_URL", "https://gitlab.com")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-token")

    with mock.patch("git_provider.requests.get") as get:
        get.return_value = _FakeResponse(200, {"default_branch": "main"})
        branch = git_provider.default_branch("gitlab", "g/p", "p")

        assert branch == "main"
        assert get.call_args.args[0] == "https://gitlab.com/api/v4/projects/g%2Fp"
        assert get.call_args.kwargs["headers"] == {"PRIVATE-TOKEN": "glpat-token"}


def test_default_branch_gitlab_no_token(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with mock.patch("git_provider.requests.get") as get:
        assert git_provider.default_branch("gitlab", "g/p", "p") is None
        get.assert_not_called()


# --- create_merge_request: GitHub ----------------------------------------------


def test_create_github_pr_created(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setattr(git_provider.shutil, "which", lambda x: None)  # без gh CLI

    with mock.patch("git_provider.requests.post") as post:
        post.return_value = _FakeResponse(201, {"html_url": "https://github.com/o/r/pull/7"})
        result = git_provider.create_merge_request(
            "github", "o", "r", "feature/x", "main", "Title", "Body",
        )

        assert result["status"] == "pr-created"
        assert result["url"] == "https://github.com/o/r/pull/7"
        assert post.call_args.args[0] == "https://api.github.com/repos/o/r/pulls"
        assert post.call_args.kwargs["json"]["head"] == "feature/x"
        assert post.call_args.kwargs["json"]["base"] == "main"


def test_create_github_pr_no_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(git_provider.shutil, "which", lambda x: None)

    result = git_provider.create_merge_request(
        "github", "o", "r", "feature/x", "main", "Title", "Body",
    )
    assert result["status"] == "pr-failed"
    assert "GITHUB_TOKEN" in result["error"]


# --- create_merge_request: GitLab ----------------------------------------------


def test_create_gitlab_mr_created(monkeypatch):
    monkeypatch.setattr(git_provider, "GITLAB_URL", "https://gitlab.com")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-token")

    with mock.patch("git_provider.requests.post") as post:
        post.return_value = _FakeResponse(201, {"web_url": "https://gitlab.com/g/p/-/merge_requests/9"})
        result = git_provider.create_merge_request(
            "gitlab", "group/sub/project", "project",
            "feature/x", "main", "Title", "Desc",
        )

        assert result["status"] == "mr-created"
        assert result["url"] == "https://gitlab.com/g/p/-/merge_requests/9"
        # project path должен быть URL-encoded
        assert post.call_args.args[0] == (
            "https://gitlab.com/api/v4/projects/group%2Fsub%2Fproject/merge_requests"
        )
        assert post.call_args.kwargs["headers"] == {"PRIVATE-TOKEN": "glpat-token"}
        assert post.call_args.kwargs["json"]["source_branch"] == "feature/x"
        assert post.call_args.kwargs["json"]["target_branch"] == "main"


def test_create_gitlab_mr_existing_409(monkeypatch):
    monkeypatch.setattr(git_provider, "GITLAB_URL", "https://gitlab.com")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-token")

    with mock.patch("git_provider.requests.post") as post, \
         mock.patch("git_provider.requests.get") as get:
        post.return_value = _FakeResponse(409, text="already exists")
        get.return_value = _FakeResponse(200, [{"web_url": "https://gitlab.com/g/p/-/merge_requests/9"}])

        result = git_provider.create_merge_request(
            "gitlab", "g/p", "p", "feature/x", "main", "Title", "Desc",
        )

        assert result["status"] == "mr-exists"
        assert result["url"] == "https://gitlab.com/g/p/-/merge_requests/9"


def test_create_gitlab_mr_no_token(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    result = git_provider.create_merge_request(
        "gitlab", "g/p", "p", "feature/x", "main", "Title", "Desc",
    )
    assert result["status"] == "mr-failed"
    assert "GITLAB_TOKEN" in result["error"]

