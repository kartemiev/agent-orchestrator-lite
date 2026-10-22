#!/usr/bin/env python3
"""capture-screenshots — автоснимки README с живого стека одной командой.

Скрипт обновляет изображения в ``docs/assets/``:

* ``streamlit-dashboard.png`` — главный экран Streamlit (JSON-план,
  ``Auto-approve``, кнопка запуска) в разрешении 1920x1080;
* ``dag-execution-view.png`` — блок мониторинга workflow: статус Temporal
  и раскрытый ``ExecutionPlan``;
* ``cli-diagnostics.png`` — терминальный рендер реального вывода
  ``agent-cli.py list`` / ``status`` / ``inspect``.

Два режима (определяются автоматически):

1. **С хоста** (обычный случай) — скрипт собирает артефакты (план, HTML
   терминала), поднимает Playwright в venv внутри контейнера ``openhands``,
   запускает съёмку там (у контейнера уже есть Chromium и доступ к сервисам
   Compose-сети по именам) и копирует PNG обратно в репозиторий.
2. **Внутри контейнера** (``--capture-only``) — только браузерная часть;
   используется оркестратором из режима 1.

Режим ``--runner host`` выполняет всё локально: нужны ``playwright`` и
Chromium на хосте (удобно в CI-образе с Playwright).

Примеры:

    python3 scripts/capture-screenshots.py
    python3 scripts/capture-screenshots.py --workflow-id direct-plan-1792655341
    python3 scripts/capture-screenshots.py --runner host --out-dir /tmp/shots
    python3 scripts/capture-screenshots.py --skip-cli --keep-workdir

Сторонние зависимости нужны только для браузерной части (Playwright);
оркестрация и сбор терминального HTML — на стандартной библиотеке.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

VERSION = "1.0.0"

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "assets"

VIEWPORT = {"width": 1920, "height": 1080}

# Внутри Compose-сети UI доступен по имени сервиса; хост-порт задаётся
# переменной STREAMLIT_PORT и нужен только в режиме --runner host.
STREAMLIT_INTERNAL_URL = "http://agent-ui:8501"

OPENHANDS_CONTAINER_DEFAULT = "openhands"
CONTAINER_WORKDIR = "/tmp/oh-capture"
CONTAINER_VENV = "/tmp/oh-capture-venv"

TERMINAL_STATUSES = {
    "RUNNING", "COMPLETED", "FAILED", "TERMINATED", "CANCELED", "CANCELLED",
    "TIMED_OUT", "CONTINUED_AS_NEW",
}

# Демонстрационный ExecutionPlan для поля ввода: git-sync, две параллельные
# задачи одной волны, затем проверка. Показывает и волны, и depends_on.
DEMO_PLAN = {
    "reasoning": "Healthcheck endpoint plus parallel docs, then verify and deliver.",
    "tasks": [
        {
            "id": "task_0_git_sync",
            "title": "Git Sync: clone the repository",
            "prompt": (
                "Clone https://github.com/your-org/your-app into the working "
                "directory and prepare the environment."
            ),
            "depends_on": [],
            "engine": "openhands",
        },
        {
            "id": "task_a_healthcheck",
            "title": "API: add GET /health",
            "prompt": 'Add a GET /health endpoint to app/main.py returning {"status": "ok"}.',
            "depends_on": ["task_0_git_sync"],
            "engine": "openhands",
        },
        {
            "id": "task_b_docs",
            "title": "Docs: document the healthcheck",
            "prompt": "Describe the GET /health endpoint in README.md.",
            "depends_on": ["task_0_git_sync"],
            "engine": "openhands",
        },
        {
            "id": "task_2_verify",
            "title": "Verify: pytest + ruff",
            "prompt": "Run pytest and ruff, fix any failures.",
            "depends_on": ["task_a_healthcheck", "task_b_docs"],
            "engine": "openhands",
        },
    ],
}

# Косметика: убираем из кадра панель Deploy и индикатор "Running…".
HIDE_CHROME_CSS = """
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"] { display: none !important; }
h3 { scroll-margin-top: 88px; }
"""


def log(message: str) -> None:
    print(f"[capture] {message}", flush=True)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def env_value(key: str, default: str | None = None) -> str | None:
    """Читает переменную из .env (без python-dotenv), затем из окружения."""
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    return os.environ.get(key, default)


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 180,
        check: bool = True, echo: bool = False) -> subprocess.CompletedProcess:
    kwargs = {} if echo else {"capture_output": True, "text": True}
    proc = subprocess.run(cmd, cwd=cwd, timeout=timeout, check=False, **kwargs)
    if check and proc.returncode != 0:
        details = "" if echo else (proc.stderr or proc.stdout or "").strip()
        raise SystemExit(
            f"error: команда завершилась с кодом {proc.returncode}: "
            f"{' '.join(map(str, cmd))}\n{details}"
        )
    return proc


def docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    return run(["docker", "info"], timeout=30, check=False).returncode == 0


def container_running(name: str) -> bool:
    proc = run(["docker", "inspect", "-f", "{{.State.Running}}", name],
               timeout=30, check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def host_streamlit_url() -> str:
    bind_ip = env_value("BIND_IP", "127.0.0.1")
    port = env_value("STREAMLIT_PORT", "8501")
    return f"http://{bind_ip}:{port}"


def cli_run(cli_args: list[str], timeout: int = 180) -> str:
    """Запускает agent-cli.py на хосте и возвращает stdout+stderr."""
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "agent-cli.py"), *cli_args]
    proc = run(cmd, cwd=REPO_ROOT, timeout=timeout, check=False)
    return ((proc.stdout or "") + (proc.stderr or "")).rstrip("\n")


def resolve_workflow_id(since: str) -> str:
    """Берёт самый свежий workflow из `agent-cli.py list`."""
    output = cli_run(["list", "--since", since, "--limit", "5"])
    for line in output.splitlines():
        match = re.match(r"^(\S+)\s{2,}(\S+)\s{2,}", line)
        if match and match.group(2) in TERMINAL_STATUSES:
            return match.group(1)
    return ""


# ---------------------------------------------------------------------------
# Терминальный рендер реального вывода CLI
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  html, body { margin:0; padding:0; background:#0b0f16; }
  body {
    width:1920px; height:1080px; display:flex; align-items:center;
    justify-content:center; box-sizing:border-box;
    font-family:'DejaVu Sans Mono', monospace;
  }
  .row { display:flex; align-items:flex-start; gap:24px; }
  .win { width:916px; border-radius:12px; overflow:hidden; background:#0d1117;
         border:1px solid #222a36; box-shadow:0 26px 60px rgba(0,0,0,.55); }
  .bar { height:42px; display:flex; align-items:center; padding:0 16px;
         background:#161c26; border-bottom:1px solid #222a36; }
  .dots { display:flex; gap:8px; }
  .dots i { width:12px; height:12px; border-radius:50%; }
  .dots i:nth-child(1) { background:#ff5f57; }
  .dots i:nth-child(2) { background:#febc2e; }
  .dots i:nth-child(3) { background:#28c840; }
  .title { flex:1; text-align:center; color:#8b96a5; font-size:13px; }
  .body { padding:16px 20px 20px; font-size:13.5px; line-height:1.42; }
  .cmd { color:#e6edf3; white-space:pre; }
  .user { color:#3fb950; }
  .path { color:#58a6ff; }
  .dollar { color:#8b96a5; }
  .bin { color:#e6edf3; }
  pre { margin:2px 0 14px; font:inherit; white-space:pre; color:#aab4c0; }
</style></head>
<body><div class="row">
  <div class="win">
    <div class="bar"><div class="dots"><i></i><i></i><i></i></div>
      <div class="title">agent-cli — list &amp; status — live stack</div></div>
    <div class="body">__LEFT__</div>
  </div>
  <div class="win">
    <div class="bar"><div class="dots"><i></i><i></i><i></i></div>
      <div class="title">agent-cli — inspect — DAG → OpenHands sessions</div></div>
    <div class="body">__RIGHT__</div>
  </div>
</div></body></html>
"""


