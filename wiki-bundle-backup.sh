#!/bin/bash
# ~/wiki 每日 git bundle 备份到 /mnt/g（服务器迁移前的过渡备份）
set -uo pipefail
DEST=/mnt/g/CodeSpace/Backups/feishu-wiki-sync
STAMP=$(date '+%Y%m%d')
WIKI=~/wiki

cd "$WIKI" || exit 1
mkdir -p "$DEST"
# 先提交未入库变更（cron 同步产物），再打 bundle
if [ -n "$(git status --porcelain)" ]; then
    git add -A && git commit -q -m "backup: pre-bundle commit ${STAMP}"
fi
git bundle create "$DEST/wiki-${STAMP}.bundle" --all 2>/dev/null
# 只保留最近 7 天的 bundle
ls -t "$DEST"/wiki-*.bundle 2>/dev/null | tail -n +8 | xargs -r rm -f
echo "$(date '+%F %T') bundle: wiki-${STAMP}.bundle ($(du -h "$DEST/wiki-${STAMP}.bundle" | cut -f1))" >> "$DEST/backup.log"