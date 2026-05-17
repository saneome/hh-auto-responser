#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$0")"

# Ensure logs dir exists
mkdir -p logs

# Activate venv
VENV_BIN="./.venv/bin"
if [ ! -f "${VENV_BIN}/activate" ]; then
    echo "ОШИБКА: venv не найден. Запустите: python3 -m venv .venv && pip install -r requirements.txt"
    exit 1
fi
source "${VENV_BIN}/activate"

# Check display (GUI needs X11/Wayland)
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "ПРЕДУПРЕЖДЕНИЕ: DISPLAY не задан. GUI может не открыться."
fi

# Kill stale Chrome + SingletonLock (same as run-both.sh)
kill_stale_chrome() {
    local profile_dir="$1"
    if [ -d "$profile_dir" ]; then
        local lock="${profile_dir}/SingletonLock"
        local cookie="${profile_dir}/SingletonCookie"
        local socket="${profile_dir}/SingletonSocket"
        if [ -f "$lock" ] || [ -f "$cookie" ] || [ -f "$socket" ]; then
            echo "[cleanup] Removing stale locks from ${profile_dir}"
            rm -f "$lock" "$cookie" "$socket" 2>/dev/null || true
        fi
    fi
}

kill_stale_chrome "user-data-search"
kill_stale_chrome "user-data-responder"

# Launch GUI (no exec — keep shell for error visibility)
echo "[$(date '+%H:%M:%S')] Запуск GUI..."
python main.py --gui "$@" 2>&1 | tee -a logs/gui.log
EXIT_CODE=${PIPESTATUS[0]}
echo "[$(date '+%H:%M:%S')] GUI завершён (код ${EXIT_CODE})"
exit ${EXIT_CODE}
