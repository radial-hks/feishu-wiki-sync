#!/bin/bash
# 飞书 Wiki 定时同步流水线: 增量同步 + 清理 + git 提交
# 由 crontab 每小时调用，日志写 ~/wiki/.sync-cron.log
set -uo pipefail

SYNC_REPO=~/github/feishu-wiki-sync
WIKI=~/wiki
LOG="$WIKI/.sync-cron.log"

echo "=== $(date '+%F %T') 同步开始 ===" >> "$LOG"

# 1) 增量同步（云端为准 + prune 清理）
OUT=$(python3 "$SYNC_REPO/feishu_wiki_sync.py" sync --prune 2>>"$LOG")
RC=$?
echo "$OUT" >> "$LOG"
if [ $RC -ne 0 ]; then
    echo "$(date '+%F %T') 同步失败 rc=$RC，跳过 git 提交" >> "$LOG"
    exit $RC
fi

# 2) 有变化才提交（变更审计）
cd "$WIKI"
if [ -n "$(git status --porcelain)" ]; then
    SUMMARY=$(echo "$OUT" | tail -1)
    git add -A
    git commit -q -m "sync: $(date '+%F %H:%M') ${SUMMARY}"
    echo "$(date '+%F %T') git 已提交: ${SUMMARY}" >> "$LOG"
else
    echo "$(date '+%F %T') 无变化，跳过提交" >> "$LOG"
fi