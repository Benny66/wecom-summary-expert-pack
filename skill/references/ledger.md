# 事项台账

台账是这份总结的唯一真相。报告是它的切片，窗口稿是它的原料。台账放在固定路径（默认 `~/worklog/ledger.json`），**定了不许换**。

## 1. 结构

```json
{
  "schema_version": 1,
  "account_label": "sales",
  "updated_at": "2026-11-25T21:10:00+08:00",
  "last_capture_at": "2026-11-25",
  "silence_days": 7,
  "clients": {
    "3966f242a06640d29b23e7822e2dadbd": {
      "name": "张三(某科技)",
      "company": "某科技",
      "type": "single"
    },
    "d1b2c3d4e5f60718293a4b5c6d7e8f93": {
      "name": "灵动科技项目对接群(外部)",
      "company": "灵动科技",
      "type": "group"
    }
  },
  "items": [
    {
      "item_id": "3966f2-20261105-01",
      "client": "张三(某科技)",
      "conversation_key": "3966f242a06640d29b23e7822e2dadbd",
      "title": "报价与折扣谈判",
      "stage": "quote",
      "status": "active",
      "opened_at": "2026-11-05",
      "last_update_at": "2026-11-25",
      "next_action": "出阶梯价（3 档）并发给客户",
      "next_action_due": "2026-11-26",
      "amount": {
        "value": 300000,
        "currency": "CNY",
        "source": "客户口述预算上限",
        "evidence_msg_id": "a2cd1b67e9ca4a229a71bf27cda412c7"
      },
      "evidence": [
        {
          "at": "2026-11-05 10:12",
          "msg_id": "a2cd1b67e9ca4a229a71bf27cda412c7",
          "quote": "先报个价吧，我们预算 30 万以内"
        },
        {
          "at": "2026-11-25 15:10",
          "msg_id": "26411fc2c95e45f69e21bf774ca0657c",
          "quote": "价格高了 5 个点，能不能给个阶梯"
        }
      ],
      "status_history": [
        {"at": "2026-11-05", "from": null, "to": "quote", "note": "客户主动询价"},
        {"at": "2026-11-25", "from": "quote", "to": "quote", "note": "客户嫌高 5 个点，要求阶梯价"}
      ]
    }
  ]
}
```

字段口径：

| 字段 | 必填 | 说明 |
|---|---|---|
| `item_id` | 是 | `<conversation_key 前 6 位>-<opened_at 去横线>-<两位序号>`，例 `3966f2-20261105-01`。**一旦分配不许改** |
| `client` | 是 | 侧栏原名，不简写。**群里的 item 写客户主体（公司或主对接人），不要写群名**，否则同一个客户会以「张三」和「张三项目群」两种身份出现在报告里 |
| `conversation_key` | 是 | 与采集仓库一致，用于定位来源。群来源就填群会话的 key |
| `channel` | 建议 | `single` 或 `group:<群名>`。一个客户同时有单聊和群时，靠它区分这条线从哪来 |
| `title` | 是 | ≤40 字，一句话说清这条线是什么，不要写成「客户沟通」 |
| `stage` | 是 | 九枚举之一，见 `sales-signals.md` |
| `status` | 是 | 五枚举之一 |
| `blocked_by` | `blocked` 时必填 | 卡在什么上，一句话 |
| `opened_at` | 是 | 首次出现日期，`YYYY-MM-DD` |
| `last_update_at` | 是 | 最近一次有实质更新的日期 |
| `closed_at` | `done`/`lost` 时必填 | 关闭日期 |
| `next_action` | `active` 时必填 | 下一步做什么；空着等于没在管 |
| `next_action_due` | 建议 | 过期会进 `overdue` |
| `amount` | 可选 | 四项齐全，见金额铁律 |
| `evidence` | 是，至少一条 | 只追加不删除 |
| `status_history` | 是，至少一条 | 时间单调不减，最后一条的 `to` 必须等于当前 `stage` |

## 2. 状态机

```
        ┌──────────┐
        │  active  │◀────────────┐
        └────┬─────┘             │
     卡住    │      解除          │
             ▼                   │
        ┌──────────┐             │
        │ blocked  │─────────────┘
        └────┬─────┘
             │ 客户说不做 / 选了别家
             ▼
        ┌──────────┐     长期无动静（脚本提示，人工确认）
        │   lost   │◀──────────────  active ──▶ vanished
        └──────────┘
             ▲
             │ 该事项完成（不等于成交）
        ┌──────────┐
        │   done   │
        └──────────┘
```

允许的转移：

