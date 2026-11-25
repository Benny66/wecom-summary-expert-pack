# 采集与输入

采集模块随技能携带，装好就能直接采，不需要额外安装或配置：

- `scripts/wecom/`：采集器本体（13 个 py + 6 个 swift）
- `scripts/capture_chat.py`：采集入口，管目标解析、日期校验、多轮续采
- `scripts/check_environment.py`：可选的环境自检，出问题先跑它

采集器本身不要改。遇到读屏异常先看 `--status`，再按第 9 节的退出码处理。

## 1. 命令

```bash
# 只采客户单聊（销售主场景）
python3 scripts/capture_chat.py \
  --store ~/wecom-data/wecom-chat-sales \
  --singles "张三,李四(某科技),王五" \
  --start 2025-11-01 --end 2026-11-25 \
  --account-label sales --self-names "<本人中文名>"

# 单聊 + 客户群
python3 scripts/capture_chat.py \
  --store ~/wecom-data/wecom-chat-sales \
  --singles "张三" --groups "某科技-项目群" \
  --start 2025-11-01 --end 2026-11-25 \
  --account-label sales --self-names "<本人中文名>"

# 只看计划，不启动（会打印 [group]/[single] 清单，先核对名字）
python3 scripts/capture_chat.py --store <仓库> --singles "张三" \
  --start 2025-11-01 --prepare-only

# 只读操作
python3 scripts/capture_chat.py --store <仓库> --status
python3 scripts/capture_chat.py --store <仓库> --stop
python3 scripts/capture_chat.py --store <仓库> --index
```

## 2. 三条铁律

1. **`--start` 在同一仓库内必须固定**，只改 `--end`。第一次就把起点定到你真正想回溯的最早点（做年报就覆盖整年）。改不动，只能新建仓库重采。
2. **名字必须与企微侧栏逐字一致**。单聊常见形态是「英文名(中文名)」；群改过名用当前名字。
3. **采集独占前台**，跑到一半不要动键盘鼠标。手动模式下建议一次采一周，放后台跑完再干活；停止只用 `--stop`，不要 kill 进程，残留子进程会和下一轮抢企微。

同一时间只允许一个采集进程（含其它技能），UI 锁会拒绝第二个。

## 3. 单聊与群聊的差别

| | 单聊 | 群聊 |
|---|---|---|
| 发送者 | 界面不渲染名字，方向按头像位置判定；自己发的落在 `is_outgoing=true`，`sender` 是本人中文名（如「温志聪」），**不一定是「我」** | 有真实显示名；历史通道下外部成员常带 `name@company` |
| 会话名 | 客户显示名（可能是「英文名(中文名)」） | 群名；外部群常带 `(外部)` 后缀 |
| 我方识别 | **`is_outgoing` 为准**（true/false）；缺失时才回退 `--self-names` 匹配 `sender`/`sender_raw` | 自己发的同样看 `is_outgoing`；同事和客户要靠 `sender_is_external` 分 |
| 双方分边 | 不需要，非我即客户 | **必须分**，见 3.1 |
| 噪声 | 个人事务、转发、寒暄占比高 | 通知、@全体多 |
| 用途 | 销售总结的主数据源 | 只在「该群=某客户项目」时纳入 |

### 3.1 群里的「对方」是两类人

群里把同事和客户混成一个「对方」，会让所有「谁在等谁」的判断失真：客户一周没说话，但同事在群里天天汇报，就会被读成「客户活跃」。所以 `prepare_window.py` 把每条消息分成四类：

| 分类 | 判定 | md 里显示 |
|---|---|---|
| 我 | `is_outgoing=true` | `**我**` |
| 我方同事 | `sender_is_external=false` | `**梁伟（我方）**` |
| 客户方 | `sender_is_external=true` | `**王经理（客户方）**` |
| 未标注 | `sender_is_external` 为 null | `**陈经理**`（不加分边） |

分边数据来自通讯录 `directory`。历史通道下外部成员的 `sender` 形如 `name@company`，采集器会自动判成 `sender_is_external=true` 并填 `sender_company`；同事通常查得到 `false`。

### 3.2 分边全空时怎么办

整段群消息的 `sender_is_external` 都是 `null`（常见于通讯录没导全、或群成员不在好友列表）：

- 脚本会打 `side_unknown`，超过 `--awaiting-days` 再加 `awaiting_check`，**不产出** `silent` / `awaiting_me` / `awaiting_client`；
- `window.md` 会在这段对话上方写一行警告，`window.json` 里 `client_side_known=false`；
- **你不要自己补这个判断**。报告里只能写「本窗口内谁说了什么」，涉及「客户是否在等我们」就写成「该群分边未确认，需人工看一眼」。把同事的话当客户回应，比漏一条结论更糟。

要让分边生效，重新采一次并确保通讯录可用；或直接人工在台账里记下这个群的主要客户方联系人。

### 3.3 哪些群不能进台账

