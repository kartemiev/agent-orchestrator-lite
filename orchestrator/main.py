"""FastAPI Gateway (Lite-версия).

Оставлены только:
  - GET  /health              — healthcheck;
  - POST /api/v1/plan/direct  — прямой запуск workflow по готовому JSON-плану.

Удалено (полная версия): GitHub webhook, Canvas bridge, LLM Planner.
"""

import json
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from temporalio.client import Client

logger = logging.getLogger(__name__)

app = FastAPI(title="Agent Orchestrator Lite API")

TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "temporal:7233")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/plan/direct")
async def plan_direct(request: Request):
    """Запускает EnterpriseHITLWorkflow по переданному JSON-плану.

    Принимает JSON-тело. План передаётся либо строкой в ключе ``raw_json``,
    либо самим объектом (с ключом ``tasks``). ``auto_approve=true`` пропускает
    Human-in-the-Loop гейт.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "reason": "Request body must be valid JSON"},
        )

    plan_json_str = None
    if isinstance(payload.get("raw_json"), str):
        plan_json_str = payload["raw_json"]
    elif isinstance(payload, dict) and "tasks" in payload:
        plan_json_str = json.dumps(payload)
    else:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "reason": (
                    "Provide the plan as a 'raw_json' string key, "
                    "or the plan object itself (with a 'tasks' key)"
                ),
            },
        )

    # Валидация: это корректный JSON и содержит "tasks".
    try:
        parsed = json.loads(plan_json_str)
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "reason": "Provided JSON string is not valid JSON"},
        )
    if not isinstance(parsed.get("tasks"), list):
        return JSONResponse(
            status_code=400,
            content={"status": "error", "reason": "JSON must contain a 'tasks' list"},
        )

    auto_approve = bool(payload.get("auto_approve", False))

    workflow_params: dict = {}
    if auto_approve:
        workflow_params["auto_approve"] = True
    repo_url = payload.get("repo_url") or parsed.get("repo_url") or parsed.get("repo") or ""
    if repo_url:
        workflow_params["repo_url"] = repo_url
    if "wave_max_concurrency" in payload:
        workflow_params["wave_max_concurrency"] = payload["wave_max_concurrency"]

    wf_id = f"direct-plan-{int(time.time())}"
    client = await Client.connect(TEMPORAL_HOST)
    wf_args = [plan_json_str, workflow_params] if workflow_params else [plan_json_str]
    await client.start_workflow(
        "EnterpriseHITLWorkflow",
        args=wf_args,
        id=wf_id,
        task_queue="agent-tasks-queue",
    )

    return {
        "status": "triggered",
        "workflow_id": wf_id,
        "mode": "direct_json_plan",
        "task_count": len(parsed["tasks"]),
        "auto_approve": auto_approve,
    }
