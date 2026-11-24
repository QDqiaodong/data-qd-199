# data-qd-199 · 乐谱小节与延音链

审核显式小节与声部事件的精确时值覆盖，诊断缺拍、重叠和越界，合并合法延音链并移调。

## 安装与运行

Python >=3.11，实际验证 CPython 3.12.14 + uv 0.12.19。运行依赖仅标准库；构建固定 setuptools 80.9.0、wheel 0.45.1。每个项目独立虚拟环境，公共下载缓存可以复用。

在本仓根目录执行：

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/score-spans --source fixtures/input.json --output run/demo.json
```

以上安装命令已在本地公共缓存存在时真实执行。首次使用且缓存为空时须先准备 Python 和锁定构建包，或在允许联网的安装环境移除对应 `--offline`；运行与测试不需要网络。`.venv` 互不共享，未纳入Git。

样例两小节、3事件（含休止），跨小节延音链n1→n2合并为[0,6]四分音符单位；音高60移调到62，诊断0。

## 演奏顺序窗口查询

除旧入口外，`score_spans.service.process_window(score, sections, first, stop)` 提供反复编排下的窗口查询，纯 Python 可直接调用、返回可 JSON 序列化对象，CLI 不变。两个入口使用不同时间轴，不可混用：

| 入口 | 时间轴 | 延音判定依据 |
| --- | --- | --- |
| `process` / CLI | 源谱顺序，事件用源小节偏移 | 源谱绝对坐标 |
| `process_window` | `sections` 反复后的演奏顺序，每次出现带 `ordinal` 与累加全曲起点 | 实际经过的下一次小节出现 |

`sections` 每项为 `{from_measure, through_measure, repeat}`，窗口 `[first, stop)` 为半开 ordinal 区间（1..8 个小节）。返回 `measures`/`spans`/`diagnostics`：链按整份编排的完整合法延音链定义，窗口只裁剪区间与可见成员，并给出首尾实际事件身份 `[ordinal, event_id]` 与整链成员数。编排不逐次展开，`repeat` 达到 10^9 时单次查询仍是毫秒级、内存与重复次数无关。字段、拒绝规则与诊断口径详见 [数据契约](docs/contract.md)。

最小调用示例：

```python
from score_spans.service import process_window
result = process_window(score, sections, first, stop)
```

## 语料发布：train/eval 数据集

`score_spans.corpus` 在旧入口之外提供本地语料发布接口，供数据管道把
补录、更正和撤回整理成可复现、防串集的两份数据集与隔离清单：

```python
from score_spans.corpus import publish_corpus, read_corpus
release = publish_corpus(store, request_id, expected_release, changes)
current = read_corpus(store)      # 默认当前版本
old = read_corpus(store, 1)       # 按编号原样读回历史版本
```

* 批次 1..100 项 `upsert`/`withdraw`，共同生效；`score` 沿用现有源谱
  结构检查，训练准入新增越界、同声部重叠、未兑现延音、无发音跨度
  四项拒绝（缺拍允许），任何一项失败拒绝整批。
* 样本取源谱顺序的发音跨度（合法延音合并、移调后音高），端点为规范
  精确分数字符串；内容指纹对整体平移、改名、等效延音写法不敏感，
  对节奏、相对音程、重复跨度数目和谱尾留白敏感。
* 共享曲族或内容指纹连成关系组；曾发布到某侧的 ID 留下历史锚点，
  组内两侧锚点同时出现时整组只进隔离清单，单侧锚点优先于新谱的
  分集建议。
* 版本递增、历史版本原样可读；同一 `request_id` 原样重试返回原版本，
  改参数报冲突；发布经原子改名一次生效，并发提交至多一个成功。

入库规则、关系证据与锚点、返回字段、重试顺序、错误类型、崩溃语义见
[语料发布契约](docs/corpus.md)。

## 起音标注对账：跨谱面修订的工作单

`score_spans.reconcile` 在旧入口之外提供本地对账接口，把校订方的谱面
修订与标注方先前领取的人工起音工作单对齐，产出当前待标注工作单和可供
下游直接使用的已确认标注行：

```python
from score_spans.reconcile import open_batch, Reconciler

