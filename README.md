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

## 运行测试

在本仓根目录（已安装本包或设置 `PYTHONPATH=src`）：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试包含旧接口回归、窗口链裁剪、段/轮次/跨段边界、覆盖审核与 `invalid_tie` 的 ordinal、非法参数拒绝，以及 10^9 次反复的冷启动计时用例。

演示标准输出摘要：

```json
{"diagnostics": 0, "events": 3, "sounding_spans": 1}
```

完整证据写入 `run/demo.json`。数值输入为契约约定的十进制字符串或整数分数对，输出的精确有理数采用整数或 `n/d` 字符串，避免浮点舍入。

## 已执行验证

独立环境离线构建安装、依赖一致性检查、6项业务测试及安装后的命令行端到端演示通过。另核重复目标、符号链接目标和重复JSON键都返回2，既有输出字节不变，非法输入不发布报告。输出路径必须未存在，重新演示请选新文件名。

源码入口为 `src/score_spans/service.py`，测试在 `tests/`。业务字段、身份、单位、界限和失败约定见 [数据契约](docs/contract.md)。样例全部为原创合成数据，见 [样例来源](fixtures/SOURCES.md)，源码和样例采用MIT许可。

## 当前边界

不处理音频、踏板、速度或调号拼写。`process`/CLI 的源谱入口本身不含反复段，反复编排只通过 `process_window` 的 `sections` 表达。存在诊断仍输出报告，CLI成功不等于谱面合规。

本仓提供当前文档描述的业务基线；尚未实现的演进能力见契约中的集成说明，不承诺固定数量的后续任务。
