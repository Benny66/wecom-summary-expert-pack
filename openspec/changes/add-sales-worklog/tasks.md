# Tasks

> 约定：`[x]` 是已落地的文件，`[ ]` 是还没做的。标了「未实测」的，说明代码写完了但没跑过。

## 1. 技能正文

- [x] 1.1 `skill/SKILL.md`：7 步主流程、3 个 CHECKPOINT、8 条硬限制、排查表、质量自查、5 条停止条件
- [x] 1.2 `skill/references/capture.md`：单聊命令、`--start` 固定等三条铁律、增量策略、覆盖判读、导出 JSONL 字段
- [x] 1.3 `skill/references/sales-signals.md`：9 个 stage、5 个 status、信号词、丢弃清单、金额铁律、客户命名
- [x] 1.4 `skill/references/ledger.md`：台账结构、状态机、4 个派生状态、合并规则、备份约定
- [x] 1.5 `skill/references/renderers.md`：六种周期分三组，各自的骨架与写法禁令
- [x] 1.6 `skill/test-prompts.json`：10 条回归 prompt（含 3 条应当拒绝的、2 条群聊口径的）

## 2. 脚本

- [x] 2.1 `scripts/prepare_window.py`：窗口切片、按客户分组、按天分段、引用渲染、脱敏、三类结构信号、`--list`
- [x] 2.2 `scripts/check_ledger.py`：结构/枚举/日期/证据/金额原文核对/PII/状态历史，`--suggest` 派生状态
- [x] 2.3 `scripts/vendor_wecom.sh`：从上游同步采集模块 + `py_compile` 自检（仓库维护脚本，不进包；同步后自动改写 `check_environment.py` 的 3 处上游口径）
- [x] 2.4 `scripts/selftest.sh`：一条命令跑完 fixture 的全部断言（含金额造假必须被拦）
- [x] 2.5 修渲染三处问题：`[quote]` 重复标记、`has_media` 把引用误判成媒体、日期标题前缺空行
- [x] 2.6 `bash selftest.sh` 全绿（12 项断言，含金额造假必须 exit 1）
- [x] 2.7 造一份单聊 fixture（3 个客户 / 覆盖报价、沉默、欠回复），放在 `skill/scripts/fixtures/wecom-chat-demo/`，含 `expected-ledger.json` 示例台账
- [x] 2.9 修方向判定 bug：真实数据里自己发的 `sender` 是中文名（`温志聪`）而非「我」，导致 `我 0 / 对方 20`。改为**优先用 `is_outgoing`**，`--self-names` 只作兜底；`prepare_window.py` 与 `check_ledger.py` 同步改；`window.msgs.jsonl` 增加 `is_outgoing`/`sender_raw` 字段
- [x] 2.10 `selftest.sh` 增加方向断言（`我 5 / 对方 7`）与真实仓库守卫检查（存在 `~/wecom-data/wecom-chat-sales-test` 时断言两边都 > 0）
- [x] 2.11 支持群聊：`prepare_window.py` 用 `sender_is_external` 把消息分成 我/我方同事/客户方/未标注，`silent`/`awaiting_*` 只在分边已知时产出，分边未知改打 `side_unknown`+`awaiting_check`；`window.json` 增加 `group_sides`、`coverage.customer_groups`、`coverage.groups_side_unknown`
- [x] 2.12 群聊 fixture：加「灵动科技项目对接群(外部)」（分边已知）与「宏远集团上线群」（`sender_is_external` 全空）两个群，`selftest.sh` 断言提到 33 项
- [x] 2.13 文档同步：`capture.md` 补 3.1 群内分边 / 3.2 分边全空 / 3.3 哪些群不能进台账；`sales-signals.md` 补 6.1 群聊三个坑；`ledger.md` 补 `channel` 字段与 6.1 群聊怎么进台账；`SKILL.md` 补「群聊怎么读」
- [x] 2.8 `prepare_window.py` 增加 `--week` / `--month` 快捷参数（ISO 周周一起、自然月；只写参数名 = 本周/本月；给了就忽略 `--since/--until`）。顺带修「周期末在未来时静默天数被多算」——基准改为 `min(周期末, 今天)`，写进 `window.ref_date`
- [x] 2.14 统一群分边的展示措辞：`display_sender` 不再自带一份映射，改走 `side_word()`（原先消息行写「我方」、名册写「我方同事」）