reconciler = open_batch('batch-7', initial_score)
reconciler.apply_revision(1, revised_score, [['old_note', 'new_note']])
reconciler.submit('batch-7', ticket, 'accent')
delivery = reconciler.delivery()           # revision + pending + confirmed
reconciler = Reconciler.from_json(reconciler.to_json())  # 调用方保存/恢复
```

* 工作项粒度是源谱顺序的发音链（合法延音合并，休止不产生工作项）；
  复核内容为 `voice`、全曲绝对精确 `start/end`、移调后完整 `pitches`，
  事件 ID 只用于来源追溯。新入口拒绝重叠、越界、未兑现延音的谱，留白、
  纯休止或空事件表不妨碍开批或修订。
* 修订带校订方来源对 `[旧 event_id, 新 event_id]`，允许一变多、多变一；
  未声明来源的事件没有这层证据，同名也不例外。人工复核只在双向一一、
  成员被来源对完整覆盖、复核内容完全相同时沿用；竞争关联不挑"较像的
  一条"，不满足即作废旧票、为新链发新票。
* 回传只认批号匹配且当前仍待回传的本批票；同票重传、跨批、已作废票
  拒绝，批内用过的票号永不再分配。检查点只保存当前谱、当前链与固定
  数量的递增计数，不保存历次谱、关系或已销票明细，只凭最新 JSON 即可
  判定回传。字段、沿用条件、错误类型与检查点格式见
  [起音标注对账契约](docs/reconcile.md)。

## 排练终端断点续送：奏谱指令帧与恢复

`score_spans.terminal` 在旧入口之外提供本地数据接口，把谱面编译成外接
琴键提示终端只认的"输出通道、音高、开关"指令，并按终端回执断点续送；
只做结构化数据，不驱动硬件、不接网络、不做播放器或 CLI：

```python
from score_spans.terminal import open_transfer, TerminalDelivery

delivery = open_transfer('transfer-7', 0, score, {'v1': 1, 'v2': 1}, 2)
packet = delivery.next_packet()                 # 保存检查点后才发送
delivery.receive_receipt(packet['transfer_id'],
                         packet['terminal_code'],
                         packet['packet_index'])
delivery.record_reset(1)                        # 终端复位到更大代号
delivery.confirmed_position()                   # 最后确认的整帧位置
delivery = TerminalDelivery.from_json(delivery.to_json())
```

* 发音链按半开区间占用物理键 `(通道, 移调后音高)`：首次占用开、最后
  占用结束关，旧占用同刻全部结束而新占用开始时即使同键也先关后开；
  同刻关操作全部先于开操作，两组各按通道、音高升序，无操作时刻无帧，
  时间为规范精确分数字符串。合法延音链中间不重触发。
* 非空帧按每包至多 K（1..64）个操作分包，前面的包装满；包带传送标识、
  终端代号、代号内从 1 开始的包序号、帧类别（奏谱/恢复）、谱面时间、
  片号、总片数与操作。非末片回执只表示暂存，末片回执才整帧生效。
* 调用方串行：保存最新 JSON 检查点后才发包，回执先应用到检查点再取下
  一包；未确认前反复取包（含重载后）得到同一个完整包。检查点不累计
  已送包、过去代号或历次检查点。
* 终端复位到更大代号时保留最后确认的**整帧**位置，丢弃未确认分片；
  新代号先用恢复帧重开该位置应亮的键（每键一次、可分片、不推进位置），
  无键则跳过恢复；恢复途中再复位仍从同一位置重来，旧代号回执无效。

包协议、检查点保存/重载时机、恢复屏障与错误类型见
[排练终端断点续送契约](docs/terminal.md)。

## 录音切片配准与采集块回收

`score_spans.recording` 在旧入口之外提供归档端与采集端共用的本地接口：
把谱面时间轴上的切片单经对时锚点精确换算到 PCM 块目录的采样点轴，判定
每个切片单的交付状态、产出就绪清单，并决定哪些不可变 PCM 块当前可以
整块回收。只处理目录与结构化数据，**不读写 PCM 内容**，不管理跨进程
状态或对象存储：

```python
from score_spans.recording import RecordingSnapshot

