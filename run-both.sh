#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$0")"

mkdir -p logs

# ── Cleanup (guard against re-entrant loops) ──
_cleanup_done=0
cleanup() {
    ((_cleanup_done)) && return
    _cleanup_done=1
    # Disable all further traps immediately
    trap '' EXIT INT TERM HUP
    echo "[cleanup] Stopping agents..."

    # Kill Python agents by command-line patterns (more reliable than PID trees)
    pkill -TERM -f "python main.py .*user-data-dir user-data-search" 2>/dev/null || true
    pkill -TERM -f "python main.py .*check-negotiations" 2>/dev/null || true
    sleep 2
    pkill -KILL -f "python main.py .*user-data-dir user-data-search" 2>/dev/null || true
    pkill -KILL -f "python main.py .*check-negotiations" 2>/dev/null || true

    # Direct Chrome cleanup
    pkill -9 -f "user-data-responder" 2>/dev/null || true
    pkill -9 -f "user-data-search" 2>/dev/null || true
    rm -f user-data-search/SingletonLock user-data-search/SingletonSocket 2>/dev/null || true
    rm -f user-data-responder/SingletonLock user-data-responder/SingletonSocket 2>/dev/null || true
    wait 2>/dev/null || true
    echo "[cleanup] Done."
}
trap 'cleanup' EXIT

# ── Pre-start cleanup ──
echo "Убиваю старые Chrome-процессы..."
pkill -9 -f "user-data-responder" 2>/dev/null || true
pkill -9 -f "user-data-search" 2>/dev/null || true
sleep 2
rm -f user-data-search/SingletonLock user-data-search/SingletonSocket 2>/dev/null || true
rm -f user-data-responder/SingletonLock user-data-responder/SingletonSocket 2>/dev/null || true
echo "Lock-файлы очищены."

# ── Responder Agent (bg, internal --loop) ──
python main.py \
  --check-negotiations \
  --auto-reply \
  --user-data-dir user-data-responder \
  --loop \
  -v \
  2>&1 | tee -a logs/responder.log &

RESPONDER_PID=$!

# ── Search Agent (bg subshell with loop) ──
(
  while true; do
    python main.py \
      --user-data-dir user-data-search \
      --applied-log applied-search.json \
      --no-post-search-responder \
      -v \
      2>&1 | tee -a logs/search.log || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Search batch finished, sleeping 3600s" >> logs/search.log
    sleep 3600
  done
) &

SEARCH_PID=$!

# ── Info ──
echo "Search agent PID:    $SEARCH_PID"
echo "Responder agent PID: $RESPONDER_PID"
echo "Logs:                logs/search.log  logs/responder.log"
echo "PGID:                $$"
echo ""

# ── Wait forever ──
wait
