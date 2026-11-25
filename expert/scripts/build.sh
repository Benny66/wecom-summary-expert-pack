#!/usr/bin/env bash
# 组装并打包 WorkBuddy 专家包。
#
#   bash scripts/build.sh                 # 组装 + zip + 自检
#   SKILL_SRC=/path/to/skill bash scripts/build.sh
#
# 技能正文不在本仓库维护：构建时从 ../skill 复制到 skills/sales-worklog/，避免两份副本漂移。
set -euo pipefail

NAME="sales-worklog-expert"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_SRC="${SKILL_SRC:-$ROOT/../skill}"
BUILD="$ROOT/build/$NAME"
VERSION="$(python3 -c "import json;print(json.load(open('$ROOT/.codebuddy-plugin/plugin.json'))['version'])")"
ZIP="$ROOT/$NAME-$VERSION.zip"

if [ ! -f "$SKILL_SRC/SKILL.md" ]; then
  echo "找不到技能源：$SKILL_SRC/SKILL.md" >&2
  exit 1
fi

if [ ! -f "$ROOT/avatars/expert.png" ]; then
  echo "== 生成头像 =="
  python3 "$ROOT/scripts/make_avatar.py"
fi

echo "== 组装 $BUILD =="
rm -rf "$ROOT/build"
mkdir -p "$BUILD/skills"

cp -R "$ROOT/.codebuddy-plugin" "$BUILD/.codebuddy-plugin"
cp -R "$ROOT/agents" "$BUILD/agents"
cp -R "$ROOT/avatars" "$BUILD/avatars"
[ -f "$ROOT/README.md" ] && cp "$ROOT/README.md" "$BUILD/README.md"

# 技能正文：只带运行必需的文件。开发期产物（fixtures、selftest、test-prompts、上游同步脚本）不进包。
rsync -a --exclude '.git' --exclude 'test-prompts.json' --exclude '.DS_Store' \
      --exclude '__pycache__' --exclude 'fixtures' --exclude 'selftest.sh' \
      --exclude 'vendor_wecom.sh' \
      --exclude 'generated-images' \
      "$SKILL_SRC/" "$BUILD/skills/sales-worklog/"

echo "== 打包 $ZIP =="
rm -f "$ZIP"
( cd "$ROOT/build" && zip -rqX "$ZIP" "$NAME" \
    -x "*/.DS_Store" -x "*__MACOSX*" -x "*._*" -x "*/.git/*" -x "*.zip" \
       -x "*/CHANGELOG.md" -x "*/__pycache__/*" -x "*/fixtures/*" )

echo "== 隐藏文件核对 =="
# 先把清单落到变量里再匹配：pipefail 下 grep -q 提前退出会让 unzip 收到 SIGPIPE，误报缺失
LIST="$(unzip -l "$ZIP")"
for f in "$NAME/.codebuddy-plugin/plugin.json" "$NAME/agents/sales-worklog-expert.md" \
         "$NAME/skills/sales-worklog/SKILL.md"; do
  printf '%s\n' "$LIST" | grep -q -- "$f" || { echo "ZIP 缺少 $f" >&2; exit 1; }
done

echo "== 自检 =="
python3 "$ROOT/scripts/check_package.py" --zip "$ZIP"

echo "完成：$ZIP"
