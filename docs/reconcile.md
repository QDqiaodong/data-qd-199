# 起音标注对账契约：open_batch / Reconciler

`score_spans.reconcile` 在旧谱面处理、演奏窗、语料发布入口与 CLI 之外，
提供一套本地可调用的对账接口，把校订方的谱面修订与标注方的人工起音
工作单对齐：既不因事件改名丢弃人工结果，也不让已撤销的工作结果借同名
新音符回到交付里。纯 Python、只依赖标准库，状态可经标准 JSON 保存和
载入，不要求服务、并发写入或由本模块管理调用方的文件。

```python
from score_spans.reconcile import open_batch, Reconciler

reconciler = open_batch('batch-7', initial_score)          # 修订号 0
reconciler.apply_revision(1, revised_score, source_pairs)  # 成功后修订号 +1
reconciler.submit('batch-7', ticket, 'accent')             # 不推进修订号
delivery = reconciler.delivery()
blob = reconciler.to_json()                                 # 调用方自行保存
reconciler = Reconciler.from_json(blob)                     # 多年后原样恢复
```

调用方串行更新并只保存最新的 `to_json()` 字符串。所有写操作要么整体
生效，要么抛异常且状态字节级不变。

## 链与复核内容

工作项的粒度是**当前谱的发音链**：沿用源谱顺序的链定义（合法延音合并，
见 [数据契约](contract.md)），未连延音的发音事件本身就是一条链，休止
（空 `pitches`）不产生工作项。每条链的复核内容为：

* `voice`；
* 全曲绝对 `start` / `end`，精确分数比较，公开端点用规范分数字符串
  （整数如 `"4"`，否则约分 `"n/d"`）；
* 移调后的完整 `pitches` 数组。

事件 ID 只用于来源追溯，**不是复核内容**。开批与修订入口比旧 `process`
入口更严格：不接受有同声部重叠（`overlap`）、越界（`overrun`）或未兑现
延音（`invalid_tie`）的谱；留白（`gap`）、纯休止或空事件表不妨碍开批
或修订，对应交付就是两份空数组。

## 修订与来源关系

`apply_revision(revision, score, sources)` 输入当前修订号、完整的新谱和
校订方提供的来源对数组 `[[旧 event_id, 新 event_id], ...]`。

* `revision` 必须恰为当前修订号加一，否则报 `StaleRevision`；不能拿某版
  关系去对另一版谱。
* 来源对左右各在相应谱内解析，必须是非休止事件；重复对、引用不存在的
  事件、把休止作为来源端、数组形状错误都报 `InvalidSource`，整次修订
  拒绝。同一事件可以出现在多对里，允许一变多、多变一。
* 未声明来源的事件视为没有这层证据，**同名也不例外**。

### 人工复核的沿用条件

一条旧链的未消费票或已确认标签，只在下列条件**同时满足**时沿用到一条
新链：

1. 旧链成员在来源关系里只连到这一条新链；
2. 该新链成员也只连回这一条旧链（双向一一，没有第二条链声明竞争）；
3. 旧链和新链均无未被来源对覆盖的成员；
4. 两条链的复核内容（voice、绝对起止、完整音高）完全相同。

因此纯改名、拆成数个精确相接的合法延音（1→N）都可以保留复核；只对齐
音高、时间相交、同名、只对齐链首都不能代替上述条件。若有另一条链也
声明来源（例如多对一合并，或一个新成员同时被两条旧链声明），不存在
"挑其中较像的一条"：相关链一律不沿用。

不满足沿用条件时：旧链失去待回传资格，其旧标签不再出现在当前交付；
每条不能沿用的新链产生一张**新的**待回传工作单。满足沿用条件的链保留
未消费票的回传资格或已确认标签，但当前交付始终使用**新谱的事件成员**。

## 工作单、回传与交付

* 每条当前链恰好对应一张待回传工作单（`pending`）或一条已确认标注
  （`confirmed`），二者互斥且并集覆盖全部当前链。
* 工作单含批号与批内唯一票号（从 0 起的整数）。`submit(batch_id, ticket,
  label)` 只接受批号匹配、当前仍待回传的本批票，标签必须是 `plain`、
  `accent`、`tenuto` 之一。成功后关闭工作单、对其指向的当前链交付标签，
  不增加谱面修订号。
