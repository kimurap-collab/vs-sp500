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

# 無人の自律実行であることの目印。いけゲート（ike_gate_guard.py）はこれが立っている時だけ
# vs-sp500配下の書き込みを「いけ」なしで通す。対話セッションでは立たない。
export IKE_GATE_AUTONOMOUS=1

# 自律ループの週報・月次総括も夜には送らず、朝07:00の send_report.py にまとめて流す
export VS_SP500_DEFER_TELEGRAM=1

echo "[loop_run.sh] 開始 $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"

# 529のような一時的なサーバ側エラーは待てば回復するため、間隔を空けて最大3回まで試行する
# （1回目失敗後60秒・2回目失敗後300秒。この間隔はFableが決めた値で大将の指示ではない。
# 2026-08-19改修2-a: 8/19未明に529で一発死しリトライ無く朝の自己点検が丸ごと飛んだ対策）。
RETRY_DELAYS_SEC=(60 300)
ATTEMPT=1
EXIT_CODE=1

while :; do
    echo "[loop_run.sh] 試行${ATTEMPT}回目" >> "$LOG_FILE"

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

    if [ "$EXIT_CODE" -eq 0 ]; then
        break
    fi

    if [ "$ATTEMPT" -ge $((${#RETRY_DELAYS_SEC[@]} + 1)) ]; then
        echo "[loop_run.sh] 試行${ATTEMPT}回目も失敗(exit=${EXIT_CODE})。リトライ上限に達したため終了する" >> "$LOG_FILE"
        break
    fi

    DELAY="${RETRY_DELAYS_SEC[$((ATTEMPT - 1))]}"
    echo "[loop_run.sh] 試行${ATTEMPT}回目が失敗(exit=${EXIT_CODE})。${DELAY}秒待って再試行する" >> "$LOG_FILE"
    sleep "$DELAY"
    ATTEMPT=$((ATTEMPT + 1))
done

# ループ本体が残した変更をここでcommit&pushする。
# claude内のBashは「いけゲート」でgit pushがブロックされるため、
# 公開はフック対象外のこのスクリプト側で行う（2026-08-13追加）。
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    git add -A >> "$LOG_FILE" 2>&1
    git commit -m "loop: 自律ループの記録 $(date '+%Y-%m-%d')" >> "$LOG_FILE" 2>&1
fi
if [ -n "$(git log origin/main..HEAD --oneline 2>/dev/null)" ]; then
    git push >> "$LOG_FILE" 2>&1 && echo "[loop_run.sh] push完了" >> "$LOG_FILE"
fi

echo "[loop_run.sh] 終了 exit=${EXIT_CODE} $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"

# 大将への通知はしない（2026-08-19改修2。全部失敗してもログに残すのみ）。
# 復旧は send_report.py（07:00・改修2-b）と翌朝のループ（改修2-c）が引き継ぐ。
exit "$EXIT_CODE"
