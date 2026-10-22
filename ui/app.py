"""Streamlit Dashboard (Lite-версия).

Оставлено только:
  1. Поле ввода готового JSON-плана (с синтаксической валидацией);
  2. Чекбокс Auto-approve — прямая отправка на ``POST /api/v1/plan/direct``;
  3. Панель мониторинга статуса Temporal Workflow + кнопки сигналов
     ``approve_plan_signal`` / ``reject_plan_signal``.

Удалено: вкладка «Текстовое ТЗ / LLM Planner», проверка ключей DeepSeek,
редактор LLMPLANNER.md, Совет Старейшин.
"""

import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

import requests
import streamlit as st
from temporalio.client import Client

TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "temporal:7233")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://agent-orchestrator:8000")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ui")

st.set_page_config(page_title="Agent Orchestrator Lite", page_icon="🤖", layout="wide")

st.title("🤖 Agent Orchestrator Lite: Human-in-the-Loop Control Center")
st.caption("Вставь готовый JSON-план, при необходимости утверди и следи за исполнением.")


async def _get_temporal_client():
    return await Client.connect(TEMPORAL_HOST)


# ---------------------------------------------------------------------------
# Безопасный запуск async-функций внутри Streamlit (отдельный event loop).
# ---------------------------------------------------------------------------
_ui_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="st-ui")


def run_async(fn, *args, **kwargs):
    def _target():
        return asyncio.run(fn(*args, **kwargs))

    future = _ui_executor.submit(_target)
    return future.result(timeout=120)


# ---------------------------------------------------------------------------
# 1. Ввод готового JSON-плана
# ---------------------------------------------------------------------------
st.subheader("1. Готовый JSON-план")

json_str = st.text_area(
    "JSON план:",
    height=260,
    placeholder=(
        '{\n'
        '  "reasoning": "Обоснование плана",\n'
        '  "tasks": [\n'
        '    {\n'
        '      "id": "step_1",\n'
        '      "title": "Создать модель",\n'
        '      "prompt": "Создай Pydantic модель User",\n'
        '      "depends_on": [],\n'
        '      "model": "deepseek-v4-pro",\n'
        '      "engine": "openhands"\n'
        '    }\n'
        '  ]\n'
        '}'
    ),
)

auto_approve = st.checkbox(
    "⚡ Auto-approve (запустить без ручного подтверждения)",
    value=False,
    help="План утверждается автоматически — Human-in-the-Loop гейт пропускается.",
)

if st.button("⚡ Запустить с готовым JSON-планом", type="primary"):
    if not json_str.strip():
        st.error("Пожалуйста, вставьте JSON-план!")
    else:
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError as e:
            st.error(f"❌ Невалидный JSON! Проверьте синтаксис.\n\n```\n{e}\n```")
        else:
            if not isinstance(parsed.get("tasks"), list):
                st.error("❌ JSON-план должен содержать ключ `tasks` со списком шагов!")
            else:
                with st.spinner("🚀 Отправка плана в оркестратор..."):
                    try:
                        resp = requests.post(
                            f"{GATEWAY_URL}/api/v1/plan/direct",
                            json={"raw_json": json_str, "auto_approve": auto_approve},
                            timeout=30,
                        )
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as exc:
                        st.error(f"❌ Не удалось запустить workflow: {exc}")
                    else:
                        wf_id = data.get("workflow_id")
                        st.session_state["current_wf_id"] = wf_id
                        st.session_state["active_wf_input"] = wf_id
                        st.success(
                            f"🎯 Запущен workflow!\n\n"
                            f"ID: `{wf_id}` | Фаз: {len(parsed['tasks'])}"
                            + (" | ⚡ Auto-approve" if auto_approve else "")
                        )

st.divider()

# ---------------------------------------------------------------------------
# 2. Мониторинг workflow и сигналы approve/reject
# ---------------------------------------------------------------------------
st.subheader("2. Мониторинг и управление workflow")

active_wf_id = st.text_input("Workflow ID для управления:", key="active_wf_input")

if active_wf_id:
    async def inspect_and_signal(action: str, updated_plan=None):
        client = await _get_temporal_client()
        handle = client.get_workflow_handle(active_wf_id)

        if action == "query":
            desc = await handle.describe()
            waiting = await handle.query("is_waiting_for_approval")
            plan = await handle.query("get_generated_plan")
            return desc.status.name, waiting, plan
        elif action == "approve":
            await handle.signal("approve_plan_signal", updated_plan)
        elif action == "reject":
            await handle.signal("reject_plan_signal", "Rejected by user from UI")

    try:
        status_name, is_waiting, plan = run_async(inspect_and_signal, "query")

        st.info(f"**Статус Temporal Workflow:** `{status_name}`")

        if is_waiting:
            st.warning("⏸️ Workflow приостановлен и ожидает вашего утверждения!")
            st.json(plan)

            col_btn1, col_btn2 = st.columns(2)
            with col_btn1:
                if st.button("✅ APPROVE & DISPATCH (Утвердить и запустить)", type="primary"):
                    try:
                        run_async(inspect_and_signal, "approve", plan)
                        st.success("План утверждён! Агенты приступили к выполнению.")
                    except Exception as exc:
                        st.error(f"❌ Не удалось отправить сигнал аппрува: {exc}")
                    st.rerun()
            with col_btn2:
                if st.button("❌ Отклонить запуск"):
                    try:
                        run_async(inspect_and_signal, "reject")
                        st.error("Workflow отменён.")
                    except Exception as exc:
                        st.error(f"❌ Не удалось отправить сигнал отклонения: {exc}")
                    st.rerun()
        else:
            if plan:
                st.success("План утверждён / workflow в процессе выполнения или завершён.")
                st.json(plan)
            else:
                st.info("⏳ План ещё не доступен (workflow стартует).")
    except Exception as e:
        logger.error("UI failed to query workflow %s: %s", active_wf_id, e, exc_info=True)
        st.error(f"Не удалось получить данные по Workflow `{active_wf_id}`: {e}")

