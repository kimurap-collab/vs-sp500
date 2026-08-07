#!/bin/bash
# vs-sp500: 毎朝7:15の自律ループ起動スクリプト。
# claude -p にLOOP_PROMPT.mdを渡し、書き込み系ツールを --allowedTools で個別許可して
# ヘッドレス実行する（--dangerously-skip-permissionsは使用しない）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

mkdir -p logs/loop

DATE_STR="$(date +%Y%m%d)"
LOG_FILE="logs/loop/run_${DATE_STR}.log"
TIMEOUT_SECONDS=900

CLAUDE_BIN="$(command -v claude || echo /Users/seijikimura/.local/bin/claude)"

echo "[loop_run.sh] 開始 $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"

"$CLAUDE_BIN" -p "$(cat LOOP_PROMPT.md)" \
    --allowedTools "Read" "Glob" "Grep" "Bash" "Write" "Edit" \
    >> "$LOG_FILE" 2>&1 &
CLAUDE_PID=$!

(
    sleep "$TIMEOUT_SECONDS"
    if kill -0 "$CLAUDE_PID" 2>/dev/null; then
        echo "[loop_run.sh] タイムアウト(${TIMEOUT_SECONDS}秒)のため強制終了" >> "$LOG_FILE"
        kill -9 "$CLAUDE_PID" 2>/dev/null
    fi
) &
WATCHER_PID=$!

wait "$CLAUDE_PID"
EXIT_CODE=$?

kill "$WATCHER_PID" 2>/dev/null
wait "$WATCHER_PID" 2>/dev/null

echo "[loop_run.sh] 終了 exit=${EXIT_CODE} $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"

if [ "$EXIT_CODE" -ne 0 ]; then
    python3 -c "import report; report.send_telegram_message('⚠️ 自律ループ実行失敗（exit ${EXIT_CODE}）。${LOG_FILE} を確認')"
fi

exit "$EXIT_CODE"