snapshot = RecordingSnapshot(snap)   # 一次解析、建立索引，规划与读取复用
plan = snapshot.plan()               # 每单状态 + 可回收块 ID
manifest = snapshot.manifest('A')    # 按单号读取一份就绪清单
```

* 锚点 `(谱面位置, 采样点)` 首项 `(0,0)`、两列严格递增、不越全谱末尾，
  相邻锚点间精确直线换算，末锚点之外不外推；谱面位置用
  `[分子, 分母]`（分子 ≤10^9、分母 1..10^6），采样点与封口为
  0..2^53−1 整数（布尔拒绝）。
* 切片两端分别按所在锚点段换算，整数采样区间为
  `[ceil(F(a)), ceil(F(b)))`；区分已结束、等锚点、等块、就绪和两种原因
  的不可交付（空整数区间 / 封口以下永久空洞），不拿空洞当静音，目录
  完整即就绪、不要求等封口。
* 就绪清单给出实际采样区间、按采样顺序拼接的块与块内半开偏移，以及
  先在谱面轴裁剪再分段换算的发音链标签（声部、整组移调音高、整条链
  源事件 ID、相对整数起止采样点、是否含真正起音）；取不到采样点的
  标签省略，纯休止切片只要录音完整即可交付。
* 回收只放行完全位于封口内且无任何未结束切片依赖的块：就绪单保留原块
  到交接结束，等块单保留整个已确定区间碰到的块，等锚点单按
  `ceil(F(a))`（左端未知则末锚点采样点 +1）起保留无上界尾区间，不可
  交付与已结束单不占块；跨封口块不回收，多单共享等最后依赖消失。
* 规划只产出状态与区间，不预生成按单清单；块占用经差分数组汇总，
  20000 块 / 10000 重叠切片 / 2000 锚点规模下规划为亚秒级，不逐单扫块、
  不展开逐采样点数据。结果稳定且可 JSON 序列化。

快照形状、半开边界、封口与结束标记语义、回收规则、错误类型与测试口径
见 [录音切片配准契约](docs/recording.md)。

## 带踏板键盘补录：预览计划与写回当前谱

`score_spans.overdub` 在旧入口之外提供本地数据接口，把排练室补录的按键、
抬键和延音踏板消息整理成可预览的声部放置，再把已预览的计划写回可能已被
同事修改的谱面；不做音频、设备控制、文件或界面：

```python
from score_spans.overdub import plan_overdub, apply_overdub

