# 语料发布契约：publish_corpus / read_corpus

`score_spans.corpus` 为数据管道提供本地语料发布接口，产出可直接消费的
训练集、评估集与隔离清单。纯 Python、可 JSON 序列化、不依赖外部服务；
不做播放器、页面或命令行。谱面模型沿用 [数据契约](contract.md)，
`process` / `process_window` / CLI 行为不变。

```python
from score_spans.corpus import publish_corpus, read_corpus
release = publish_corpus(store, request_id, expected_release, changes)
current = read_corpus(store)          # 当前版本
old = read_corpus(store, 1)           # 按编号读历史版本
```

`store` 是每个语料库独立的本地目录（不存在时由发布方创建）。新库的
版本 0 是空集：`{'release_id': 0, 'train': [], 'eval': [], 'quarantine': []}`。

## 变更与批次

`changes` 为 1..100 项的数组，同一批次不得重复操作同一 `score_id`；
对不同 ID 的变更共同生效（一次发布一版），不按输入顺序逐条发布。
`score_id`、`family_id`、`request_id` 沿用仓库标识符约束
（`[A-Za-z0-9_:-]{1,64}`）。两种变更：

* `{'op': 'upsert', 'score_id', 'family_id', 'preferred_split', 'score'}`：
  登记或更正一份谱。`preferred_split` 为 `'train'` 或 `'eval'`，是
  初次分集建议；`score` 为完整源谱（`measures`/`events`/`transpose`）。
* `{'op': 'withdraw', 'score_id'}`：撤回一份当前有效的谱；该 ID 必须
  当前有效，否则报 `UnknownScore`。

`upsert` 的 `score` 先过现有源谱结构检查（`model.parse`），再过训练
准入检查；任何一项失败都拒绝整批，不产生新版本、不留下关系证据。

## 训练准入规则

出现以下任一情况拒绝整批（`InvalidChange`）：

* 事件越出所属小节（`overrun`）；
* 同声部事件重叠（`overlap`）；
* 存在未兑现延音（源谱坐标下的 `invalid_tie`）；
* 没有任何发音跨度（无事件或全是休止）。

缺拍（`gap`）本身不拒绝：声部允许留白。准入只影响语料发布，不改变
`process` 的诊断口径。

## 发布行与内容指纹

样本以源谱顺序的发音跨度为准（合法延音链合并、移调后音高），不使用
演奏反复编排。`train`/`eval` 每行：

```json
{"score_id": "A", "family_id": "F", "duration": "8",
 "spans": [{"start": "1/3", "end": "2", "pitches": [60, 64]}]}
```

* `family_id` 为当前有效登记；`duration` 为全谱时长（含谱尾留白）。
* 时长与跨度端点为规范精确分数字符串（整数如 `"4"`，否则约分 `"n/d"`）。
* 每条跨度只含起点、终点和移调后完整音高数组，不带声部名、事件 ID
  或延音链成员；跨度按起点数值、终点数值、音高数组排序，相同的行
  重复几次保留几次。

**内容指纹**：全谱时长 + 把每个音高减去该谱最低发音音高后的跨度
多重集。两样本指纹相同当且仅当两者都相同。因此整体平移、改名、把
等效长音改写为合法延音（或反向）都不能把数据洗到另一集合；节奏、
相对音程、重复跨度数目和谱尾留白仍能区分样本。分数精确比较，不用
浮点或近似相似度。

## 曲族证据、关系组与历史锚点

* 每次成功入库的 `upsert` 留下证据：该 `score_id` 与当次 `family_id`、
  当次内容指纹属于同一组关系。共享曲族或内容指纹连成同组，关系可
  传递。谱 ID、曲族 ID、内容指纹是不同身份空间。
* 更正谱面、改曲族或撤回只改变当前有效样本，不删除已接纳的关系：
  撤回后别名和旧内容依然可能出现在另一来源。相同 ID 重新登记延续
  原身份（新旧证据挂在同一身份上）。
