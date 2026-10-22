"""HITLWorkflow — базовый контур исполнения.

Единственный workflow: принимает готовый JSON-план, проходит Human-in-the-Loop
гейт (сигналы approve_plan_signal / reject_plan_signal, либо auto_approve) и
исполняет задачи волновым DAG-исполнителем через ``asyncio.gather``.

Из удалённого функционала (полная версия): Совет Старейшин, AITM, Swarm,
Dogfood, Sprint-оркестратор, LLM Planner, off-peak-пауза, супервизор,
Oscillation Guard, email/alerting. Здесь только строгое исполнение плана.
"""

import asyncio
import json
import os
from datetime import timedelta
from typing import List, Mapping, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

# --- Конфигурация (читается на этапе импорта — sandbox Temporal) ---
WORKSPACE_BASE = os.getenv("WORKSPACE_BASE", "/app/workspace")
WORKSPACE_RUNS_DIR = os.getenv("WORKSPACE_RUNS_DIR", f"{WORKSPACE_BASE}/runs")

# Ширина волны DAG (сколько агентских фаз стартует параллельно в одной волне).
WAVE_MAX_CONCURRENCY = int(os.getenv("WAVE_MAX_CONCURRENCY", "4"))
WAVE_MAX_CONCURRENCY_CEILING = 32

# Retry-политика OpenHands-фаз: разовые «спотыкания» LLM не роняют шаг сразу.
OPENHANDS_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=3,
)

# Retry-политика финальной доставки (git_push_and_create_pr) — идемпотентна.
GIT_PUSH_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=3,
)

# Уведомления — best-effort, без ретраев (не должны мешать основному контуру).
NOTIFY_RETRY_POLICY = RetryPolicy(maximum_attempts=1)


def resolve_wave_max_concurrency(plan, params=None) -> int:
    """Ширина волны DAG: ``params`` -> ``plan`` -> env ``WAVE_MAX_CONCURRENCY``."""
    for source in (params, plan):
        if not isinstance(source, Mapping):
            continue
        raw = source.get("wave_max_concurrency")
        if raw is None or raw == "":
            continue
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            continue
        return max(1, min(WAVE_MAX_CONCURRENCY_CEILING, value))
    return max(1, WAVE_MAX_CONCURRENCY)


def _normalize_tasks(tasks: List[dict]) -> List[dict]:
    """Нормализует задачи плана: гарантирует ``id`` и ``depends_on``."""
    normalized: List[dict] = []
    seen: set = set()
    for idx, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        t = dict(task)
        task_id = str(t.get("id") or "").strip()
        if not task_id or task_id in seen:
            task_id = f"task_{idx}"
            if task_id in seen:
                task_id = f"task_{idx}_{len(normalized)}"
        t["id"] = task_id
        seen.add(task_id)
        deps = t.get("depends_on") or []
        if not isinstance(deps, list):
            deps = [deps]
        t["depends_on"] = [str(d) for d in deps if d]
        t.setdefault("prompt", "")
        normalized.append(t)
    return normalized


