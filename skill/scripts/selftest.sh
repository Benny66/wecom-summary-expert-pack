#!/usr/bin/env bash
# 用假数据自检：窗口切片、渲染、台账校验（含金额造假必须被拦）。
# 用法：bash selftest.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FIXTURE="${HERE}/fixtures/wecom-chat-demo"
OUT="$(mktemp -d)"
FAIL=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAIL=1; }

echo "== 1. 切窗口 =="
python3 "${HERE}/prepare_window.py" --store "$FIXTURE" \
  --since 2026-11-19 --until 2026-11-25 --out "$OUT" >"${OUT}/run.log" 2>&1
if [ $? -ne 0 ]; then
  fail "prepare_window 退出码非 0"
  cat "${OUT}/run.log"
  echo ""
  echo "自检中止：窗口都没生成，后面的不用看了。"
  exit 1
fi
cat "${OUT}/run.log"

[ -f "${OUT}/window.md" ] && pass "生成 window.md" || fail "缺 window.md"
[ -f "${OUT}/window.json" ] && pass "生成 window.json" || fail "缺 window.json"
[ -f "${OUT}/window.msgs.jsonl" ] && pass "生成 window.msgs.jsonl" || fail "缺 window.msgs.jsonl"

grep -q "消息 21 条" "${OUT}/run.log" && pass "窗口内 21 条" || fail "消息数不是 21"
grep -q "会话 5（单聊 3 / 群 2）" "${OUT}/run.log" && pass "5 个会话（单聊 3 / 群 2）" || fail "会话数不是 5"
if grep -q "消息 21 条（我 7 / 对方 14）" "${OUT}/window.md"; then
  pass "方向判定：我 7 / 对方 14"
else
  fail "方向判定不对，检查 is_mine / --self-names"
fi
if grep -q "群聊分边（群消息 9 条）：我 2 ｜ 我方同事 1 ｜ 客户方 3 ｜ 未标注 3" "${OUT}/window.md"; then
  pass "群聊分边：我方同事/客户方/未标注 分开统计"
else
  fail "群聊分边统计不对"
fi

echo ""
echo "== 1b. 快捷周期参数 =="
python3 "${HERE}/prepare_window.py" --store "$FIXTURE" \
  --month 2026-11 --out "${OUT}/m" >"${OUT}/m.log" 2>&1
if [ $? -eq 0 ]; then
  python3 - "${OUT}/m/window.json" <<'PY'
import json, sys
w = json.load(open(sys.argv[1], encoding="utf-8"))
win, tot = w["window"], w["totals"]
ok = (win["since"], win["until"]) == ("2026-11-01", "2026-11-30")
print("PASS  --month 2026-11 → %s ~ %s" % (win["since"], win["until"]) if ok
      else "FAIL  --month 2026-11 得到 %s ~ %s" % (win["since"], win["until"]))
ok2 = tot["messages"] == 23
print("PASS  --month 切出 23 条（比 --since 11-19 多出 11-18、11-15 两条）" if ok2
      else "FAIL  --month 切出 %d 条（应为 23）" % tot["messages"])
ok3 = win["ref_date"] <= win["until"]
print("PASS  ref_date(%s) 不晚于周期末" % win["ref_date"] if ok3
      else "FAIL  ref_date=%s 晚于 until=%s" % (win["ref_date"], win["until"]))
sys.exit(0 if (ok and ok2 and ok3) else 1)
PY
  [ $? -eq 0 ] || FAIL=1
else
  fail "--month 跑不起来"
  cat "${OUT}/m.log"
fi

python3 "${HERE}/prepare_window.py" --store "$FIXTURE" \
  --week 2026-W48 --out "${OUT}/w" >"${OUT}/w.log" 2>&1
if [ $? -eq 0 ]; then
  python3 - "${OUT}/w/window.json" <<'PY'
import json, sys
w = json.load(open(sys.argv[1], encoding="utf-8"))
win, tot = w["window"], w["totals"]
ok = (win["since"], win["until"]) == ("2026-11-23", "2026-11-29")
print("PASS  --week 2026-W48 → %s ~ %s（周一起）" % (win["since"], win["until"]) if ok
      else "FAIL  --week 2026-W48 得到 %s ~ %s" % (win["since"], win["until"]))
ok2 = tot["messages"] == 1
print("PASS  --week 只切出 11-23 那 1 条" if ok2
      else "FAIL  --week 切出 %d 条（应为 1）" % tot["messages"])
sys.exit(0 if (ok and ok2) else 1)
PY
  [ $? -eq 0 ] || FAIL=1
else
  fail "--week 跑不起来"
  cat "${OUT}/w.log"
fi

