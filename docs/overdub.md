# 带踏板键盘补录契约：plan_overdub / apply_overdub

`score_spans.overdub` 在旧谱面处理、演奏窗、语料发布、对账、终端与录音
入口之外，提供两个纯 Python、只依赖标准库的入口，把排练室补录的按键、
抬键与延音踏板消息整理成可预览的声部放置，再把这份已预览的计划写回
（可能已被同事修改的）当前谱：

```python
from score_spans.overdub import plan_overdub, apply_overdub

plan = plan_overdub(score, 'take-7', 'm1', 48, messages)
blob = json.dumps(plan, sort_keys=True)     # 调用方自行保存
plan = json.loads(blob)                     # 之后原样载入
new_score = apply_overdub(current_score, plan)
```

* 生成计划只读输入，不返回谱面、不改原谱；
* 应用计划只写入预览中的新增事件，不重新跑声部选择，不原地修改传入对象；
* 不做音频、设备控制、文件读写、并发或常驻状态；调用方负责保存计划、
  选择要应用的当前谱与最终持久化。

## 采集输入

```python
plan_overdub(score, batch_id, start_measure, ticks_per_quarter, messages)
```

* `score`：现有源谱，沿用 [数据契约](contract.md) 的 measures/events/
  transpose 形状。留白、纯休止与空事件表允许；失败口径见下文。
* `batch_id`：1..32 字符的 ID（字母、数字、`_:-`），不同补录批次不得
  复用同一号；由调用方保证唯一。
* `start_measure`：已有小节 ID。tick 0 对应它的开头，只用源谱顺序。
* `ticks_per_quarter`：每四分音符的 tick 数，取 48、96、480 或 960。
  布尔值不作整数。
* `messages`：1..256 条消息，按 tick 非降序给出；同 tick 以数组顺序为准。
  每条是带 `kind` 的 JSON 对象：

| kind | 字段 | 含义 |
| --- | --- | --- |
| `key_down` | `tick`、`onset_id`、`pitch`、`candidates` | 按下：起音 ID、听到的 MIDI 整数音高（0..127）、非空候选声部列表（至多 4 个、不重复） |
| `key_up` | `tick`、`onset_id` | 抬起对应起音 |
| `pedal` | `tick`、`on` | 布尔踏板状态；初始抬起 |

`kind` 也接受 `note_on`/`note_off`/`sustain` 等别名，`onset_id` 也接受
`id`/`key`，`pitch` 接受 `midi`，`candidates` 接受 `voices`，`on` 接受
`down`/`state`；未知字段整条拒绝。

### 发音区间

踏板初始抬起。每个起音 ID 只可按下一次、且必须在更晚的 tick 抬键一次；
按键/抬键消息的 tick 必须落在 `[0, 从 start_measure 到谱尾的 tick 总数]`
内，结束也须在谱尾内。

* 无踏板（或抬键前踏板已抬起）：抬键立即结束发音。
* 抬键时踏板踩下：发音延长到下一次抬起踏板；**抬踏板不结束仍按住的键**。
* 重复设置同一踏板状态无额外效果；同 tick 时数组顺序决定生效先后
  （例如踩踏板消息排在抬键消息之前，则该次抬键已被踏板延音）。
* 消息读完仍有按住、或仍等踏板释放的起音时，整批拒绝；**不把缺失的
  结束时间补成谱尾**。
* 相同音高的两次起音仍是两条独立发音，哪怕前一条仍被踏板保持；不得按
  音高合并、截掉旧音或消去新起音。

## 声部放置

每次起音从按键到实际结束的整个半开区间必须落在**同一个**候选声部；
该声部原有事件（包括显式休止）都是不可覆盖的占位，新起音在同一声部也
不能相交，端点相接不算冲突。候选 ID 不必已在谱上出现（空声部完全可放）。

若有多个完整放置，按 `(按键 tick, 起音 ID)` 排列所有起音，选声部 ID
序列**字典序最小**的完整放置。不能完整放置（含越界、候选全被占、新起音
互相冲突）时报 `NoPlacement`，**不返回局部补录**。

> 这不是"每个音取第一个空声部"的贪心：前一条起音必须为后一条留出可行
> 候选，实现以「起音下标 + 各声部当前最大终点」做记忆化搜索，精确求出
> 字典序最小完整解，而不是指数回溯。测试用小规模独立枚举对照。

## 写入记谱