| from | to | 条件 |
|---|---|---|
| `active` | `blocked` | 卡在具体一件事，填得出 `blocked_by` |
| `blocked` | `active` | 卡点解除，写清怎么解的 |
| `active`/`blocked` | `done` | 事项本身完成，有证据 |
| `active`/`blocked` | `lost` | 客户明确放弃，必须有客户原话 evidence |
| `active` | `vanished` | 超过 2×`silence_days` 无证据追加，人工确认 |
| `vanished` | `active` | 客户重新联系，`status_history` 记「重启」 |
| `done`/`lost` | — | 终态，不再改。要重启就新建 item |

`done` ≠ 成交。`done` 是「这条事项做完了」，成交是 `stage=won`。一条商机可以有多个 done 的事项（试用开通 done、方案确认 done），整条商机仍然是 active。

## 3. 派生状态（脚本算，不靠记忆）

`check_ledger.py --suggest` 输出，**只建议不改文件**：

| 派生 | 条件 | 报告里怎么用 |
|---|---|---|
| `stalled` | `status=active` 且 `until - last_update_at > silence_days` | 进周报「风险与卡点」 |
| `overdue` | `status=active` 且 `next_action_due < until` | 进日报「明日动作」第一行 |
| `vanished` | `status=active` 且 `until - last_update_at > 2 × silence_days` | 进月报「客户结构」的失联区，人工确认后改 `status` |
| `stale_promise` | 最后一条 evidence 是我方承诺类话术，且之后无追加 | 进周报「我方承诺未闭环」 |

## 4. 更新规则

| 情况 | 动作 |
|---|---|
| 新推进线 | 新建 item；`opened_at` = 窗口内首次出现日期；`status_history` 第一条 `from: null` |
| 有进展 | 追加 evidence；更新 `last_update_at`、`stage`（如需）、`next_action`；追加 `status_history` |
| 没提到、没闭环 | **什么都不改**。不要因为没动静就降级，降级由脚本提示 + 人工决定 |
| 客户明确放弃 | `status=lost`，`closed_at`，附客户原话 evidence |
| 事项完成 | `status=done`，`closed_at`，附证据。商机整体别跟着关 |

`stage` 没变但有进展时，`status_history` 也要追加一条 `from == to`，note 写清进展。「时间单调不减」指的是这一串日期不许倒流。

## 5. 合并规则

发现两个 item 其实是同一条线（客户名对不上、或第一次整理时分错了）：

1. 保留 `opened_at` 较早的那个 `item_id`。
2. 把晚的 evidence 按 `at` 插入早的 evidence，去重按 `msg_id`。
3. 把晚的 `status_history` 按 `at` 插入，`from` 要对齐前一条的 `to`。
4. 删掉晚的 item。
5. **不要删早的 `item_id` 后重建**——历史 evidence 与上期报告的引用会失联。

## 6. 什么进台账，什么不进

进：能独立讲清楚的一条客户推进线，有阶段、有下一步。

不进：

- 单次触达（发资料、打招呼）—— 计入 `window.json` 的统计，不建 item
- 纯内部事务（申请折扣审批本身不是客户事项，是这个 item 的 `blocked_by`）
- 客户的人事变动 —— 除非影响推进，写成 `status_history` 的一条 note
- 我自己待办里的非客户事项（写周报、开会）—— 不进客户台账
- 部门群、通知群、跨客户的行业群 —— 群不是客户，只有「一个群 = 一个客户项目」才进

### 6.1 群聊怎么进台账

前提：这个群整个属于一个客户项目（见 `capture.md` 3.3）。确定后：

| 情况 | 做法 |
|---|---|
| 客户只在这个群里对接，没有单聊 | `client` 写客户主体，`channel: "group:<群名>"`，`conversation_key` 填群 key |
| 同一客户既有单聊又有群 | 仍然按 `客户 × 主题` 建 item，不按渠道建。证据可以混着引，但 `conversation_key` 只能填一个主渠道，另一条渠道的往来写进 `evidence` 的 `quote` 里 |
| 群里我方同事在推进（不是我） | 算这条线的进展，但 `next_action` 要写清「谁做」。同事答应了排期，我的下一步就是「盯梁伟下周一前出排期」，不是替同事承诺 |
| 群里客户方有多人 | `client` 用主导的那个（发需求 / 拍板的），其他人不各建 item |
| 分边未知（`client_side_known=false`） | 不进台账，先在报告里标「该群分边未确认」。分不清同事和客户时建立的事项，十有八九是错的 |

群里只 @ 我一句「收到」不算推进。判据还是那条：有没有让阶段前进的信息，或者有没有产生下一步。

## 7. 备份

台账是单文件、会长期积累、改错难回滚。**每次渲染报告前先把台账复制一份**到 `~/worklog/backup/ledger-<日期>.json`。这一步手动做，不用脚本，两秒钟的事。
