#!/usr/bin/env bash
# 从上游采集链路同步采集模块到本技能。
#
#   WECOM_CAPTURE_SRC=/path/to/upstream/skill bash scripts/vendor_wecom.sh
#
# 仓库维护用，不随专家包发布：采集模块已经随技能携带在 scripts/ 下，
# 日常安装与使用都不需要跑这个脚本，只有上游采集器更新时才重新同步一次。
set -euo pipefail

SRC="${WECOM_CAPTURE_SRC:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$SRC" ]; then
  echo "vendor: 需要指向上游技能目录（含 scripts/wecom 与 scripts/capture_chat.py）" >&2
  echo "vendor: WECOM_CAPTURE_SRC=<上游目录> bash scripts/vendor_wecom.sh" >&2
  exit 1
fi

if [ ! -f "${SRC}/scripts/capture_chat.py" ]; then
  echo "vendor: 在 ${SRC}/scripts 下找不到 capture_chat.py" >&2
  exit 1
fi

if [ ! -d "${SRC}/scripts/wecom" ]; then
  echo "vendor: 在 ${SRC}/scripts 下找不到 wecom/" >&2
  exit 1
fi

rm -rf "${HERE}/wecom"
cp -R "${SRC}/scripts/wecom" "${HERE}/wecom"
cp "${SRC}/scripts/capture_chat.py" "${HERE}/capture_chat.py"
cp "${SRC}/scripts/check_environment.py" "${HERE}/check_environment.py"
find "${HERE}/wecom" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# check_environment.py 是面向另一个场景写的，3 处措辞要在同步后改写成本项目口径。
# 替换不到就报错退出，避免上游改字后静默留下不相关的说明。
python3 - "${HERE}/check_environment.py" <<'PATCH'
import io
import re
import sys

path = sys.argv[1]
src = io.open(path, encoding="utf-8").read()

# 上游文案是另一个场景的，换成本项目口径
for old, new in [
    ("FAQ 知识库同步的本地环境只读自检", "企业微信采集的本地环境只读自检"),
    ("FAQ 知识库同步环境自检", "企业微信采集环境自检"),
]:
    if old not in src:
        sys.exit("vendor: 改写失败，上游文本已变：{}".format(old))
    src = src.replace(old, new)

# 台账示例路径换成本项目的 ~/worklog/ledger.json
src, n = re.subn(r"~/\.[A-Za-z0-9._-]+/ledger\.json", "~/worklog/ledger.json", src)
if n == 0:
    sys.exit("vendor: 改写失败，没找到台账示例路径")

io.open(path, "w", encoding="utf-8").write(src)
print("vendor: check_environment.py 已套用本地措辞")
PATCH

fail=0
count=0
for f in "${HERE}/wecom"/*.py "${HERE}"/*.py; do
  [ -f "$f" ] || continue
  count=$((count + 1))
  if ! python3 -m py_compile "$f" 2>/dev/null; then
    echo "vendor: py_compile FAIL ${f}" >&2
    fail=1
  fi
done
find "${HERE}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "vendor: checked ${count} python files"
if [ "$fail" -ne 0 ]; then
  echo "vendor: at least one file failed to compile" >&2
  exit 1
fi

echo "vendor: done -> ${HERE}"
echo "vendor: wecom files $(find "${HERE}/wecom" -type f | wc -l | tr -d ' ')"
echo ""
echo "next:"
echo "  python3 ${HERE}/capture_chat.py --store ~/wecom-data/wecom-chat-sales \\"
echo "      --singles \"client-a,client-b\" --start 2025-11-01 --prepare-only"