echo ""
echo "== 2. 结构信号 =="
python3 - "$OUT/window.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
sig = data["signals"]
expect = {
    "awaiting_me": {"王五", "张三(某科技)", "灵动科技项目对接群(外部)"},
    "awaiting_client": {"李四"},
    "silent": set(),
    # 分边全空的群不许下「谁欠谁」的结论，只能挂到待人工确认
    "side_unknown": {"宏远集团上线群"},
    "awaiting_check": {"宏远集团上线群"},
}
bad = 0
for key, want in expect.items():
    got = set(sig.get(key) or [])
    if got == want:
        print("PASS  signals.%s = %s" % (key, sorted(got)))
    else:
        print("FAIL  signals.%s 期望 %s 实际 %s" % (key, sorted(want), sorted(got)))
        bad = 1
cov = data["coverage"]
if cov["incomplete"] == ["王五"]:
    print("PASS  coverage.incomplete = ['王五']")
else:
    print("FAIL  coverage.incomplete 实际 %s" % (cov["incomplete"],))
    bad = 1
if (cov["groups"], cov["customer_groups"], cov["groups_side_unknown"]) == (2, 1, 1):
    print("PASS  coverage 群统计：群 2 / 外部群 1 / 分边未知 1")
else:
    print("FAIL  coverage 群统计 实际 %s" % ({k: cov[k] for k in
          ("groups", "customer_groups", "groups_side_unknown")},))
    bad = 1
if "宏远集团上线群" not in (sig.get("awaiting_me") or []) and "宏远集团上线群" not in (sig.get("silent") or []):
    print("PASS  分边未知的群没有被算成「客户在等」或「客户零回复」")
else:
    print("FAIL  分边未知的群被下了不该下的结论")
    bad = 1
sys.exit(bad)
PY
[ $? -eq 0 ] || FAIL=1

echo ""
echo "== 3. 渲染检查 =="
if grep -q '\[quote\]' "${OUT}/window.md"; then
  fail "md 里还有 [quote] 标记"
else
  pass "引用消息没有多余的 [quote] 标记"
fi
if grep -q 'has_media' "${OUT}/window.md"; then
  fail "md 里出现 has_media（fixture 没有图片/文件）"
else
  pass "has_media 没有误报"
fi
GLUED=$(grep -B1 '^### ' "${OUT}/window.md" | grep -c '^- `' || true)
if [ "${GLUED}" = "0" ]; then
  pass "日期标题前有空行"
else
  fail "有 ${GLUED} 处日期标题粘在消息行后面"
fi
if grep -qF '[引用「报价单发您了，年费 28 万，含 3 个环境」] 28 万有点超预算了' "${OUT}/window.md"; then
  pass "引用消息渲染成「引用段 + 回复段」"
else
  fail "引用消息渲染不符合预期"
fi

echo ""
echo "== 3b. 群聊渲染 =="
if grep -qF '**王经理（客户方）**' "${OUT}/window.md"; then
  pass "客户方发言人标了（客户方）"
else
  fail "客户方发言人没标分边"
fi
if grep -qF '**梁伟（我方同事）**' "${OUT}/window.md"; then
  pass "我方同事标了（我方同事），没被当成客户"
else
  fail "我方同事被当成客户，群聊分边失效"
fi
if grep -qF '群成员分边（按发言数）' "${OUT}/window.md"; then
  pass "群成员分边名册有输出"
else
  fail "缺群成员分边名册"
fi
if grep -qF '分不清谁是我方谁是客户' "${OUT}/window.md"; then
  pass "分边全空的群有醒目警告"
else
  fail "分边全空的群没有警告，容易被写成「客户在等我们」"
fi
if grep -qF '**陈经理（客户方）**' "${OUT}/window.md"; then
  fail "分边未知的人被硬标成客户方"
else
  pass "分边未知的人没有硬标成客户方"
fi
if grep -qF '群聊·外部' "${OUT}/window.md"; then
  pass "外部群标出「群聊·外部」"
else
  fail "外部群没有标注"
fi

echo ""
echo "== 4. 台账校验（应当通过） =="
python3 "${HERE}/check_ledger.py" --ledger "${FIXTURE}/expected-ledger.json" \
  --window "${OUT}/window.msgs.jsonl" --until 2026-11-25 --suggest
if [ $? -eq 0 ]; then
  pass "示例台账 ERROR 0"
else
  fail "示例台账不该有 ERROR"
fi

echo ""
echo "== 5. 金额造假（必须被拦） =="
python3 - "${FIXTURE}/expected-ledger.json" "${OUT}/bad-ledger.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
touched = 0
for item in data["items"]:
    if item.get("amount"):
        item["amount"]["value"] = 500000
        touched += 1