* 同票再次回传、其他批的票、已作废的票都报 `TicketRejected`；非法标签
  报 `InvalidLabel`。不能用整体修订号代替票的有效性：一张工作单可以
  连续跨越多次不改变其复核的修订。
* 批内用过的票号永不再分配，即使以后同名同内容的链重新出现；票号由
  单调计数器发放，已消费票不保留明细，因此无法被旧工作单"复活"。
* `delivery()` 返回 `{'revision', 'pending', 'confirmed'}`。每行带当前链
  的复核内容（`voice`、`pitches`、`start`、`end`）与按时间排列的当前
  成员 ID 数组 `events`；`pending` 行另含 `batch_id` 与 `ticket`，
  `confirmed` 行另含 `label`。两份数组各自按 `(start 数值, voice,
  链首事件 ID)` 排序；输入事件与来源对的数组顺序不影响结果。

## 检查点

`to_json()` / `from_json()` 使用标准 JSON（`sort_keys`、拒绝非有限值）。
检查点只保存判定回传所必需的最新状态：

```text
format      固定 "score-spans.reconcile/1"
batch_id    批号
revision    当前修订号（固定数量的递增数量值）
next_ticket 下一个可发票号（固定数量的单调计数器）
score       当前谱（一份，不是历次谱）
entries     每条当前链一项：复核内容、当前成员、open_ticket 与 label 二选一
```

检查点**不保存**历次谱、来源关系、已销票明细或任何随修订/回传次数
增长的集合；业务记录的保留量只随当前谱与当前链数变化，另加固定数量的
递增计数。一批可以反复校订多年，标注方手里的工作单不受控制：只凭最新
检查点即可判定回传——不在当前 `open_ticket` 中的票一律拒绝。
`from_json` 会重算当前谱的链并核对每条 entry 的不变量（成员、复核内容、
票/标签互斥、票号落在计数器范围内、开放票不重复），任何破坏报
`InvalidCheckpoint`。检查点格式是实现细节，只应通过这两个方法访问。

## 错误类型

均可按异常类型或稳定 `code` 属性区分，基类 `ReconcileError`：

| 类型 | code | 含义 |
| --- | --- | --- |
| `InvalidScore` | `invalid_score` | 谱面结构非法，或含重叠、越界、未兑现延音（也是 `ValueError`） |
| `InvalidSource` | `invalid_source` | 来源对形状、重复、不存在、休止端或跨谱不匹配（也是 `ValueError`） |
| `StaleRevision` | `stale_revision` | 修订号不是当前修订号加一（过期/跳号修订） |
| `TicketRejected` | `ticket_rejected` | 票不存在、已作废、跨批或重复回传 |
| `InvalidLabel` | `invalid_label` | 标签不在 plain/accent/tenuto 内（也是 `ValueError`） |
| `InvalidCheckpoint` | `invalid_checkpoint` | JSON 检查点形状或不变量被破坏 |

任何修订或回传被拒绝时，原谱、当前修订号、有效票和已确认标注均不变；
失败不会使其他链接丢标或误收。

## 测试

在本仓根目录（已安装本包或把 `src` 加入 `sys.path`）：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`tests/test_reconcile.py` 覆盖：

1. 拆分接续与改名后仅凭最新检查点回传、缺一条来源即作废旧票、竞争来源
   下两条链都不沿用；
2. 改音高/位置使已确认标签退出并出新票、改后恢复含旧 ID 的原谱也不
   复活旧票、多轮撤销重发后检查点形状固定且不累积失效票或修订记录；
3. 含非法来源对的混合修订、过期修订号、无效回传、空谱与纯休止、已确认
   标签的多对一合法延音改写，以及失败原子性（其他链不丢标、不误收）、
   检查点损坏检测与旧入口回归。

## 边界

本模块不改变 `process` / `process_window` / 语料发布 / CLI 的任何行为，
不做演奏反复编排（对账只用源谱顺序的链）、不持久化文件、不处理并发
写入；调用方负责批号的全局唯一性与检查点文件的保存。
