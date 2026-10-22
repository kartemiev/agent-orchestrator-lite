"""Юнит-тесты модуля notifier (ntfy push + Resend email).

Проверяют:
  - graceful fallback (no-op, если каналы не настроены);
  - корректные HTTP-запросы (URL, заголовки, тело) с замоканным ``requests.post``;
  - fire-and-forget поведение ``send_notification`` (не блокирует, проглатывает
    ошибки HTTP).

Модуль ``notifier`` лежит в ``orchestrator/``, поэтому добавляем этот каталог в
``sys.path`` перед импортом.
"""

import asyncio
import sys
from pathlib import Path
from unittest import mock

import pytest
import requests

ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1] / "orchestrator"
if str(ORCHESTRATOR_DIR) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATOR_DIR))

import notifier  # noqa: E402


class _FakeResponse:
    """Минимальный заменитель ``requests.Response``."""

    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


# --- Конфигурация каналов -----------------------------------------------------


def test_ntfy_enabled_true_when_topic_set(monkeypatch):
    monkeypatch.setattr(notifier, "NTFY_TOPIC", "mytopic")
    assert notifier.ntfy_enabled() is True


def test_ntfy_enabled_false_when_topic_missing(monkeypatch):
    monkeypatch.setattr(notifier, "NTFY_TOPIC", "")
    assert notifier.ntfy_enabled() is False


def test_resend_enabled_true_when_configured(monkeypatch):
    monkeypatch.setattr(notifier, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(notifier, "NOTIFICATION_EMAIL", "me@example.com")
    assert notifier.resend_enabled() is True


def test_resend_enabled_false_when_config_missing(monkeypatch):
    monkeypatch.setattr(notifier, "RESEND_API_KEY", "")
    monkeypatch.setattr(notifier, "NOTIFICATION_EMAIL", "")
    assert notifier.resend_enabled() is False

# --- ntfy ----------------------------------------------------------------------


def test_send_ntfy_sync_posts_to_ntfy(monkeypatch):
    monkeypatch.setattr(notifier, "NTFY_TOPIC", "mytopic")
    monkeypatch.setattr(notifier, "NTFY_SERVER", "https://ntfy.example.com")

    with mock.patch("notifier.requests.post") as post:
        post.return_value = _FakeResponse(200)
        notifier._send_ntfy_sync("Title", "Message", priority="high")

        post.assert_called_once()
        assert post.call_args.args[0] == "https://ntfy.example.com/mytopic"
        assert post.call_args.kwargs["json"]["title"] == "Title"
        assert post.call_args.kwargs["json"]["message"] == "Message"
        assert post.call_args.kwargs["json"]["priority"] == "high"


def test_send_ntfy_sync_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(notifier, "NTFY_TOPIC", "")

    with mock.patch("notifier.requests.post") as post:
        notifier._send_ntfy_sync("Title", "Message")
        post.assert_not_called()


# --- Resend --------------------------------------------------------------------


def test_send_resend_sync_posts_to_resend(monkeypatch):
    monkeypatch.setattr(notifier, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(notifier, "NOTIFICATION_EMAIL", "me@example.com")
    monkeypatch.setattr(notifier, "NOTIFICATION_FROM", "from@example.com")

    with mock.patch("notifier.requests.post") as post:
        post.return_value = _FakeResponse(200)
        notifier._send_resend_sync("Subject", "<h1>Hi</h1>")

        post.assert_called_once()
        assert post.call_args.args[0] == "https://api.resend.com/emails"
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer re_test"
        assert post.call_args.kwargs["json"]["from"] == "from@example.com"
        assert post.call_args.kwargs["json"]["to"] == ["me@example.com"]
        assert post.call_args.kwargs["json"]["subject"] == "Subject"
        assert post.call_args.kwargs["json"]["html"] == "<h1>Hi</h1>"


def test_send_resend_sync_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(notifier, "RESEND_API_KEY", "")
    monkeypatch.setattr(notifier, "NOTIFICATION_EMAIL", "")

    with mock.patch("notifier.requests.post") as post:
        notifier._send_resend_sync("Subject", "<h1>Hi</h1>")
        post.assert_not_called()


# --- fire-and-forget отправка --------------------------------------------------


def test_send_notification_fires_ntfy_in_background(monkeypatch):
    monkeypatch.setattr(notifier, "NTFY_TOPIC", "mytopic")
    monkeypatch.setattr(notifier, "NTFY_SERVER", "https://ntfy.example.com")
    monkeypatch.setattr(notifier, "RESEND_API_KEY", "")
    monkeypatch.setattr(notifier, "NOTIFICATION_EMAIL", "")

    with mock.patch("notifier.requests.post") as post:
        post.return_value = _FakeResponse(200)

        async def run():
            await notifier.send_notification("Title", "Hello")
            await notifier.flush_notifications()

        asyncio.run(run())

        assert post.called
        assert post.call_args.args[0] == "https://ntfy.example.com/mytopic"


def test_send_notification_noop_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(notifier, "NTFY_TOPIC", "")
    monkeypatch.setattr(notifier, "RESEND_API_KEY", "")
    monkeypatch.setattr(notifier, "NOTIFICATION_EMAIL", "")

    with mock.patch("notifier.requests.post") as post:
        async def run():
            await notifier.send_notification("Title", "Hello")
            await notifier.flush_notifications()

        asyncio.run(run())
        post.assert_not_called()


def test_send_notification_swallows_http_errors(monkeypatch):
    monkeypatch.setattr(notifier, "NTFY_TOPIC", "mytopic")
    monkeypatch.setattr(notifier, "RESEND_API_KEY", "")
    monkeypatch.setattr(notifier, "NOTIFICATION_EMAIL", "")

    with mock.patch("notifier.requests.post") as post:
        post.return_value = _FakeResponse(500)

        async def run():
            await notifier.send_notification("Title", "Hello")
            await notifier.flush_notifications()

        asyncio.run(run())  # не должен бросать исключение


def test_send_notification_sends_resend_html(monkeypatch):
    monkeypatch.setattr(notifier, "NTFY_TOPIC", "")
    monkeypatch.setattr(notifier, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(notifier, "NOTIFICATION_EMAIL", "me@example.com")

    with mock.patch("notifier.requests.post") as post:
        post.return_value = _FakeResponse(200)

        async def run():
            await notifier.send_notification(
                "Subject", "Message", html="<h1>Report</h1>",
            )
            await notifier.flush_notifications()

        asyncio.run(run())

        assert post.called
        assert post.call_args.args[0] == "https://api.resend.com/emails"
        assert post.call_args.kwargs["json"]["html"] == "<h1>Report</h1>"

