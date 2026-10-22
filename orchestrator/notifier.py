"""Notifier — опциональные уведомления workflow (ntfy push + Resend email).

Best-effort модуль: если переменные окружения не заданы, вызовы молча
пропускаются; любые ошибки HTTP проглатываются (graceful fallback), чтобы
уведомления никогда не роняли основной workflow.

Отправка выполняется **fire-and-forget**: реальные HTTP-запросы уходят в фоновые
asyncio-таски, поэтому ``await send_notification(...)`` возвращается мгновенно и
не блокирует основной поток.

Каналы:
  - ntfy   — push-уведомления (``NTFY_SERVER`` / ``NTFY_TOPIC``);
  - Resend — HTML-отчёты на почту (``RESEND_API_KEY`` / ``NOTIFICATION_EMAIL``).
"""

import asyncio
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# --- Конфигурация (читается один раз на импорте) ---
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
NOTIFICATION_EMAIL = os.getenv("NOTIFICATION_EMAIL", "").strip()
NOTIFICATION_FROM = os.getenv("NOTIFICATION_FROM", "onboarding@resend.dev").strip()

_NOTIFIER_TIMEOUT_S = float(os.getenv("NOTIFIER_TIMEOUT_S", "5") or "5")

# Сильные ссылки на незавершённые фоновые таски — без этого asyncio может собрать
# их сборщиком мусора до завершения. Done-callback сам убирает задачу из набора.
_PENDING: set = set()


def ntfy_enabled() -> bool:
    """True, если для ntfy задан хотя бы topic."""
    return bool(NTFY_TOPIC)


def resend_enabled() -> bool:
    """True, если для Resend заданы API-ключ и адрес получателя."""
    return bool(RESEND_API_KEY and NOTIFICATION_EMAIL)


def _spawn(coro) -> "asyncio.Task":
    """Запускает корутину в фоне и гарантированно проглатывает любые ошибки."""
    loop = asyncio.get_running_loop()
    task = loop.create_task(coro)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)

    def _consume_exception(done: "asyncio.Task") -> None:
        try:
            done.result()
        except Exception:  # noqa: BLE001
            logger.debug("Фоновый таск Notifier упал", exc_info=True)

    task.add_done_callback(_consume_exception)
    return task


def _send_ntfy_sync(title: str, message: str, priority: str = "default") -> None:
    """Синхронный POST в ntfy (выполняется в фоновом потоке)."""
    if not ntfy_enabled():
        return
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    resp = requests.post(
        url,
        json={
            "topic": NTFY_TOPIC,
            "title": title,
            "message": message,
            "priority": priority,
        },
        timeout=_NOTIFIER_TIMEOUT_S,
    )
    resp.raise_for_status()


def _send_resend_sync(subject: str, html: str) -> None:
    """Синхронный POST в Resend (выполняется в фоновом потоке)."""
    if not resend_enabled():
        return
    resp = requests.post(
        "https://api.resend.com/emails",
        json={
            "from": NOTIFICATION_FROM,
            "to": [NOTIFICATION_EMAIL],
            "subject": subject,
            "html": html,
        },
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=_NOTIFIER_TIMEOUT_S,
    )
    resp.raise_for_status()


async def _ntfy_task(title: str, message: str, priority: str) -> None:
    try:
        await asyncio.to_thread(_send_ntfy_sync, title, message, priority)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ntfy-уведомление не отправлено: %s", exc)


async def _resend_task(subject: str, html: str) -> None:
    try:
        await asyncio.to_thread(_send_resend_sync, subject, html)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Resend-отчёт не отправлен: %s", exc)


async def send_notification(
    title: str,
    message: str,
    html: Optional[str] = None,
    priority: str = "default",
) -> None:
    """Fire-and-forget отправка уведомления по всем настроенным каналам.

    Планирует фоновые HTTP-запросы и сразу возвращается, не дожидаясь ответа
    внешних API. Если ни один канал не настроен — no-op. Никогда не поднимает
    исключение наружу.
    """
    if not (ntfy_enabled() or resend_enabled()):
        return
    if ntfy_enabled():
        _spawn(_ntfy_task(title, message, priority))
    if resend_enabled():
        _spawn(_resend_task(title, html if html is not None else f"<p>{message}</p>"))


async def flush_notifications() -> None:
    """Дожидается завершения всех фоновых тасков (используется в тестах)."""
    while _PENDING:
        tasks = list(_PENDING)
        await asyncio.gather(*tasks, return_exceptions=True)
