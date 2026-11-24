"""录音切片配准与采集块回收：归档端与采集端共用的本地接口。

一轨录音的一份完整一致快照包含录音 ID、源谱、对时锚点、当前可读 PCM 块
目录、封口采样点与切片单。本模块只处理目录与结构化数据，**不读写任何
PCM 内容**，也不管理跨进程状态或对象存储：

* :class:`RecordingSnapshot` 解析并索引一份快照，规划与按单读取复用同一
  套索引，保证二者基于同一份输入；
* :meth:`RecordingSnapshot.plan` 做全局规划：返回每个切片单的状态与当前
  可整块回收的块 ID；
* :meth:`RecordingSnapshot.manifest` 按单号读取一份就绪清单（采样区间、
  拼块偏移与配准到采样轴的发音链标签）。

模块级便捷入口 :func:`plan_snapshot` 与 :func:`read_manifest` 适合一次性
调用；同一快照既要规划又要读清单时，请显式构建一个 :class:`RecordingSnapshot`
复用。所有时间换算使用 :class:`fractions.Fraction` 精确完成，输出中不含
分数（采样轴端点一律整数），结果可直接 JSON 序列化。

半开边界、封口、结束标记、状态口径、回收规则与错误类型见
docs/recording.md。
"""

from bisect import bisect_left, bisect_right
from fractions import Fraction

from .audit import audit_measures, chains
from .checks import array, fields, ident
from .model import parse

MAX_BLOCKS = 20_000
MAX_TICKETS = 10_000
MAX_ANCHORS = 2_000
SAMPLE_LIMIT = 2 ** 53 - 1
POSITION_NUM_MAX = 1_000_000_000
POSITION_DEN_MAX = 1_000_000

# 切片单状态。
CLOSED = 'closed'              # 交接已结束（或调用方放弃），不再交付、不占块
AWAIT_ANCHOR = 'await_anchor'  # 右端（或两端）尚不能换算，等后续锚点
AWAIT_BLOCK = 'await_block'    # 区间可换算，目录仍缺采样点且缺口尚未封口
READY = 'ready'                # 区间完整可交付
UNDELIVERABLE = 'undeliverable'  # 永久性不可交付

# 不可交付的两个原因。
EMPTY_INTERVAL = 'empty_interval'  # 换算后整数区间没有任何采样点
SEALED_GAP = 'sealed_gap'          # 封口以下存在永远不会补来的未覆盖采样点


class RecordingError(Exception):
    """录音快照错误基类，``code`` 为稳定机器可读错误码。"""

    code = 'recording_error'


class InvalidSnapshot(RecordingError, ValueError):
    """快照形状或不变量非法（也是 ``ValueError``），不交付任何部分结果。"""

    code = 'invalid_snapshot'


class UnknownTicket(RecordingError):
    """按单读取时单号在快照中不存在。"""

    code = 'unknown_ticket'


class TicketNotReady(RecordingError):
    """单号存在但切片单当前不是就绪状态，不能吐出清单。"""

    code = 'ticket_not_ready'


def _sample(value):
    """采样点/封口：0..2^53-1 的整数，布尔值显式拒绝。"""
    if type(value) is not int or not 0 <= value <= SAMPLE_LIMIT:
        raise InvalidSnapshot(
            f'sample point must be an integer in 0..2^53-1, got {value!r}')
    return value


def _position(value):
    """谱面位置：[非负分子, 正分母]，分子 <= 10^9，分母 1..10^6。"""
    if not isinstance(value, list) or len(value) != 2:
        raise InvalidSnapshot('position must be a [numerator, denominator] pair')
    numerator, denominator = value
    if type(numerator) is not int \
            or not 0 <= numerator <= POSITION_NUM_MAX:
        raise InvalidSnapshot(
            f'position numerator must be an integer in 0..{POSITION_NUM_MAX}')
    if type(denominator) is not int \
            or not 1 <= denominator <= POSITION_DEN_MAX:
        raise InvalidSnapshot(
            f'position denominator must be an integer in 1..{POSITION_DEN_MAX}')
    return Fraction(numerator, denominator)