plan = plan_overdub(score, 'take-7', 'm1', 48, messages)  # 只读输入
blob = json.dumps(plan, sort_keys=True)                  # 调用方保存
new_score = apply_overdub(current_score, json.loads(blob))
```

* tick 0 是起始小节开头；踏板初始抬起，抬键时踩下则延长到下一次抬踏板，
  抬踏板不结束仍按住的键；未配对或被踏板卡住的起音整批拒绝，不补到谱尾。
  同音两次起音各是一条独立发音，不合并。
* 每次起音从按键到实际结束的整个区间必须在同一候选声部；原事件（含显式
  休止）是不可覆盖的占位，新起音同声部不相交，端点相接可以。多个完整
  放置时按 `(按键 tick, 起音 ID)` 取声部 ID 序列字典序最小者；不能完整
  放置报 `no_placement`，不返回局部补录。
* 新发音按小节边界精确切分，只在自身相邻成员间声明延音；源事件音高反算
  `transpose`，写入精确分数时值。计划含绝对发音区间（规范分数字符串）、
  所选声部、按发音顺序的新事件 ID 和写回所需的新事件行，不嵌整份旧谱；
  新 ID 规则稳定，调换原事件/候选顺序不改变计划。
* 应用只写入计划的新增事件，不重跑声部选择；要求当前谱仍严格合规、移调
  与从首小节到最后一个新增片段所在小节的小节 ID/精确长度与预览一致，
  前缀内不占计划区间、不撞新 ID，前缀之外可自由变更。冲突整份拒绝，
  不改传入谱；同计划重复应用因新 ID 已占用而冲突。

计划格式、放置规则、错误码与应用边界见
[带踏板键盘补录契约](docs/overdub.md)。

## 运行测试

在本仓根目录（已安装本包或设置 `PYTHONPATH=src`）：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试包含旧接口回归、窗口链裁剪、段/轮次/跨段边界、覆盖审核与 `invalid_tie` 的 ordinal、非法参数拒绝，以及 10^9 次反复的冷启动计时用例；`tests/test_corpus.py` 另覆盖语料发布的桥接隔离、历史锚点、历史版本读取、等效指纹与区分边界、进程中断恢复、同版本并发写入、不合规批次整批拒绝与错误类别区分；`tests/test_reconcile.py` 覆盖起音对账的拆分/改名沿用、缺来源作废、竞争关联不挑选、改内容与恢复后的票号规则、多轮撤销重发的检查点边界、非法混合修订与过期号/无效回传/空谱/多对一改写的原子拒绝；`tests/test_terminal.py` 覆盖终端指令帧的先关后开与排序、延音链不重触发、K 分包、末片回执丢失与复位恢复屏障、恢复分片与途中再复位、回执拒收矩阵、空谱与非法输入、检查点损坏检测及本地接收端替身的端到端亮键核对；`tests/test_recording.py` 覆盖切片配准的两组验收例（锚点追加、结束标记、删块、封口 4/8 对比、分数边界不重不漏、分段换算与裁入延音不标起音）、跨封口/多单共享/左端待校准/相接不占用的回收规则、标签裁剪省略与稳定顺序、20000 块与 10000 个重叠切片的规模规划与单份清单、非法输入矩阵及旧入口回归；`tests/test_overdub.py` 覆盖踏板延音与同 tick 顺序、跨小节切分与移调还原、显式休止占位、字典序最小完整放置（含小规模独立枚举对照）、计划保存重载后的应用、同事修改的保留与前缀外变更、移调/小节长度/新 ID 三类冲突、旧谱与消息非法输入、未配对与踏板卡住的整批拒绝，以及旧入口回归中每个新起音一条独立链。

演示标准输出摘要：

```json
{"diagnostics": 0, "events": 3, "sounding_spans": 1}
```

完整证据写入 `run/demo.json`。数值输入为契约约定的十进制字符串或整数分数对，输出的精确有理数采用整数或 `n/d` 字符串，避免浮点舍入。

## 已执行验证

独立环境离线构建安装、依赖一致性检查、6项业务测试及安装后的命令行端到端演示通过。另核重复目标、符号链接目标和重复JSON键都返回2，既有输出字节不变，非法输入不发布报告。输出路径必须未存在，重新演示请选新文件名。

源码入口为 `src/score_spans/service.py`，测试在 `tests/`。业务字段、身份、单位、界限和失败约定见 [数据契约](docs/contract.md)。样例全部为原创合成数据，见 [样例来源](fixtures/SOURCES.md)，源码和样例采用MIT许可。

## 当前边界

`process`/`process_window` 不处理音频、踏板、速度或调号拼写；踏板只作为
`overdub` 的采集消息参与发音区间计算，写回后的谱面不含踏板事件。
`process`/CLI 的源谱入口本身不含反复段，反复编排只通过 `process_window`
的 `sections` 表达。存在诊断仍输出报告，CLI成功不等于谱面合规。

本仓提供当前文档描述的业务基线；尚未实现的演进能力见契约中的集成说明，不承诺固定数量的后续任务。
