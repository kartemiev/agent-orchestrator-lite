#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# OpenHands (agent-canvas) entrypoint wrapper (Lite-версия)
#
# Решает проблему прав в общем bind-mounted workspace, который делят:
#   * пользователь хоста (UID 1000)
#   * root-процессы (контейнер agent-orchestrator)
#   * пользователь этого контейнера openhands (UID 10001)
#
# Гарантирует umask 000 (новые файлы world-readable/writable) и что на каждом
# старте монтирование workspace остаётся «расслабленным» (chmod a+rwX + runs/ 0777).
# После настройки прав управление передаётся оригинальному entrypoint образа.
# ═══════════════════════════════════════════════════════════════════════════════
set -uo pipefail

umask 000

# ────────────────────────────────────────────────────────────────────────────
# Права на /home/openhands/.openhands (bind-mount /etc/openhands/config).
# Если хостовая директория /etc/openhands не существует, Docker создаёт её
# от root (UID 0), а Agent Server работает от UID 10001 и падает с
# PermissionError на старте. Переводим корень конфига во владение openhands.
# ────────────────────────────────────────────────────────────────────────────
if [ -d /home/openhands/.openhands ]; then
  if command -v sudo >/dev/null 2>&1; then
    sudo -n chown 10001:10001 /home/openhands/.openhands 2>/dev/null || true
  fi
  mkdir -p \
    /home/openhands/.openhands/agent-canvas \
    /home/openhands/.openhands/storage \
    /home/openhands/.openhands/workspaces 2>/dev/null || true
fi

if [ -d /app/workspace ]; then
  chmod -R a+rwX /app/workspace 2>/dev/null || true
  mkdir -p /app/workspace/runs 2>/dev/null || true
  chmod 0777 /app/workspace/runs 2>/dev/null || true
fi

exec tini -- /opt/agent-canvas/entrypoint.sh "$@"