def build_cli_html(workflow_id: str, since: str) -> str:
    """Вставляет фактический вывод CLI в терминальное оформление."""

    def column(commands: list[list[str]]) -> str:
        blocks = []
        for cli_args in commands:
            output = cli_run(cli_args)
            blocks.append(
                '<div class="cmd"><span class="user">developer</span>:'
                '<span class="path">~/agent-orchestrator-lite</span>'
                '<span class="dollar">$</span> <span class="bin">python3 '
                "scripts/agent-cli.py %s</span></div>\n<pre>%s</pre>"
                % (html.escape(" ".join(cli_args)), html.escape(output))
            )
        return "\n".join(blocks)

    left = column([
        ["list", "--since", since, "--limit", "3"],
        ["status", workflow_id],
    ])
    right = column([["inspect", workflow_id]])
    return _HTML_TEMPLATE.replace("__LEFT__", left).replace("__RIGHT__", right)


def write_artifacts(work_dir: Path, plan: dict, workflow_id: str, since: str,
                    skip_cli: bool) -> None:
    """Готовит входные файлы для браузерной части (её можно запускать в контейнере)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if not skip_cli and workflow_id:
        (work_dir / "cli.html").write_text(
            build_cli_html(workflow_id, since), encoding="utf-8")
    shutil.copy2(SCRIPT_PATH, work_dir / "capture.py")


# ---------------------------------------------------------------------------
# Браузерная часть (Playwright + системный Chromium)
# ---------------------------------------------------------------------------


def find_chromium(explicit: str | None) -> str:
    if explicit:
        return explicit
    from_env = os.environ.get("CAPTURE_CHROMIUM")
    if from_env:
        return from_env
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    raise SystemExit(
        "error: Chromium не найден. Укажите --chromium /path/to/chromium "
        "или задайте CAPTURE_CHROMIUM."
    )


def warn_missing_emoji_font() -> None:
    """Streamlit рисует эмодзи шрифтом ОС; без него они станут монохромными."""
    try:
        fonts = subprocess.run(["fc-list"], capture_output=True, text=True,
                               timeout=15).stdout
    except Exception:  # noqa: BLE001
        return
    if "emoji" not in fonts.lower():
        log("warning: emoji-шрифт не найден — эмодзи Streamlit будут монохромными "
            "(в openhands помогает fonts-noto-color-emoji в ~/.fonts)")


async def _screenshot(page, path: Path) -> None:
    await page.screenshot(path=str(path))
    log(f"saved {path} ({path.stat().st_size} B)")


async def _capture_async(*, work_dir: Path, out_dir: Path, base_url: str,
                         workflow_id: str | None, chromium: str | None,
                         skip_ui: bool, skip_cli: bool) -> None:
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    executable = find_chromium(chromium)
    log(f"chromium: {executable}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=executable,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-color-profile=srgb",
                "--font-render-hinting=none",
            ],
        )
        context = await browser.new_context(
            viewport=VIEWPORT, device_scale_factor=1, locale="ru-RU"
        )
        page = await context.new_page()
        page.set_default_timeout(60_000)
        try:
            if not skip_ui:
                await _capture_ui(page, base_url, workflow_id, work_dir, out_dir)
            if not skip_cli:
                await _capture_cli(page, work_dir, out_dir)
        finally:
            await context.close()
            await browser.close()


async def _capture_ui(page, base_url: str, workflow_id: str | None,
                      work_dir: Path, out_dir: Path) -> None:
    log(f"открываю {base_url}")
    await page.goto(base_url, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_selector("h1", timeout=90_000)
    await page.wait_for_timeout(4_000)  # websocket, шрифты, первая отрисовка
    await page.add_style_tag(content=HIDE_CHROME_CSS)

    plan = json.loads((work_dir / "plan.json").read_text(encoding="utf-8"))

    text_area = page.locator("textarea").first
    await text_area.wait_for(state="visible")
    await text_area.click()
    await text_area.fill(json.dumps(plan, ensure_ascii=False, indent=2))
    await text_area.evaluate("el => { el.scrollTop = 0; }")
    await page.wait_for_timeout(800)

    try:
        await page.locator('input[type="checkbox"]').first.check(
            force=True, timeout=5_000)
    except Exception:  # noqa: BLE001
        await page.get_by_text("Auto-approve", exact=False).first.click(
            timeout=5_000)

    await page.wait_for_timeout(1_500)
    await _screenshot(page, out_dir / "streamlit-dashboard.png")

    if not workflow_id:
        log("workflow_id не задан — пропускаю dag-execution-view.png")
        return

    wf_input = page.locator('input[type="text"]').first
    await wf_input.wait_for(state="visible", timeout=30_000)
    await wf_input.click()
    await wf_input.fill(workflow_id)
    await wf_input.press("Enter")
    try:
        await page.wait_for_selector("text=Статус Temporal Workflow", timeout=90_000)
    except Exception:  # noqa: BLE001
        log("warning: блок статуса не появился — снимаю как есть")
    await page.wait_for_timeout(3_000)  # запрос в Temporal + отрисовка JSON

    monitor_header = page.locator("h3", has_text="Мониторинг").first
    await monitor_header.evaluate("el => el.scrollIntoView({block: 'start'})")
    await page.wait_for_timeout(1_200)
    await _screenshot(page, out_dir / "dag-execution-view.png")


async def _capture_cli(page, work_dir: Path, out_dir: Path) -> None:
    cli_html = work_dir / "cli.html"
    if not cli_html.exists():
        log("cli.html отсутствует — пропускаю cli-diagnostics.png")
        return
    await page.goto(cli_html.resolve().as_uri(), wait_until="load", timeout=60_000)
    await page.wait_for_timeout(500)
    await _screenshot(page, out_dir / "cli-diagnostics.png")


# ---------------------------------------------------------------------------
# Оркестрация: хост -> контейнер -> PNG обратно
# ---------------------------------------------------------------------------


def bootstrap_container(args) -> None:
    """Поднимает venv с Playwright внутри контейнера (браузер не скачивается)."""
    venv = args.container_venv
    script = (
        "set -e; "
        f"if [ ! -x {venv}/bin/python ]; then "
        f"python3 -m venv {venv} && "
        f"{venv}/bin/pip -q --disable-pip-version-check install playwright; "
        "fi; "
        f"{venv}/bin/python -c 'import playwright'"
    )
    log(f"проверяю Playwright в контейнере {args.container}…")
    run(["docker", "exec", args.container, "bash", "-lc", script], timeout=900,
        echo=True)


def orchestrate_in_container(args, work_dir: Path) -> None:
    if not docker_ok():
        raise SystemExit("error: docker недоступен — используйте --runner host")
    if not container_running(args.container):
        raise SystemExit(
            f"error: контейнер '{args.container}' не запущен. "
            "Поднимите стек: docker compose up -d"
        )

    bootstrap_container(args)

    run(["docker", "exec", args.container, "bash", "-lc",
         f"rm -rf {CONTAINER_WORKDIR} && mkdir -p {CONTAINER_WORKDIR}"], timeout=60)
    run(["docker", "cp", f"{work_dir}/.", f"{args.container}:{CONTAINER_WORKDIR}"],
        timeout=120)

    cmd = [
        "docker", "exec", args.container,
        f"{args.container_venv}/bin/python", f"{CONTAINER_WORKDIR}/capture.py",
        "--capture-only",
        "--work-dir", CONTAINER_WORKDIR,
        "--out-dir", f"{CONTAINER_WORKDIR}/out",
        "--base-url", args.base_url,
    ]
    if args.workflow_id:
        cmd += ["--workflow-id", args.workflow_id]
    if args.chromium:
        cmd += ["--chromium", args.chromium]
    if args.skip_ui:
        cmd.append("--skip-ui")
    if args.skip_cli:
        cmd.append("--skip-cli")

    log(f"снимаю скриншоты внутри контейнера {args.container}")
    run(cmd, timeout=900, echo=True)

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    run(["docker", "cp", f"{args.container}:{CONTAINER_WORKDIR}/out/.", str(out_dir)],
        timeout=120)


def capture_on_host(args, work_dir: Path) -> None:
    try:
        import playwright  # noqa: F401
    except ModuleNotFoundError:
        raise SystemExit(
            "error: playwright не установлен на хосте. "
            "Установите его (pip install playwright) или запустите без --runner host."
        )
    warn_missing_emoji_font()
    asyncio.run(_capture_async(
        work_dir=work_dir,
        out_dir=Path(args.out_dir).resolve(),
        base_url=args.base_url,
        workflow_id=args.workflow_id,
        chromium=args.chromium,
        skip_ui=args.skip_ui,
        skip_cli=args.skip_cli,
    ))


# ---------------------------------------------------------------------------
# Проверка результата
# ---------------------------------------------------------------------------


def png_dimensions(path: Path) -> tuple[int, int] | None:
    header = path.read_bytes()[:24]
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", header[16:24])


def verify_outputs(out_dir: Path, expected: list[str]) -> int:
    failures = 0
    for name in expected:
        path = out_dir / name
        if not path.exists() or path.stat().st_size == 0:
            log(f"FAIL {path} — файл отсутствует или пуст")
            failures += 1
            continue
        dims = png_dimensions(path)
        if dims != (VIEWPORT["width"], VIEWPORT["height"]):
            log(f"FAIL {path} — {dims} вместо 1920x1080")
            failures += 1
            continue
        log(f"ok   {path} — 1920x1080, {path.stat().st_size} B")
    return failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capture-screenshots",
        description="Автоснимки README (Streamlit UI, монитор workflow, CLI) с живого стека.",
    )
    parser.add_argument("--version", action="version",
                        version=f"capture-screenshots {VERSION}")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                        help="каталог для PNG (по умолчанию docs/assets)")
    parser.add_argument("--workflow-id", default=None,
                        help="workflow для монитора и CLI "
                             "(по умолчанию — последний из agent-cli list)")
    parser.add_argument("--since", default="24h",
                        help="окно поиска workflow (по умолчанию 24h)")
    parser.add_argument("--plan-file", default=None,
                        help="JSON-план для поля ввода (по умолчанию демо-план)")
    parser.add_argument("--base-url", default=None,
                        help="URL Streamlit (по умолчанию http://agent-ui:8501 в "
                             "контейнере или http://$BIND_IP:$STREAMLIT_PORT на хосте)")
    parser.add_argument("--container", default=OPENHANDS_CONTAINER_DEFAULT,
                        help="контейнер для съёмки (по умолчанию openhands)")
    parser.add_argument("--container-venv", default=CONTAINER_VENV,
                        help=argparse.SUPPRESS)
    parser.add_argument("--chromium", default=None,
                        help="путь к Chromium (по умолчанию из PATH или $CAPTURE_CHROMIUM)")
    parser.add_argument("--runner", choices=("auto", "container", "host"), default="auto",
                        help="где запускать браузер: auto — контейнер, если доступен docker")
    parser.add_argument("--skip-ui", action="store_true", help="не снимать Streamlit")
    parser.add_argument("--skip-cli", action="store_true", help="не снимать терминал")
    parser.add_argument("--keep-workdir", action="store_true",
                        help="не удалять временный каталог с артефактами")
    parser.add_argument("--capture-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--work-dir", default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Внутренний режим: только браузерная часть (запускается в контейнере).
    if args.capture_only:
        warn_missing_emoji_font()
        asyncio.run(_capture_async(
            work_dir=Path(args.work_dir or CONTAINER_WORKDIR),
            out_dir=Path(args.out_dir),
            base_url=args.base_url or STREAMLIT_INTERNAL_URL,
            workflow_id=args.workflow_id,
            chromium=args.chromium,
            skip_ui=args.skip_ui,
            skip_cli=args.skip_cli,
        ))
        return 0

    if args.runner == "auto":
        args.runner = "container" if docker_ok() else "host"
    if args.base_url is None:
        args.base_url = (STREAMLIT_INTERNAL_URL if args.runner == "container"
                         else host_streamlit_url())

    if args.workflow_id is None:
        args.workflow_id = resolve_workflow_id(args.since)
        if args.workflow_id:
            log(f"workflow: {args.workflow_id}")
        else:
            log("warning: workflow не найден — снимки монитора и CLI будут пропущены")

    log(f"runner: {args.runner}, streamlit: {args.base_url}")

    plan = DEMO_PLAN
    if args.plan_file:
        plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))

    work_dir = Path(tempfile.mkdtemp(prefix="oh-shots-"))
    try:
        write_artifacts(work_dir, plan, args.workflow_id or "", args.since,
                        skip_cli=args.skip_cli or not args.workflow_id)
        if args.runner == "container":
            orchestrate_in_container(args, work_dir)
        else:
            capture_on_host(args, work_dir)

        expected: list[str] = []
        if not args.skip_ui:
            expected.append("streamlit-dashboard.png")
            if args.workflow_id:
                expected.append("dag-execution-view.png")
        if not args.skip_cli and args.workflow_id:
            expected.append("cli-diagnostics.png")

        log("проверяю результат:")
        failures = verify_outputs(Path(args.out_dir).resolve(), expected)
    finally:
        if args.keep_workdir:
            log(f"временный каталог сохранён: {work_dir}")
        else:
            shutil.rmtree(work_dir, ignore_errors=True)

    if failures:
        log(f"завершено с ошибками: {failures}")
        return 1
    log("готово")
    return 0


if __name__ == "__main__":
    sys.exit(main())