def _strict_score(score):
    """解析并严格校验源谱，返回 (measures, events, 发音链列表)。

    越界（overrun）、同声部重叠（overlap）或无效延音（invalid_tie）拒绝；
    留白（gap）、纯休止与空事件表允许，休止不产生发音链。
    """
    try:
        measures, events = parse(score)
    except ValueError as e:
        raise InvalidSnapshot(f'invalid score: {e}') from e
    for diagnostic in audit_measures(measures, events):
        if diagnostic['kind'] in ('overlap', 'overrun'):
            raise InvalidSnapshot(
                f"score not admitted: {diagnostic['kind']} in voice "
                f"{diagnostic['voice']} measure {diagnostic['measure']}")
    chain_list, tie_diagnostics = chains(events)
    if tie_diagnostics:
        raise InvalidSnapshot('score not admitted: unfulfilled tie')
    return measures, events, chain_list


class RecordingSnapshot:
    """一份不可变录音快照的解析结果与索引；调用方串行使用，不做并发。

    构建期完成全部校验（任何失败抛 :class:`InvalidSnapshot`，不产生部分
    结果）并建立两套只读索引：按起点排序的块目录（含前缀覆盖长度）与
    锚点段。规划与清单都走区间查询（二分 + 差分数组），不逐单扫描全部
    块，也不展开逐采样点数据。
    """

    def __init__(self, snapshot):
        try:
            self._build(snapshot)
        except InvalidSnapshot:
            raise
        except ValueError as e:
            # checks/parse 等底层助手抛出的普通 ValueError 统一为快照错误，
            # 仍然是 ValueError 的子类；任何失败都发生在索引建成之前。
            raise InvalidSnapshot(str(e)) from e

    def _build(self, snapshot):
        if not isinstance(snapshot, dict):
            raise InvalidSnapshot('snapshot must be an object')
        fields(snapshot, ('recording_id', 'score', 'anchors', 'blocks',
                          'seal', 'tickets'))
        self._recording_id = ident(snapshot['recording_id'])
        measures, _events, chain_list = _strict_score(snapshot['score'])
        self._score_length = (measures[-1]['offset'] + measures[-1]['length']
                              if measures else Fraction(0))
        # 发音链已按 (起点, 声部, 事件 id) 排序，事件 ID 按链内时间排列。
        self._chains = chain_list

        self._anchor_positions, self._anchor_samples = \
            self._build_anchors(snapshot['anchors'])
        self._block_ids, self._starts, self._ends, self._prefix = \
            self._build_blocks(snapshot['blocks'])
        self._seal = _sample(snapshot['seal'])
        self._tickets, self._ticket_by_id = self._build_tickets(
            snapshot['tickets'])

    # ------------------------------------------------------------------ #
    # 校验与索引
    # ------------------------------------------------------------------ #
    def _build_anchors(self, raw):
        array(raw, 1, MAX_ANCHORS)
        positions, samples = [], []
        for item in raw:
            fields(item, ('position', 'sample'))
            positions.append(_position(item['position']))
            samples.append(_sample(item['sample']))
        if positions[0] != 0 or samples[0] != 0:
            raise InvalidSnapshot('first anchor must be (0, 0)')
        for i in range(1, len(positions)):
            if positions[i] <= positions[i - 1]:
                raise InvalidSnapshot('anchor positions must be strictly increasing')
            if samples[i] <= samples[i - 1]:
                raise InvalidSnapshot('anchor samples must be strictly increasing')
        if positions[-1] > self._score_length:
            raise InvalidSnapshot('anchor position beyond score end')
        return positions, samples

    def _build_blocks(self, raw):
        array(raw, 0, MAX_BLOCKS)
        parsed = []
        seen_ids = set()
        for item in raw:
            fields(item, ('id', 'start', 'end'))
            block_id = ident(item['id'])
            if block_id in seen_ids:
                raise InvalidSnapshot(f'duplicate block id: {block_id}')
            seen_ids.add(block_id)
            start = _sample(item['start'])
            end = _sample(item['end'])
            if end <= start:
                raise InvalidSnapshot('block must have positive length')
            parsed.append((block_id, start, end))
        # 目录顺序不代表时间：统一按起点排序后校验相交（相邻允许）。
        parsed.sort(key=lambda row: (row[1], row[0]))
        for prev, cur in zip(parsed, parsed[1:]):
            if cur[1] < prev[2]:
                raise InvalidSnapshot(
                    f'blocks {prev[0]!r} and {cur[0]!r} overlap')
        ids = [row[0] for row in parsed]
        starts = [row[1] for row in parsed]
        ends = [row[2] for row in parsed]
        # prefix[k] = 前 k 个块的长度之和（块互不相交，即其覆盖采样点数）。
        prefix = [0]
        for start, end in zip(starts, ends):
            prefix.append(prefix[-1] + (end - start))
        return ids, starts, ends, prefix

    def _build_tickets(self, raw):
        array(raw, 0, MAX_TICKETS)
        tickets, by_id = [], {}
        seen_ids = set()
        for item in raw:
            fields(item, ('id', 'start', 'end', 'closed'))
            ticket_id = ident(item['id'])
            if ticket_id in seen_ids:
                raise InvalidSnapshot(f'duplicate ticket id: {ticket_id}')
            seen_ids.add(ticket_id)
            start = _position(item['start'])
            end = _position(item['end'])
            if not start < end:
                raise InvalidSnapshot(
                    f'ticket {ticket_id!r} must have positive length')
            if end > self._score_length:
                raise InvalidSnapshot(
                    f'ticket {ticket_id!r} extends beyond score end')
            if type(item['closed']) is not bool:
                raise InvalidSnapshot(
                    f'ticket {ticket_id!r} closed flag must be a boolean')
            entry = {'id': ticket_id, 'start': start, 'end': end,
                     'closed': item['closed']}
            tickets.append(entry)
            by_id[ticket_id] = entry
        return tickets, by_id

    # ------------------------------------------------------------------ #
    # 锚点段换算与块区间查询
    # ------------------------------------------------------------------ #
    def _map(self, position):
        """谱面位置 -> 采样点（精确 Fraction）；调用方保证不越末锚点。"""
        positions, samples = self._anchor_positions, self._anchor_samples
        i = bisect_right(positions, position) - 1
        if i == len(positions) - 1:
            return Fraction(samples[-1])  # 末锚点自身可用，之外不可外推
        p0, p1 = positions[i], positions[i + 1]
        s0, s1 = samples[i], samples[i + 1]
        return Fraction(s0) + (s1 - s0) * (position - p0) / (p1 - p0)

    def _block_slice(self, lo, hi=None):
        """返回与整数区间 [lo, hi) 有正长度交集的块下标半开区间 [i0, i1)。

        ``hi`` 为 None 表示无上界的尾区间。块尾恰好等于 ``lo``（相接）
        不算占用；块互不相交，命中块在排序数组中必为连续切片。
        """
        i0 = bisect_right(self._ends, lo)
        i1 = len(self._block_ids) if hi is None \
            else bisect_left(self._starts, hi)
        return i0, i1

    def _covered_count(self, lo, hi):
        """块目录对整数区间 [lo, hi) 内采样点的覆盖数（跨封口块照常计数）。"""
        if hi <= lo:
            return 0
        i0, i1 = self._block_slice(lo, hi)
        if i0 >= i1:
            return 0
        total = self._prefix[i1] - self._prefix[i0]
        total -= max(0, lo - self._starts[i0])       # 左端块在 lo 之前的部分
        total -= max(0, self._ends[i1 - 1] - hi)     # 右端块越过 hi 的部分
        return total

    def _status(self, ticket):
        """计算单个切片单的状态与可换算时的整数采样区间 [lo, hi)。"""
        if ticket['closed']:
            return CLOSED, None, None
        last_position = self._anchor_positions[-1]
        if ticket['end'] > last_position:
            return AWAIT_ANCHOR, None, None
        lo = _ceil(self._map(ticket['start']))
        hi = _ceil(self._map(ticket['end']))
        if lo == hi:
            return UNDELIVERABLE, lo, hi
        # 封口以下的任何空洞都是永久缺口：整单不可交付，不拿空洞当静音。
        sealed_end = min(hi, self._seal)
        if sealed_end > lo \
                and self._covered_count(lo, sealed_end) < sealed_end - lo:
            return UNDELIVERABLE, lo, hi
        if self._covered_count(lo, hi) < hi - lo:
            return AWAIT_BLOCK, lo, hi
        return READY, lo, hi

    # ------------------------------------------------------------------ #
    # 公共入口
    # ------------------------------------------------------------------ #
    def plan(self):
        """全局规划：每单状态（稳定按单号排序）与可整块回收的块 ID。

        规划只产出状态与采样区间，不预生成任何按单清单或标签；块占用用
        块下标差分数组汇总，复杂度 O(T log N + N)，与切片重叠次数无关。
        """
        rows = []
        occupancy = [0] * (len(self._block_ids) + 1)
        for ticket in self._tickets:
            status, lo, hi = self._status(ticket)
            row = {'id': ticket['id'], 'status': status}
            # 可换算出整数区间的状态（等块/就绪/不可交付）都带上区间。
            if lo is not None:
                row['sample_start'] = lo
                row['sample_end'] = hi
            if status == UNDELIVERABLE:
                row['reason'] = EMPTY_INTERVAL if lo == hi else SEALED_GAP
            rows.append(row)
            if status in (READY, AWAIT_BLOCK):
                # 就绪单也要保留原块直到交接结束；等块单保留整个已确定
                # 采样区间碰到的现存块。
                i0, i1 = self._block_slice(lo, hi)
            elif status == AWAIT_ANCHOR:
                # 为后续补锚点保留素材：左端可换算从 ceil(F(a)) 起，否则
                # 从最后锚点采样点 +1 起；上界未知，保留所有碰到尾区间的块。
                if ticket['start'] <= self._anchor_positions[-1]:
                    tail_lo = _ceil(self._map(ticket['start']))
                else:
                    tail_lo = self._anchor_samples[-1] + 1
                i0, i1 = self._block_slice(tail_lo, None)
            else:
                continue  # 已结束或不可交付：不占用任何块
            if i1 > i0:
                occupancy[i0] += 1
                occupancy[i1] -= 1
        rows.sort(key=lambda row: row['id'])

        reclaimable = []
        used = 0
        for index, block_id in enumerate(self._block_ids):
            used += occupancy[index]
            if used == 0 and self._ends[index] <= self._seal:
                reclaimable.append(block_id)
        reclaimable.sort()
        return {'recording_id': self._recording_id,
                'tickets': rows,
                'reclaimable_blocks': reclaimable}

    def manifest(self, ticket_id):
        """按单号读取就绪清单；未知单号或非就绪单明确拒绝，无半份输出。"""
        if not isinstance(ticket_id, str):
            raise UnknownTicket(f'unknown ticket id: {ticket_id!r}')
        ticket = self._ticket_by_id.get(ticket_id)
        if ticket is None:
            raise UnknownTicket(f'unknown ticket id: {ticket_id!r}')
        status, lo, hi = self._status(ticket)
        if status != READY:
            raise TicketNotReady(
                f'ticket {ticket_id!r} is {status}, not {READY}')

        i0, i1 = self._block_slice(lo, hi)
        block_rows = []
        for index in range(i0, i1):
            start, end = self._starts[index], self._ends[index]
            block_rows.append({
                'id': self._block_ids[index],
                'offset_start': max(lo, start) - start,
                'offset_end': min(hi, end) - start,
            })

        labels = []
        for chain in self._chains:
            # 先在谱面轴上裁剪链，再按各端所在锚点段分别精确换算。
            clip_start = max(chain['start'], ticket['start'])
            clip_end = min(chain['end'], ticket['end'])
            if clip_end <= clip_start:
                continue
            label_lo = _ceil(self._map(clip_start))
            label_hi = _ceil(self._map(clip_end))
            if label_lo == label_hi:
                continue  # 裁剪后取不到任何采样点的标签省略
            contains_onset = ticket['start'] <= chain['start'] \
                < ticket['end']
            labels.append({
                'voice': chain['voice'],
                'pitches': list(chain['pitches']),
                'events': list(chain['events']),
                'relative_start': label_lo - lo,
                'relative_end': label_hi - lo,
                'contains_onset': contains_onset,
                '_sort_sample': label_lo,
            })
        labels.sort(key=lambda label: (label['_sort_sample'], label['voice'],
                                       label['events'][0]))
        for label in labels:
            del label['_sort_sample']

        return {'recording_id': self._recording_id,
                'ticket_id': ticket_id,
                'sample_start': lo,
                'sample_end': hi,
                'blocks': block_rows,
                'labels': labels}


def _ceil(value):
    """非负 Fraction 的精确向上取整（不经过浮点）。"""
    return -((-value.numerator) // value.denominator)


def plan_snapshot(snapshot):
    """一次性规划入口：等价于 ``RecordingSnapshot(snapshot).plan()``。"""
    return RecordingSnapshot(snapshot).plan()


def read_manifest(snapshot, ticket_id):
    """一次性按单读取入口：等价于 ``RecordingSnapshot(snapshot).manifest(id)``。"""
    return RecordingSnapshot(snapshot).manifest(ticket_id)