* 曾随任意成功版本发到 `train` 或 `eval` 的 ID，保留那一次的分集
  作为历史锚点，即使后来不再有效或被隔离；锚点不改写。
* 每次发布对更新后的整个关系组统一分集：
  * 只看到一侧历史锚点：组内所有当前有效样本进入该侧；
  * 没有锚点：按组内当前有效样本中 ID 字典序最小者的
    `preferred_split`；
  * 两侧锚点同时出现：整组当前有效样本只进隔离清单，不得继续留在
    任一集合。
* 没有当前有效样本的组不输出行，但其证据仍有效。

## 返回字段

`publish_corpus` 与 `read_corpus` 返回可 JSON 序列化对象：

* `release_id`：递增整数版本号；
* `train` / `eval`：上述发布行，按 `score_id` 排序；
* `quarantine`：隔离行，按 `score_id` 排序。除发布行字段外给出
  `train_anchors` / `eval_anchors`（组内两侧历史锚点 ID 列表，按 ID
  排序），便于追查旧版串集。

同一版本每个当前有效 ID 恰好出现在其中一个列表。历史版本是已交付
的训练和评估输入，按交付时的内容原样重读，不按今天的证据重算。

## 重试与并发顺序

`publish_corpus` 的处理顺序：

1. 校验 `request_id` 形状并规范化请求（`expected_release` + 整批
   `changes`，按 JSON 值比较：对象键顺序不计，数组顺序计入）。
2. `request_id` 命中已成功请求：内容相同则**先于版本检查**原样返回
   当时版本（不推进当前版本）；内容不同报 `RequestConflict`。
3. `expected_release` 不等于当前版本：报 `ReleaseConflict`。
4. 校验整批变更（形状 → 撤回有效性 → 训练准入），全部通过才提交。

未成功提交的请求不占用 ID（可修正后以同 ID 重新提交）。两个进程基于
同一当前版本并发提交不同请求，至多一个成功；另一个收到
`ReleaseConflict`，其证据、锚点与结果不会进入后续发布。

## 错误类型

均可按异常类型或稳定 `code` 属性区分，基类 `CorpusError`：

| 类型 | code | 含义 |
| --- | --- | --- |
| `InvalidChange` | `invalid_change` | 非法输入（也是 `ValueError`） |
| `UnknownScore` | `unknown_score` | 撤回不存在的当前有效 ID |
| `UnknownRelease` | `unknown_release` | 读取未知历史版本 |
| `ReleaseConflict` | `release_conflict` | `expected_release` 与当前版本不一致 |
| `RequestConflict` | `request_conflict` | 同一 `request_id` 提交了不同内容 |

## 崩溃与存储

`store` 内唯一的可变文件是 `state.json`：关系证据、当前样本、历史
锚点、各版本输出与成功请求绑定作为一次发布，经"写临时文件 + fsync +
原子改名"同时生效；读者不会看到半份新版本。发布并发由 `store/lock`
上的 flock 串行化。进程内不保存任何状态：调用进程在提交前退出，重开
仍是旧版；在提交后、返回前退出，重开能看到完整新版本，以原
`request_id` 和原参数重放即可取回该版本。覆盖单机正常文件系统上的
进程中断与并发，不要求机器掉电或跨机同步。存储格式是实现细节，只应
通过 `publish_corpus` / `read_corpus` 访问。

测试专用的故障注入点 `corpus._FAULT_HOOKS`（`before_commit` /
`after_commit`）不属于生产接口。

## 测试

在本仓根目录（已安装本包或设置 `PYTHONPATH=src`）：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`tests/test_corpus.py` 覆盖：桥接隔离与锚点、历史版本原样可读、
等价写法同指纹（含非整数时值）、节奏/相对音程/重复跨度/谱尾留白
区分、单侧锚点优先于建议分集、提交点前后进程中断、同版本并发写入、
不合规谱混合批次整批拒绝、错误类别区分与旧入口回归。
