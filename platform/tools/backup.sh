#!/usr/bin/env bash
# SmartLabHub 数据备份
#
# 备份什么（都是只在服务器本机、丢了就没了的东西）：
#   1. 账号库 —— SQLite，用 .backup 做在线备份，不会拷到写了一半的文件
#   2. 集成凭据 —— integrations.json（Salesforce sid / 钉钉密钥）
#   3. 业务数据 —— 导入的源文件、索引、LIS 记录、导出结果
#
# ⚠️ 备份和源数据在同一块盘上，盘坏了会一起没。请定期把 backups/ 同步到别的机器。
#
# 用法：
#   ./platform/tools/backup.sh              # 正常备份
#   SLH_BACKUP_KEEP=30 ./platform/tools/backup.sh   # 保留 30 份
set -euo pipefail

# 脚本所在目录 → 项目根（platform/tools/ 的上两级）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DEST="${SLH_BACKUP_DIR:-$ROOT/backups}"
KEEP="${SLH_BACKUP_KEEP:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP="$DEST/.tmp-$STAMP"

ACCOUNTS_DB="$ROOT/platform/backend/department_platform.db"
INTEGRATIONS="$ROOT/platform/data/integrations.json"
DATA_DIR="$ROOT/modules/data_analysis/data"

mkdir -p "$DEST" "$TMP"

# 1) 账号库
if [ -f "$ACCOUNTS_DB" ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$ACCOUNTS_DB" ".backup '$TMP/accounts.db'"
  else
    # 没装 sqlite3 就退回文件拷贝（服务在写时可能不一致，建议装 sqlite3）
    echo "[WARN] 未安装 sqlite3，退化为直接拷贝（建议 apt install sqlite3）" >&2
    cp "$ACCOUNTS_DB" "$TMP/accounts.db"
  fi
fi

# 2) 集成凭据
[ -f "$INTEGRATIONS" ] && cp "$INTEGRATIONS" "$TMP/" || true

# 3) 业务数据
if [ -d "$DATA_DIR" ]; then
  tar -czf "$TMP/data.tar.gz" -C "$ROOT" "modules/data_analysis/data"
fi

# 4) 打包
ARCHIVE="$DEST/smartlabhub-$STAMP.tar.gz"
tar -czf "$ARCHIVE" -C "$TMP" .
rm -rf "$TMP"

# 5) 只留最近 N 份
ls -1t "$DEST"/smartlabhub-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
echo "backup ok: $ARCHIVE ($SIZE)"