群只有在「整个群 = 一个客户项目」时才算一条工作线：

- 能进：客户项目群、招投标对接群、客户内部上线群（你在群里代表供应方）
- 不能进：部门群、全员通知群、跨多家客户的行业群、纯内部协作群

判断不准就问用户一句：「这个群是单个客户的项目群吗？」——不要自己猜。纯内部群即使被采进仓库，`prepare_window.py` 也会因为 `客户方 0 条` 给它打 `silent`，你看到 `silent` 且群名像内部群，就该把它从台账里排除并说明原因。

## 4. 仓库里读什么

| 位置 | 用途 |
|---|---|
| `manifest.json` | 会话清单、`history_complete`、`earliest_message`、`latest_message_at`、`reached_start` |
| `conversations/<key>/current.jsonl` | 每个会话当前版本的消息（`prepare_window.py` 只读这里） |
| `index.csv` | 人看的汇总，排查用 |

不读 `raw/`、不碰锁文件、不写仓库。

## 5. 消息字段口径

`prepare_window.py` 只用这些：

| 字段 | 用法 |
|---|---|
| `record_id` | 32 位十六进制，**台账 evidence 就引用它** |
| `conversation` / `conversation_key` | 客户名与稳定键；同名不同公司会有多个 key，分开处理不合并 |
| `conversation_type` | `single` / `group`；缺失时脚本按「非我的发言人是否多于 1 人」兜底推断 |
| `conversation_is_external_group` | 是否外部群。**`null` 只表示没观测到外部标记，不等于确认内部群** |
| `sender` | 显示名。单聊里自己发的常是本人中文名，别拿它判方向 |
| `sender_raw` | 原始标识（可能是 `name@company`），`--self-names` 兜底时会一起匹配 |
| `is_outgoing` | 我发的 = `true`。**判方向只看这个**，缺失才回退名字匹配 |
| `sent_at` | `YYYY-MM-DD HH:MM:SS`，本地时间，窗口按前 10 位比较 |
| `body` | 正文；引用消息形如「被引用内容 + `---` + 新内容」 |
| `message_type` | `text` / `quote` / `image_or_media` 等 |
| `content_incomplete` | 正文有未暴露的表情或媒体，可能看不全 |
| `sender_is_external` / `sender_company` | 群聊分边用：`true` 客户方、`false` 我方同事、`null` 未知 |

## 6. 增量策略

台账里记 `last_capture_at` 与每个客户的 `last_update_at`。下次：

- 采集：`--start` 不动，只把 `--end` 推到今天。
- 切窗口：日报 `--since` = 当天；周报用 `--week 2026-W48`，月报用 `--month 2026-11`（比手写 `--since/--until` 稳，跨月跨年不会错）。**不要为了"补全"而把起点往前挪太多**，重复处理只会让台账 evidence 变长，不增加信息。

如果上一期是 11-18，这期从 11-19 开始，中间不要断。断了就在报告里写明「11-19 ~ 11-22 未采集」。

## 7. 覆盖判读

| 字段 | 报告里怎么写 |
|---|---|
| `history_complete=true` | 可以说「本账号可见范围内完整」 |
| `history_complete=false` | 必须写「该客户本窗口不是完整历史」并说明原因 |
| `candidate_coverage.coverage_status=targets_subset` | 含单聊时就是这个值，**不能声称采全账号**；只写「本批覆盖指定客户」 |
| `reached_start=true` | 已翻到会话开头，最早消息可信 |
| `last_status=partial/review` | 列出会话与状态 |

成员入群前、客户端已删除、已过期的消息补不回来。

## 8. 没有采集环境时

非 macOS、无 `swiftc`、未授权辅助功能、企微未登录 —— 用导出的 JSONL 顶上。必需字段：

```
record_id, conversation, conversation_key, conversation_type,
sender, sent_at, body, message_type
```

可选：`links`、`content_incomplete`、`is_outgoing`、`sender_is_external`、`sender_company`。

采群聊时 `is_outgoing` 和 `sender_is_external` 不能省：少了第一个分不清哪条是你说的，少了第二个分不清同事和客户，报告里的「谁在等谁」就只能全靠猜。

`prepare_window.py` 对缺字段的行跳过并在 stderr 计数；缺 `record_id` 的行无法作为证据，会被统计为 `dropped_no_id`。

## 9. 采集退出码

| 码 | 含义 | 处理 |
|---|---|---|
| 0 | 正常 | — |
| 20 / 21 | 锁屏 / 息屏 | 恢复后同命令续跑 |
| 22 | 布局变化 | 确认企微窗口未改版后续跑 |
| 23 | 前台焦点丢失 | 别切窗口，续跑 |
| 25 | 弹窗阻塞 | 人工处理后加 `--retry-dialog-blocked` |
| 26 | 遇到网页应用会话 | 跳过该会话，报告里标注未采 |

已落盘的会话仍可用于本次窗口，报告里写明「本批覆盖不完整」。