## 3. 端到端验证

- [x] 3.1 用 fixture 跑通：窗口 → 示例台账 → `check_ledger` 退出码 0 → 渲染检查
- [x] 3.2 反例验证：金额改成 500000 后 `E_AMOUNT_NOT_GROUNDED` 触发且退出码 1
- [x] 3.3 真实仓库端到端（自建测试会话）：采集 → 窗口 → 台账 → `check_ledger` ERROR 0 → 日报 `.md`
- [x] 3.4 修同分钟消息排序：`(sent_at, record_id)` 会把同分钟的对话顺序打乱，改为只按 `sent_at` 稳定排序（保留采集落盘的气泡顺序）
- [ ] 3.5 用真实数据跑一周（需本人授权），检查三件事：结论能点回消息、组长认可、漏项 ≤1
- [ ] 3.6 拿同一份数据分别出日报与周报，检查同一条事项的说法不打架
- [x] 3.7 `selftest.sh` 本机实测全绿：33 项 fixture 断言 + 2 项真实仓库守卫（`我 10 / 对方 10`），共 35 个 PASS、0 个 FAIL。含群聊分边、`--week/--month` 快捷周期
- [x] 3.8 真实仓库上验证 `--week` / `--month` / 裸 `--week`：`--month 2026-11` → `11-01 ~ 11-30`（`ref_date=11-25`），`--week 2026-W48` → `11-23 ~ 11-29` 且窗口稿打出「周期未结束」提示

## 4. 专家包外壳（`expert/`）

- [x] 4.1 `.codebuddy-plugin/plugin.json`：`name=sales-worklog-expert`、`categoryId=07-SalesCommerce`、中英展示字段、3 条 quickPrompts、`defaultInitPrompt` 与首条逐字一致
- [x] 4.2 `agents/sales-worklog-expert.md`：人设、6 条意图路由、6 个确认点、按缺失能力分档的降级、11 条红线
- [x] 4.3 `avatars/expert.png`：改由 `scripts/make_avatar.py` 纯标准库生成 512×512 PNG（不调图像模型、不产生计费）
- [x] 4.4 `scripts/build.sh` + `check_package.py`：组装、打包、逐项断言；M 组按「未声明 MCP 即通过」对齐
- [x] 4.5 `expert/README.md` + `CHANGELOG.md`
- [x] 4.6 `python3 scripts/make_avatar.py` 与 `bash scripts/build.sh` 本机实测：V2（512×512 / 3101 字节）、Z4/Z5 全过，`check_package.py` 57 项 0 失败，产出 `sales-worklog-expert-0.2.0.zip`
- [x] 4.7 `plugin.json` 的 `author.email` 改为 `2283743369@qq.com`
- [x] 4.8 交付物去历史痕迹：删掉所有上游项目名与个人绝对路径（README / SKILL.md / capture.md / expert README / CHANGELOG / check_package 注释）；`vendor_wecom.sh` 改为 `WECOM_CAPTURE_SRC` 无默认值、改为上游同步表述、不进包；`check_environment.py` 的 3 处上游口径改写由 vendor 脚本自动套用（已验证与手工结果逐字一致）

## 5. 后续（不在本次范围）

- [ ] 5.1 自动化：固定无人时段跑采集 + 生成日报（需先解决读屏独占前台）
- [ ] 5.2 评估企业微信官方「会话内容存档」通道，替代读屏采集
- [ ] 5.3 团队视角：按人拆台账，出团队周报
- [ ] 5.4 客户档案页：一个客户一页，汇总 items 与时间线
- [ ] 5.5 导出 docx / 腾讯文档 / 贴回企微群