json.dump(data, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("改了 %d 条金额为 500000" % touched)
PY

python3 "${HERE}/check_ledger.py" --ledger "${OUT}/bad-ledger.json" \
  --window "${OUT}/window.msgs.jsonl" --until 2026-11-25 >"${OUT}/bad.log" 2>&1
BAD_EXIT=$?
cat "${OUT}/bad.log"
if [ "${BAD_EXIT}" -eq 1 ]; then
  pass "退出码 1"
else
  fail "退出码应为 1，实际 ${BAD_EXIT}"
fi
if grep -q 'E_AMOUNT_NOT_GROUNDED' "${OUT}/bad.log"; then
  pass "报出 E_AMOUNT_NOT_GROUNDED"
else
  fail "没报 E_AMOUNT_NOT_GROUNDED，金额闸失效"
fi

echo ""
echo "== 5b. 原话造假（必须被拦） =="
python3 - "${FIXTURE}/expected-ledger.json" "${OUT}/fake-quote.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
item = data["items"][0]
item["evidence"][1]["quote"] = "客户觉得价格有点贵，希望给个优惠"
json.dump(data, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("把第 1 条 evidence 的 quote 换成概括句")
PY

python3 "${HERE}/check_ledger.py" --ledger "${OUT}/fake-quote.json" \
  --window "${OUT}/window.msgs.jsonl" --until 2026-11-25 >"${OUT}/fake-quote.log" 2>&1
FQ_EXIT=$?
cat "${OUT}/fake-quote.log"
if [ "${FQ_EXIT}" -eq 1 ]; then
  pass "退出码 1"
else
  fail "退出码应为 1，实际 ${FQ_EXIT}"
fi
if grep -q 'E_QUOTE_NOT_GROUNDED' "${OUT}/fake-quote.log"; then
  pass "报出 E_QUOTE_NOT_GROUNDED"
else
  fail "没报 E_QUOTE_NOT_GROUNDED，原话闸失效"
fi

echo ""
echo "== 5c. 阶段回退与复活（派生状态） =="
python3 - "${FIXTURE}/expected-ledger.json" "${OUT}/regressed.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
item = data["items"][0]
item["stage"] = "quote"
item["status_history"] = [
    {"at": "2026-11-19", "from": None, "to": "quote", "note": "客户主动询价"},
    {"at": "2026-11-20", "from": "quote", "to": "negotiation", "note": "进入条款沟通"},
    {"at": "2026-11-23", "from": "negotiation", "to": "quote", "note": "客户说先按原报价走"},
]
json.dump(data, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("造一条 negotiation → quote 的回退")
PY

python3 "${HERE}/check_ledger.py" --ledger "${OUT}/regressed.json" \
  --window "${OUT}/window.msgs.jsonl" --until 2026-11-25 --suggest >"${OUT}/regressed.log" 2>&1
if grep -q 'S_REGRESSED' "${OUT}/regressed.log"; then
  pass "报出 S_REGRESSED"
else
  fail "没报 S_REGRESSED，回退信号失效"
fi

python3 - "${FIXTURE}/expected-ledger.json" "${OUT}/revived.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
item = data["items"][0]
item["evidence"] = [item["evidence"][0], item["evidence"][-1]]
item["evidence"][0]["at"] = "2026-11-04 09:30"
json.dump(data, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("造一条沉默 19 天后回来的证据链")
PY

python3 "${HERE}/check_ledger.py" --ledger "${OUT}/revived.json" \
  --window "${OUT}/window.msgs.jsonl" --until 2026-11-25 --suggest >"${OUT}/revived.log" 2>&1
if grep -q 'S_REVIVED' "${OUT}/revived.log"; then
  pass "报出 S_REVIVED"
else
  fail "没报 S_REVIVED，复活信号失效"
fi

echo ""
echo "== 6. 真实仓库（存在则跑） =="
REAL="${HOME}/wecom-data/wecom-chat-sales-test"
if [ -d "${REAL}" ]; then
  python3 "${HERE}/prepare_window.py" --store "${REAL}" \
    --since 2026-11-18 --until 2026-11-25 \
    --out "${OUT}/real" --self-names "温志聪,bennywen" >"${OUT}/real.log" 2>&1
  if [ $? -eq 0 ]; then
    pass "真实仓库切片成功"
    python3 - "${OUT}/real/window.json" <<'PY'
import json, sys
totals = json.load(open(sys.argv[1], encoding="utf-8"))["totals"]
if totals["my_messages"] > 0 and totals["their_messages"] > 0:
    print("PASS  真实数据方向判定：我 %d / 对方 %d" % (totals["my_messages"], totals["their_messages"]))
else:
    print("FAIL  真实数据方向判定：我 %d / 对方 %d（应当两边都 > 0）"
          % (totals["my_messages"], totals["their_messages"]))
    sys.exit(1)
PY
    [ $? -eq 0 ] || FAIL=1
  else
    fail "真实仓库切片失败"
    cat "${OUT}/real.log"
  fi
else
  echo "SKIP  没有 ${REAL}"
fi

echo ""
echo "== 7. 窗口稿全文 =="
cat "${OUT}/window.md"

echo ""
if [ "${FAIL}" -eq 0 ]; then
  echo "全部通过。产物在 ${OUT}"
else
  echo "有失败项，见上面的 FAIL。产物在 ${OUT}"
fi
exit "${FAIL}"