@workflow.defn
class HITLWorkflow:
    def __init__(self):
        self.plan_approved: bool = False
        self.current_plan: dict = {}
        self.rejection_reason: str = ""
        self.auto_approve: bool = False

    @workflow.signal
    def approve_plan_signal(self, updated_plan: dict):
        self.current_plan = updated_plan
        self.plan_approved = True
        # Сбрасываем причину отклонения: повторное одобрение после reject
        # должно корректно очищать состояние (иначе гейт вернёт отказ).
        self.rejection_reason = ""

    @workflow.signal
    def reject_plan_signal(self, reason: str):
        self.rejection_reason = reason
        self.plan_approved = False

    @workflow.query
    def get_generated_plan(self) -> dict:
        return self.current_plan

    @workflow.query
    def is_waiting_for_approval(self) -> bool:
        return not self.plan_approved and len(self.current_plan) > 0 and not self.rejection_reason

    @workflow.run
    async def run(self, raw_prompt: str, params: Optional[dict] = None) -> str:
        _p = params or {}
        if bool(_p.get("auto_approve", False)):
            self.auto_approve = True
            self.plan_approved = True
            workflow.logger.info("Auto-approve включён — пропускаю ожидание подтверждения плана")

        # Строгий разбор JSON-плана (LLM Planner отсутствует).
        try:
            plan = json.loads(raw_prompt)
        except json.JSONDecodeError as exc:
            raise ApplicationError(
                f"Invalid JSON plan: {exc}",
                type="InvalidPlanError",
                non_retryable=True,
            )
        if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
            raise ApplicationError(
                "JSON plan must contain a 'tasks' list",
                type="InvalidPlanError",
                non_retryable=True,
            )
        self.current_plan = plan

        wave_concurrency = resolve_wave_max_concurrency(self.current_plan, _p)

        # Human-in-the-Loop гейт.
        if not self.auto_approve:
            await self._notify(
                "hitl",
                "⏳ Sprint awaiting approval",
                (
                    f"Workflow {workflow.info().workflow_id} ожидает одобрения плана. "
                    "Подтвердите или отклоните его в Streamlit UI."
                ),
                priority="high",
            )
        await workflow.wait_condition(
            lambda: self.plan_approved or self.rejection_reason != ""
        )
        if self.rejection_reason:
            return f"Workflow отменён пользователем: {self.rejection_reason}"

        tasks = _normalize_tasks(self.current_plan.get("tasks", []))
        if not tasks:
            return "План пуст, нет задач для выполнения."

        workspace_dir = f"{WORKSPACE_RUNS_DIR}/{workflow.info().workflow_id}"

        await workflow.execute_activity(
            "prepare_workspace",
            workspace_dir,
            start_to_close_timeout=timedelta(minutes=5),
        )

        completed: set = set()
        failed: dict = {}
        pending: dict = {t["id"]: t for t in tasks}
        all_results: list = []
        # Инициализируется ДО try/finally: на него опирается finally-блок.
        delivery_failed = False

        try:
            while pending:
                ready = [
                    t for t in pending.values()
                    if set(t.get("depends_on") or []).issubset(completed)
                ]
                if not ready:
                    if failed:
                        workflow.logger.warning(
                            "Пропускаю недостижимые задачи: %s (упавшие: %s)",
                            list(pending.keys()), list(failed.keys()),
                        )
                        break
                    raise ApplicationError(
                        f"Ошибка DAG: циклическая зависимость или недостижимый шаг! "
                        f"Зависшие задачи: {list(pending.keys())}",
                        type="RuntimeError",
                        non_retryable=True,
                    )

                workflow.logger.info(
                    "Волна: запуск %d параллельных задач: %s",
                    len(ready), [t["id"] for t in ready],
                )

                futures = []
                for task in ready:
                    step = dict(task)
                    step["workspace_dir"] = workspace_dir
                    step["workflow_id"] = workflow.info().workflow_id
                    futures.append(self._execute_node(step))

                for batch_start in range(0, len(ready), wave_concurrency):
                    batch = ready[batch_start:batch_start + wave_concurrency]
                    batch_futures = futures[batch_start:batch_start + wave_concurrency]
                    results = await asyncio.gather(*batch_futures, return_exceptions=True)
                    for task, result in zip(batch, results):
                        if isinstance(result, BaseException):
                            failed[task["id"]] = str(result)
                            del pending[task["id"]]
                            workflow.logger.error(
                                "Задача %s упала: %s", task["id"], result,
                            )
                        else:
                            completed.add(task["id"])
                            del pending[task["id"]]
                            all_results.append(result)
                            workflow.logger.info("Задача %s завершена", task["id"])

            # Финальная доставка (git push + PR) — только при отсутствии сбоев.
            pr_url = None
            if not failed:
                try:
                    push_result = await workflow.execute_activity(
                        "git_push_and_create_pr",
                        {
                            "workspace_dir": workspace_dir,
                            "workflow_id": workflow.info().workflow_id,
                            "prompt": raw_prompt,
                        },
                        start_to_close_timeout=timedelta(minutes=10),
                        retry_policy=GIT_PUSH_RETRY_POLICY,
                    )
                    all_results.append(push_result)
                    status = push_result.get("status") if isinstance(push_result, dict) else push_result
                    if isinstance(push_result, dict):
                        pr_url = push_result.get("pull_request_url")
                    workflow.logger.info("git_push_and_create_pr result: %s", status)
                except Exception as exc:
                    delivery_failed = True
                    workflow.logger.error("Финальная доставка не удалась: %s", exc)

            summary = [
                f"{idx}. {task.get('title') or task.get('id')}"
                for idx, task in enumerate(tasks, start=1)
                if task.get("id") in completed
            ]
            if failed:
                summary.append(f"FAILED: {len(failed)} task(s) — {', '.join(failed.keys())}")
            if delivery_failed:
                summary.append("FAILED: final delivery (git push / PR)")

            # Уведомления о результате спринта.
            wf_id = workflow.info().workflow_id
            if failed or delivery_failed:
                await self._notify(
                    "failure",
                    "❌ Sprint failed",
                    (
                        f"Workflow {wf_id} завершился с ошибкой: "
                        f"{len(failed)} задача(и) упала."
                        + (" Финальная доставка не удалась." if delivery_failed else "")
                    ),
                    priority="urgent",
                )
            elif pr_url:
                await self._notify(
                    "success",
                    "✅ Sprint delivered",
                    f"Workflow {wf_id} завершён. Pull Request: {pr_url}",
                    html=(
                        f"<h3>✅ Sprint delivered</h3>"
                        f"<p>Workflow <code>{wf_id}</code> завершён.</p>"
                        f"<p>Pull Request: <a href=\"{pr_url}\">{pr_url}</a></p>"
                    ),
                    priority="default",
                )

            return "DAG execution finished.\n" + "\n".join(summary)

        finally:
            preserve = bool(failed or delivery_failed)
            try:
                await workflow.execute_activity(
                    "cleanup_workspace",
                    args=[workspace_dir, preserve],
                    start_to_close_timeout=timedelta(minutes=2),
                )
            except Exception as exc:
                workflow.logger.warning("cleanup_workspace не выполнен: %s", exc)

    async def _execute_node(self, step: dict):
        """Исполняет один узел DAG. В Lite-версии движок единственный — OpenHands."""
        return await workflow.execute_activity(
            "execute_openhands_phase",
            step,
            start_to_close_timeout=timedelta(hours=8),
            heartbeat_timeout=timedelta(minutes=10),
            retry_policy=OPENHANDS_RETRY_POLICY,
        )

    async def _notify(self, event: str, title: str, message: str,
                      html=None, priority: str = "default") -> None:
        """Fire-and-forget уведомление; никогда не роняет workflow."""
        try:
            await workflow.execute_activity(
                "send_notification_activity",
                {
                    "event": event,
                    "title": title,
                    "message": message,
                    "html": html,
                    "priority": priority,
                },
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=NOTIFY_RETRY_POLICY,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning("Уведомление (%s) не отправлено: %s", event, exc)


