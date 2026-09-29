#!/bin/bash
# Mainspring 机枢 · 服务器侧每日备份
#
# 真值源 = ms.sqlite3（v2 起）。历史上这支脚本备份的是 wb.json —— 那是 v1 的快照，
# 2026-09-19 迁到 SQLite 后就再没变过，所以它此后每天备份的都是同一份老文件，
# 而真正的数据库一直没有任何备份。本版改用 SQLite 在线备份 API 取一致快照。
#
# crontab 用法（务必用绝对路径）：
#   10 4 * * * /path/to/mainspring/tools/backup.sh >> /path/to/mainspring/backup.log 2>&1
# 手动跑：
#   bash tools/backup.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"          # 本脚本在 tools/ 下，仓库根是上一级
cd "$ROOT" || exit 1

DB="$ROOT/ms.sqlite3"
DEST="$ROOT/backups"
KEEP=30
STAMP="$(date +%F)"
OUT="$DEST/ms_$STAMP.sqlite3"

PY="$ROOT/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

mkdir -p "$DEST"

"$PY" - "$DB" "$OUT" <<'PYCODE'
import os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
if not os.path.exists(src):
    sys.exit("找不到真值源：%s" % src)
tmp = dst + ".tmp"
con = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
bck = sqlite3.connect(tmp)
with bck:
    con.backup(bck)
ic = bck.execute("PRAGMA integrity_check").fetchone()[0]
n = bck.execute("SELECT count(*) FROM items").fetchone()[0]
ev = bck.execute("SELECT count(*) FROM events").fetchone()[0]
bck.close(); con.close()
if ic != "ok":
    os.unlink(tmp)
    sys.exit("快照 integrity_check 失败：%s" % ic)
os.replace(tmp, dst)
print("items=%d events=%d" % (n, ev))
PYCODE
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "$(date '+%F %T')  [FAIL] 备份失败 rc=$rc"
    exit "$rc"
fi

# 只留最近 KEEP 份（只清本脚本产出的 *.sqlite3，历史 *.json 一概不动）
ls -1t "$DEST"/ms_*.sqlite3 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "$(date '+%F %T')  [OK] 已备份 $(basename "$OUT")  $(du -h "$OUT" | cut -f1)  现存 $(ls -1 "$DEST"/ms_*.sqlite3 2>/dev/null | wc -l) 份"
