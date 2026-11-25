# sales-worklog-expert

WorkBuddy 专家包（Agent 型）：把销售本人在企业微信里与客户（单聊 + 客户群）的聊天，整理成日报、周报、月报、季报、半年报、年报（本地 Markdown）。

| 字段 | 值 |
|---|---|
| `name` / `plugin` | `sales-worklog-expert` |
| `agentName` | `sales-worklog-expert`（= `agents/sales-worklog-expert.md` = frontmatter `name`） |
| `displayName` | 销售客户跟进总结 / Sales Client Follow-up Reports |
| `profession` | 销售运营与客户跟进 / Sales Operations Specialist |
| `categoryId` | `07-SalesCommerce`（使用者是销售，放销售分类） |
| `expertType` | `agent` |
| `defaultInitPrompt` | 与 `quickPrompts[0]` 逐字一致 |

## 结构

```
sales-worklog-expert/
├── .codebuddy-plugin/plugin.json   name/version/agents[]/skills[]/expertType/agentName/展示字段
├── agents/sales-worklog-expert.md  frontmatter + 人设 + 6 意图路由 + 6 确认点 + 降级分档 + 11 红线
├── avatars/expert.png              512×512 PNG 插画头像（缺失时由 make_avatar.py 生成兜底版）
├── scripts/make_avatar.py          纯标准库画头像（无第三方依赖、无外部计费）
├── scripts/build.sh                组装 + zip + 自检
├── scripts/check_package.py        逐项断言，零依赖
└── build/sales-worklog-expert/     构建产物（不进仓库）
```

**技能正文不在这里维护**：`skills/sales-worklog/` 由 `scripts/build.sh` 从 `../skill` 复制生成。改规则请改技能目录，不要在这里改。

## 打包

```bash
bash scripts/build.sh                                  # 组装 + zip + 自检
python3 scripts/make_avatar.py                         # 只生成头像
SKILL_SRC=/path/to/skill bash scripts/build.sh         # 技能源不在默认位置
```

产物 `sales-worklog-expert-<version>.zip`，包内顶层目录 `sales-worklog-expert/`。zip 不进仓库（`.gitignore` 已排除），通过 GitHub Releases 分发。

打包会排除开发期产物：`fixtures/`、`selftest.sh`、`test-prompts.json`、`vendor_wecom.sh`、`__pycache__`。采集模块本身随包发布（`skills/sales-worklog/scripts/wecom/`），装好即可采。

## 本包的两处设计选择

1. **没有 `.mcp.json`，也没有 `dependencies.mcpServers`**。本专家全部本地完成（采集 → 窗口 → 台账 → md），不写知识库、不发邮件。`check_package.py` 的 M 组相应对齐：未声明 MCP 视为通过；一旦声明则逐项校验文件、url、`x-workbuddy` 字段与硬编码 Token。
2. **`P18b` 查兄弟目录**。技能正文不在 expert/ 内，源码侧断言 `../skill/SKILL.md` 存在（构建后则查 `build/<name>/skills/<skill>/SKILL.md`），这样技能只有一份真身，不会两份副本漂移。

## 自检覆盖

| 组 | 断言示例 |
|---|---|
| S 结构 | `agents/`、`avatars/` 在插件根目录；`.codebuddy-plugin/` 下不嵌 `agents/`、`skills/`；`README.md` 存在 |
| P 配置 | `name` 小写连字符；`version` 语义化；`description` 英文；`agents`/`skills` 路径数组且文件存在；`agentName` 对得上；`displayName.zh` ≤15；`displayDescription.zh` 40—50；`categoryId` 在 15 个枚举内；`tags` 3—5；`quickPrompts` 恰好 3；`defaultInitPrompt == quickPrompts[0]` |
| A Agent MD | `name` = 文件名；`description` 英文；无 `tools`；`displayName`/`profession` 中英齐；`maxTurns` 整数；`skills` 列表；`displayName` 与 plugin.json 逐字一致 |
| V 头像 | PNG；512×512；≤500KB |
| M 依赖 | 未声明 MCP 通过；声明则校验文件、url、`x-workbuddy` 字段；无硬编码 Token |
| Z ZIP | 顶层目录唯一且 = `name`；含 plugin.json / 头像 / agents MD / 技能 SKILL.md；技能带齐 `references/renderers.md` 与两个脚本；不含 fixtures / selftest / test-prompts |

## 上架前要改的

- `plugin.json` 的 `author.email` 目前填的是 `2283743369@qq.com`，需要换就改这里。
- 头像是插画版（深蓝圆角卡 + 戴耳麦的销售手持清单），已裁成 512×512 / 约 300KB。`scripts/make_avatar.py` 保留为兜底：`avatars/expert.png` 缺失时 `build.sh` 会自动生成一个纯色卡片版。换图直接替换 `avatars/expert.png` 后重跑 `build.sh`，必须仍是 512×512、≤500KB。

## 已知能力边界（不是缺陷）

- **不接任何 MCP**：不写知识库、不发邮件、不进腾讯文档、不贴回企微群。交付形态只有本地 Markdown。
- **只在 macOS 上能采集**（读屏 + 辅助功能 + `swiftc`），且采集时独占企微前台。没有采集环境时只能用导出的 JSONL。
- **单人视角**：不做团队多人周报。多人要按人拆台账，是另一个变更。
- **不做预测**：赢率、成交金额、下季度收入一律不答。