* 每条新发音按小节边界精确切分；跨边界只在**它自己的**相邻成员间声明
  `tie_to`。不同起音即便同音、同声部且紧邻，也不连延音。
* 写入小节内精确的分数起点和时值；输出保持源谱 `start`/`duration` 的
  `[分子, 分母]` 形状，端点相接与边界归后一小节精确处理。
* 源事件音高须反算原谱 `transpose`，让现有处理得到采集到的实际音高
  （听到音高 − transpose）。反算越界、最终事件总数超过 2000、或结果
  不能表示成合法源谱时，整批拒绝。
* 不修改原小节、移调、原事件内容或它们的相对次序。

## 计划格式

计划是可直接 `json.dumps`/`json.loads` 的普通字典，**不嵌入整份旧谱**：

```text
format            固定 "score-spans.overdub/1"
batch_id          批号
start_measure     起始小节 ID
ticks_per_quarter tick 分辨率
transpose         预览时源谱的移调
prefix            从首小节到最后一个新增片段所在小节的有序 [{id, length}]，
                  length 为规范分数字符串
onsets            每次起音一项，按 (按键 tick, 起音 ID) 排列：
                    onset_id, voice, pitch,
                    start, end   —— 绝对发音区间，规范分数字符串
                    events       —— 按发音顺序的新事件 ID
new_events        全部新增事件的源谱形状行（含 onset_id 归属）：
                    id, onset_id, voice, measure, start, duration,
                    pitches, 可选 tie_to
```

新事件 ID 用稳定规则分配：`ob_` 加 `sha256(batch_id, onset_id, 片段序号)`
的前 24 个十六进制字符，只由批号、起音 ID 与片段序号决定。因此调换原
事件顺序或候选顺序、重新预览同一批，计划不变。若基础 ID 已与旧谱事件
或同批其它新事件同名，规则在此输入上追加一个递增盐值后缀再取摘要，保持
可复现且不静默改用无关 ID；计划内与绑定旧谱均无 ID 冲突。

## 应用计划

```python
apply_overdub(score, plan)
```

返回新谱，不原地修改 `score` 或 `plan`。只应用预览中的新增事件，不重新
跑声部选择。当前谱须仍符合严格谱面口径，并且：

* 移调与预览时一致；
* 从首小节到**最后一个新增片段所在小节**的有序小节 ID 与精确长度与
  预览时一致（`prefix`）。
* 该前缀内原事件可以变化，只要没有占据任何计划选定声部中的新增区间、
  没有撞新 ID，并且应用后仍符合源谱上限（2000 事件）和严格审核。
* 前缀之外可以自由增删小节与事件。

失败是**整份计划冲突**（`ApplyConflict`）：不自动改投别的声部、不覆盖
现有事件、不部分落谱，也不修改传入对象。同一已应用计划再次送来会与新
增 ID 冲突，不当作另一笔补录。计划自身形状非法（字段、格式、内部不一致）
报 `InvalidOverdub`；当前谱结构非法（重叠、越界、未兑现延音、越界移调
等）也按应用冲突报 `ApplyConflict`，因为被拒绝的是"这份计划套这份谱"。

### 适用范围

* 应用只认计划里固定下来的声部与区间，不会因为当前谱出现更好的放置而
  改投；若计划区间已被占用，就是冲突，调用方需重新调用 `plan_overdub`。
* 前缀之外的变更不影响应用；前缀内只要不碰选定声部的新增区间即可保留
  同事的修改。
* 移调变化、前缀内小节长度/顺序变化、在任意声部撞新 ID，都视为冲突，
  即使表面上不挤占声部。

## 错误类型

均可按异常类型或稳定 `code` 属性区分，基类 `OverdubError`：

| 类型 | code | 含义 |
| --- | --- | --- |
| `InvalidOverdub` | `invalid_input` | 非法参数、采集消息、旧谱或计划形状（也是 `ValueError`） |
| `NoPlacement` | `no_placement` | 不存在完整放置；不返回局部补录 |
| `ApplyConflict` | `apply_conflict` | 当前谱与计划不匹配或区间/ID 已占用；整份拒绝 |

## 与现有入口的关系

旧 `process`/CLI、`process_window`、`corpus`、`reconcile`、`terminal`、
`recording` 契约不变。补录写入后的谱可直接交给 `process` 回归验证：原
发音跨度不变，每个新增起音恰好落成一条独立链，具有预览中的声部、音高
与起止。测试见 `tests/test_overdub.py`。
