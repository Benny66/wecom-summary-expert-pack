# Changelog

## 0.2.0 — 2026-11-25

与技能 `sales-worklog` 0.2.0 配套：**支持客户群**。

- 采集可以把客户群一起采（`--groups`），窗口稿按 `sender_is_external` 把每条消息分成 **我 / 我方同事 / 客户方 / 未标注** 四类，避免把同事的话当成客户回应。
- 分边未知（该字段全空）时不产出 `silent` / `awaiting_*` 结论，改打 `side_unknown` + `awaiting_check`，并在窗口稿里挂警告行。
- `window.json` 新增 `totals.group_sides`、`coverage.customer_groups`、`coverage.groups_side_unknown`。
- 新增群聊规则：哪些群不能进台账（部门群/通知群/跨客户行业群）、同一客户既有单聊又有群时怎么建 item、群里同事推进的事项 `next_action` 要写清谁做。
- 红线从 10 条增到 11 条，新增「不把群里同事当客户」；降级表补两行（客户群混采、分边未知）。
- `prepare_window.py` 新增 `--week` / `--month` 快捷周期参数（ISO 周 / 自然月，只写参数名 = 本周/本月）。
- 修「周期末在未来时静默天数被多算」：基准改为 `min(周期末, 今天)`，写进 `window.ref_date`，窗口稿在周期未结束时打印一行说明。
- 统一群分边的展示措辞：`display_sender` 不再自带一份映射，改走 `side_word()`（原先消息行写「我方」、名册写「我方同事」）。
- `selftest.sh` 断言 12 → 33 项。前 28 项本机实测全绿（真实仓库 `我 10 / 对方 10`）；后 5 项（`--week`/`--month`）待复跑。
- 头像换成插画版（深蓝圆角卡 + 戴耳麦的销售手持清单），512×512 / 约 300KB；`make_avatar.py` 保留为缺图时的兜底。
- `author.email` 改为 `2283743369@qq.com`；交付文档去掉上游项目名与个人绝对路径。

## 0.1.0 — 2026-11-25

首个版本，与技能 `sales-worklog` 0.1.0 配套。

- 新增 Agent 人设 `agents/sales-worklog-expert.md`：6 条意图路由（写日报 / 写周报 / 月报及以上 / 查欠回复与停滞 / 查历史 / 兜底分流）、6 个必须停下来的确认点、按缺失能力分档的降级策略、10 条红线。
- 新增 `.codebuddy-plugin/plugin.json`：`categoryId=07-SalesCommerce`，3 条 quickPrompts，`defaultInitPrompt` 与首条逐字一致。
- 新增 `scripts/make_avatar.py`：纯标准库生成 512×512 PNG 头像，不依赖图像模型、不产生额外计费。
- 新增 `scripts/build.sh` 与 `scripts/check_package.py`：组装、打包、逐项断言。
- 两处设计选择：不声明 MCP 依赖（本专家全部本地完成）；技能源在兄弟目录，`P18b` 查 `../skill/SKILL.md`。
