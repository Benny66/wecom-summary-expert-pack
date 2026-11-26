# 销售客户跟进总结专家包

> Turn WeCom client chats into evidence-backed work reports — daily, weekly, monthly, quarterly, half-year and annual.
>
> 把企业微信里的客户聊天，变成一份每条结论都能点回原话的工作总结。

面向**销售本人**的本地化工具：读屏采集企业微信会话 → 切成可读窗口稿 → 维护一份事项台账 → 从台账切出六种周期的 Markdown 报告。

不做知识库、不做团队协作、不做赢率预测。只解决一件事：**让周报里的每句话都有出处。**

| | 状态 |
|---|---|
| 技能 `skill/` | v0.3，端到端跑通：fixture 39 项断言 + 真实仓库守卫 2 项，全绿 |
| 采集模块 | 随技能携带（`scripts/wecom/` 13 py + 6 swift），装好即用 |
| 专家包 `expert/` | v0.3，`check_package.py` 57 项 0 失败；成品包见 [Releases](https://github.com/Benny66/wecom-summary-expert-pack/releases) |

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [三个关键设计](#三个关键设计)
- [快速开始](#快速开始)
- [六种周期的报告](#六种周期的报告)
- [客户群怎么处理](#客户群怎么处理)
- [目录结构](#目录结构)
- [自检与打包](#自检与打包)
- [运行环境](#运行环境)
- [已知限制](#已知限制)
- [路线图](#路线图)

---

## 它解决什么问题

销售的工作痕迹几乎全在企业微信里，但「把聊天记录变成工作总结」一直没个靠谱做法：

- **手写靠回忆** —— 一周下来漏掉的推进比记住的多
- **直接丢给大模型** —— 会编：编金额、编赢率、把客户的「再看看」翻译成「有意向」
- **每周从头整理** —— 客户反复「首次出现」，状态前后矛盾

这个包走一条更笨但更可靠的路子：**脚本做确定性工作，模型做语义判断，硬闸挡住编造。**

```
企业微信 ──读屏采集──▶ 采集仓库 ──切窗口──▶ 窗口稿 ──抽取──▶ 事项台账 ──▶ 六种周期报告
                      manifest.json      window.md           ledger.json     本地 markdown
                      current.jsonl      window.json              ▲
                                         window.msgs.jsonl       └── check_ledger.py 硬闸
```

采集、切片、分组、统计、找「谁欠谁一句回复」全部由脚本完成，零幻觉；判断「这条算不算推进、到了哪个阶段」交给模型。数字只从 `window.json` 或台账取，不让模型自己数。

## 三个关键设计

### 1. 长周期的输入不是消息，是台账

年报的原料不是二十万条聊天，是 12 份月报 + 全年台账。所以台账是唯一真相，报告只是它的一个切片：

```
消息 ──▶ 窗口稿 ──▶ 事项台账 ──▶ 日报 ──▶ 周报 ──▶ 月报 ──▶ 季报 ──▶ 半年 ──▶ 年报
                        │                    └──────────┴──────────┘
                        └──── 台账始终是唯一真相，报告只是它的切片 ────┘
```

日/周读消息；月报读周报 + 台账；季报以上只读月报与台账，**不读原始消息**。周报漏写的事，月报补不回来 —— 所以周报阶段就要把「没推进但有信号」的客户写进去。

台账的最小单位是 **客户 × 主题**，不是会话。一个客户可以同时有三条线（报价、另一个产品试用、售后问题），糊成一条以后所有报告都失真。

### 2. 金额必须能在原文里找到

销售总结最容易翻车的就是编数字，所以金额过三道闸：

| 层 | 机制 |
|---|---|
| 规则层 | `amount` 只允许 `value` / `currency` / `source` / `evidence_msg_id` 四项，禁止「报价 × 赢率」这类派生值 |
| 脚本层 | 把 `value` 展开成 `300000` / `300,000` / `30万` / `30w` / `30k`，去 evidence 那条消息的**原文**里找，找不到报 `E_AMOUNT_NOT_GROUNDED` 并退出码 1 |
| 渲染层 | 原文是「30 万以内」，报告只能写「预算上限 30 万」，不能写「预算 30 万」 |

取舍是**宁漏不猜**：漏了只是少写一个数字，猜了就是造假。

### 3. 群里不把同事当客户

客户群里我方同事和客户混着说话。只数「我 / 对方」，后果不是少一点精度，而是**结论方向反了**：

- 客户一周没吭声，但同事天天在群里汇报进度 → 读成「客户很活跃、沟通顺畅」
- 同事答应「下周一出排期」→ 读成「客户承诺了时间点」
- 客户零发言被同事的发言掩盖 → `silent` 不亮，风险段漏掉

所以按 `sender_is_external` 把每条消息分成四类，并在窗口稿顶部给出分边统计：

```
群聊分边（群消息 9 条）：我 2 ｜ 我方同事 1 ｜ 客户方 3 ｜ 未标注 3
```

分不清的那部分（通讯录没导全，字段全为 `null`）**不硬猜**：脚本只打 `side_unknown`，不产出 `silent` / `awaiting_*` 结论，窗口稿里挂一行警告。宁可漏一条结论，也不把同事的话写成客户回应。

## 快速开始

```bash
git clone git@github.com:Benny66/wecom-summary-expert-pack.git
cd wecom-summary-expert-pack/skill/scripts
```

### 1. 核对会话名（不启动采集）

名字必须与企微侧栏逐字一致。单聊常见形态是「英文名(中文名)」，群改过名就用当前名字。

```bash
python3 capture_chat.py --store ~/wecom-data/wecom-chat-sales \
  --singles "张三,李四(某科技)" --groups "灵动科技项目对接群(外部)" \
  --start 2025-11-01 --prepare-only
```

### 2. 采集

`--start` 在同一仓库内**不可更改**，第一次就设到你想回溯的最早点（做年报就要覆盖整年）。往后延长只改 `--end`。

```bash
python3 capture_chat.py \
  --store ~/wecom-data/wecom-chat-sales \
  --singles "张三,李四(某科技)" \
  --groups "灵动科技项目对接群(外部)" \
  --start 2025-11-01 --end 2026-11-25 \
  --account-label sales --self-names "<本人中文名>"
```

采集独占企微前台、模拟滚动，跑的时候别动那台机器。建议一次采一周，放后台跑完再干活；锁屏、息屏、焦点丢失属正常中断，恢复后同命令续跑。

### 3. 切窗口

```bash
# 日报
python3 prepare_window.py --store ~/wecom-data/wecom-chat-sales \
  --since 2026-11-25 --until 2026-11-25 --out ~/worklog/runs/2026-11-25

# 周报 / 月报：用 --week / --month，别手写日期
python3 prepare_window.py --store ~/wecom-data/wecom-chat-sales --week 2026-W48 --out ~/worklog/runs/2026-W48
python3 prepare_window.py --store ~/wecom-data/wecom-chat-sales --month 2026-11 --out ~/worklog/runs/2026-11
```

`--week` 是 ISO 周（周一起、周日止），`--month` 是自然月；只写参数名表示本周 / 本月。跨月、跨年、记错周几这几个坑都由它兜住。

产出三份：`window.md`（模型读这个）、`window.json`（模型取数字）、`window.msgs.jsonl`（校验证据用）。

### 4. 建台账并校验

把 `window.md` 交给模型整理事项、更新 `~/worklog/ledger.json`，然后过硬闸：

```bash
python3 check_ledger.py --ledger ~/worklog/ledger.json \
  --window ~/worklog/runs/2026-11-25/window.msgs.jsonl \
  --until 2026-11-25 --suggest
```

**退出码非 0 不许渲染报告。** 之后按 `skill/references/renderers.md` 出对应周期的 md。

### 用假数据先跑一遍

不用真实数据就能验完整链路：

```bash
cd skill/scripts && bash selftest.sh
```

## 六种周期的报告

三种问句，不是六种长度：

| 问句 | 周期 | 输入 | 篇幅 | 文件名 |
|---|---|---|---|---|
| 发生了什么 | 日报、周报 | 当日 / 7 天窗口 + 台账 | 200–400 / 600–1200 字 | `2026-11-25-daily.md`、`2026-W48-weekly.md` |
| 和计划的差距 | 月报 | 4 份周报 + 台账 | 1500–2500 字 | `2026-11-monthly.md` |
| 什么变了、什么该停 | 季报 / 半年报 / 年报 | 月报 + 台账（不读原始消息） | 2500–4500 字 | `2026-Q4-quarterly.md`、`2026-H2-halfyear.md`、`2026-annual.md` |

每份报告末尾固定两段：**数据与覆盖**（窗口、消息数、哪些会话不是完整历史）、**未验证项**（口头承诺、转述的第三方诉求）。没采全就说没采全 —— 报告的可信度来自它承认了什么，不是它写了多少。

## 客户群怎么处理

群只有在「整个群 = 一个客户项目」时才算一条工作线：

- **能进**：客户项目群、招投标对接群、客户内部上线群
- **不能进**：部门群、全员通知群、跨多家客户的行业群、纯内部协作群

采集时用 `--groups` 单独给名单，可与 `--singles` 同时给。台账里不按渠道建 item —— 同一客户既有单聊又有群，仍然按「客户 × 主题」建一条线，`channel` 只记来源。

## 目录结构

```
wecom-summary-expert-pack/
├── skill/                                  # 技能正文（唯一真身，专家包从这里复制）
│   ├── SKILL.md                            # 主流程、硬限制、排查、停止条件
│   ├── references/
│   │   ├── capture.md                      # 采集命令、单聊与群的口径、增量策略、覆盖判读
│   │   ├── sales-signals.md                # 阶段/状态枚举、信号词、丢弃清单、金额铁律
│   │   ├── ledger.md                       # 台账结构、状态机、派生状态、合并规则
│   │   └── renderers.md                    # 六种周期的问句、骨架、篇幅
│   ├── scripts/
│   │   ├── capture_chat.py                 # 采集入口（随技能携带）
│   │   ├── check_environment.py            # 采集前环境自检（可选）
│   │   ├── wecom/                          # 采集器本体：13 py + 6 swift
│   │   ├── prepare_window.py               # 采集仓库 → 窗口稿 + 统计 + 结构信号
│   │   ├── check_ledger.py                 # 台账硬闸（结构 / 证据 / 金额 / PII）
│   │   ├── selftest.sh                     # 一条命令跑完全部断言（开发期，不进包）
│   │   ├── vendor_wecom.sh                 # 上游同步脚本（开发期，不进包）
│   │   ├── fixtures/wecom-chat-demo/       # 假仓库：3 个单聊 + 2 个客户群
│   │   └── test-prompts.json               # 回归 prompt
│   └── ...
├── expert/                                 # WorkBuddy 专家包外壳
│   ├── .codebuddy-plugin/plugin.json
│   ├── agents/sales-worklog-expert.md      # 人设、意图路由、确认点、降级、红线
│   ├── avatars/expert.png                  # 512×512 插画头像
│   └── scripts/                            # make_avatar / build / check_package
└── openspec/changes/add-sales-worklog/     # proposal / design / tasks
```

专家包产物（`sales-worklog-expert-<version>.zip`）不进仓库，通过 [Releases](https://github.com/Benny66/wecom-summary-expert-pack/releases) 分发；本地要重新生成就 `cd expert && bash scripts/build.sh`。

## 自检与打包

| 命令 | 覆盖 | 当前结果 |
|---|---|---|
| `cd skill/scripts && bash selftest.sh` | 窗口切片、结构信号、群聊分边、渲染、台账校验、金额造假与原话造假必须被拦、阶段回退与复活 | **39 项 fixture 断言 + 2 项真实仓库守卫，全绿** |
| `cd expert && bash scripts/build.sh` | 组装 → 打 zip → 按专家包规范 v2.4 逐项断言（结构 / 配置 / Agent MD / 头像 / 依赖 / ZIP） | **57 项，0 失败** |

`selftest.sh` 是唯一能证明「改了没改坏」的东西。它包含两条反例：把台账金额从 30 万改成 50 万，必须报 `E_AMOUNT_NOT_GROUNDED`；把客户原话换成一句概括，必须报 `E_QUOTE_NOT_GROUNDED`。两者都要退出码 1。

## 运行环境

| | 要求 |
|---|---|
| 采集 | macOS + 已登录的企业微信 + Swift 编译器（`swiftc`）+ 执行应用的辅助功能权限 |
| 切窗口 / 校验 | Python 3.8+，纯标准库，无第三方依赖 |
| 报告 | 本地 Markdown，不依赖任何 MCP、不联网 |

## 已知限制

- **只能在 macOS 采集**（读屏 + 辅助功能），采集时独占企微前台。非 macOS 环境只能用导出的 JSONL 顶上。
- **群聊分边依赖通讯录**：成员没有内外标记时整段会落在「未标注」，报告里只能写「分边未确认」。
- **`--redact all` 只替换会话名与数字类 PII**，正文里的真名不替换，外发前仍需人工复核。
- **单人视角**：多人协作、团队周报不在当前版本内（要按人拆台账，是另一个设计）。
- **不做预测**：赢率、成交金额、下季度收入一律不答；可提供「客户明确说过的预算与时间点」清单。
- **报告质量取决于台账积累**：第一天就开始写年报，只能写出一份空壳。

## 路线图

- [ ] 真实数据跑满一周的验收（结论能点回消息 / 漏项 ≤1）
- [ ] 自动化：固定无人时段跑采集并生成日报（前提是解决独占前台）
- [ ] 评估企业微信官方「会话内容存档」通道，替代读屏采集
- [ ] 团队视角：按人拆台账，出团队周报
- [ ] 客户档案页：一个客户一页，汇总事项与时间线
- [ ] 导出 docx / 在线文档

## 许可

暂未指定。如需使用或二次分发，请先联系作者。
