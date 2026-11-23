"""排练终端的断点续送：把谱面转成"输出通道、音高、开关"指令帧。

外接琴键提示终端只认物理键指令，不知道谱上有几个声部。本模块在旧谱面
处理、演奏窗、语料发布、对账入口与 CLI 之外，提供一套纯 Python、只依赖
标准库的本地数据接口（不驱动硬件、不接网络、不做播放器或 CLI）：

* :func:`open_transfer`：用调用方提供的唯一传送标识、初始终端代号、源谱、
  声部到通道映射与每包操作上限 K 开传；
* :meth:`TerminalDelivery.next_packet`：取下一包（未确认前反复取到同一个）；
* :meth:`TerminalDelivery.receive_receipt`：应用终端回执；
* :meth:`TerminalDelivery.record_reset`：记录终端复位（代号变大）；
* :meth:`TerminalDelivery.confirmed_position`：读取最后确认的整帧奏谱位置；
* :meth:`TerminalDelivery.to_json` / :meth:`TerminalDelivery.from_json`：
  标准 JSON 检查点保存与载入。

调用方串行操作：保存最新 JSON 检查点后才发送取出的包；回执先应用到检查点
再取下一包。状态只保存在调用方进程里，本模块不管理文件、不处理并发。

帧语义、分包/回执/复位规则与检查点格式见 docs/terminal.md。
"""

import copy
import json
from collections import defaultdict

from .audit import audit_measures, chains
from .checks import ident, integer
from .model import parse

MIN_CHANNEL = 1
MAX_CHANNEL = 16
MIN_K = 1
MAX_K = 64

SCORE_FRAME = 'score'
RECOVERY_FRAME = 'recovery'

OPEN = 'on'
CLOSE = 'off'


class TerminalError(Exception):
    """终端交付错误基类，``code`` 为稳定机器可读错误码。"""

    code = 'terminal_error'


class InvalidTransfer(TerminalError, ValueError):
    """开传参数非法：传送标识、代号、K、通道映射或谱面结构不合法。"""

    code = 'invalid_transfer'


class ReceiptRejected(TerminalError):
    """回执不可消费：传送/代号不匹配、包未发出或来自未来代号。"""

    code = 'receipt_rejected'


class InvalidCheckpoint(TerminalError):
    """JSON 检查点形状或不变量被破坏，无法恢复。"""

    code = 'invalid_checkpoint'


def _strict_score(score):
    """解析并严格校验谱面，返回 (measures, events)。

    沿用对账入口的严格口径：同声部重叠（overlap）、越界（overrun）或
    未兑现延音（invalid_tie）拒绝开传；留白（gap）、纯休止或空事件表
    允许，休止不产生任何物理键占用。
    """
    try:
        measures, events = parse(score)
    except ValueError as e:
        raise InvalidTransfer(f'invalid score: {e}') from e
    for diagnostic in audit_measures(measures, events):
        if diagnostic['kind'] in ('overlap', 'overrun'):
            raise InvalidTransfer(
                f"score not admitted: {diagnostic['kind']} in voice "
                f"{diagnostic['voice']} measure {diagnostic['measure']}")
    _chain_list, tie_diagnostics = chains(events)
    if tie_diagnostics:
        raise InvalidTransfer('score not admitted: unfulfilled tie')
    return measures, events


def _occupancies(events, channels):
    """合法延音合并后的每条发音链 -> 若干物理键占用 (channel, pitch, start, end)。

    同一条链的和弦音各自占用一个物理键；不同声部可经映射落到同一通道，
    同通道同音高的多个占用由帧合并逻辑按引用计数处理。
    """
    chain_list, _tie_diagnostics = chains(events)
    occupancies = []
    for chain in chain_list:
        channel = channels[chain['voice']]
        for pitch in chain['pitches']:
            occupancies.append((channel, pitch,
                                chain['start'], chain['end']))
    return occupancies


def build_frames(events, channels):
    """把发音链占用编译成非空指令帧（纯函数，供测试与状态机共用）。

    每个时刻统一处理所有开始与结束：同刻所有关操作在所有开操作之前，
    两组各自按 (通道, 音高) 升序；没有操作的时刻不产生帧。合法延音链
    中间不重触发，因为整条链是一个半开区间占用。
    """
    starts = defaultdict(list)   # 时间 -> 该时刻开始的占用
    ends = defaultdict(list)
    for channel, pitch, start, end in _occupancies(events, channels):
        starts[start].append((channel, pitch))
        ends[end].append((channel, pitch))
    active = defaultdict(int)    # (channel, pitch) -> 跨此时刻仍有效的占用数
    frames = []
    for time in sorted(set(starts) | set(ends)):
        closing = []
        for key in sorted(ends.get(time, ())):
            active[key] -= 1
            if active[key] == 0:
                del active[key]
                closing.append(key)
        opening = []
        for key in sorted(starts.get(time, ())):
            if key not in active:
                opening.append(key)
            active[key] += 1
        operations = ([{'channel': c, 'pitch': p, 'switch': CLOSE}
                       for c, p in closing]
                      + [{'channel': c, 'pitch': p, 'switch': OPEN}
                         for c, p in opening])
        if operations:
            frames.append({'time': time, 'operations': operations})
    return frames


def _keys_on_at(frames, position):
    """在奏谱位置 ``position``（最后确认整帧时间）执行完毕时应亮的物理键。

    按帧顺序模拟到该时刻：关/开同一键时先关后开，最终仍打开的键即结果。
    初始位置（None）没有任何键。
    """
    held = set()
    for frame in frames:
        if position is not None and frame['time'] > position:
            break
        for op in frame['operations']:
            key = (op['channel'], op['pitch'])
            if op['switch'] == CLOSE:
                held.discard(key)
            else:
                held.add(key)
    return held


def _check_cursor(cursor, phase, frames, k, confirmed_until):
    """核对检查点里的帧游标与按当前谱重算的结果一致。"""
    if not isinstance(cursor, dict):
        raise InvalidCheckpoint('cursor must be an object')
    if phase == SCORE_FRAME:
        expected_keys = {'frame_index', 'next_fragment', 'operations'}
    else:
        expected_keys = {'next_fragment', 'operations'}
    if set(cursor) != expected_keys:
        raise InvalidCheckpoint('cursor fields do not match its phase')
    operations = cursor['operations']
    if not isinstance(operations, list) or not operations:
        raise InvalidCheckpoint('cursor must carry a non-empty operations list')
    if any(not isinstance(op, dict) or set(op) != {'channel', 'pitch', 'switch'}
           for op in operations):
        raise InvalidCheckpoint('cursor operations are malformed')
    if any(op['switch'] not in (OPEN, CLOSE) for op in operations):
        raise InvalidCheckpoint('cursor operation switch is invalid')
    total_fragments = (len(operations) + k - 1) // k
    next_fragment = cursor['next_fragment']
    if type(next_fragment) is not int or not 1 <= next_fragment <= total_fragments:
        raise InvalidCheckpoint('cursor next_fragment is out of range')
    if phase == SCORE_FRAME:
        frame_index = cursor['frame_index']
        if type(frame_index) is not int or not 0 <= frame_index < len(frames):
            raise InvalidCheckpoint('cursor frame_index is out of range')
        expected = frames[frame_index]['operations']
        if operations != expected:
            raise InvalidCheckpoint('cursor operations do not match its frame')
    else:
        # 恢复帧操作是按确认位置规范化后的纯开键序列，重算结果必须逐字一致。
        if any(op['switch'] != OPEN for op in operations):
            raise InvalidCheckpoint('recovery cursor may only contain opens')
        position_fraction = None
        if confirmed_until >= 0:
            position_fraction = frames[confirmed_until]['time']
        expected_keys = (_keys_on_at(frames, position_fraction)
                         if frames else set())
        expected_ops = [{'channel': c, 'pitch': p, 'switch': OPEN}
                        for c, p in sorted(expected_keys)]
        if operations != expected_ops:
            raise InvalidCheckpoint(
                'recovery operations do not reopen the confirmed held keys')
    return total_fragments


class TerminalDelivery:
    """单次传送的断点续送状态机；调用方串行更新，不做并发。

    相位只有两种：``recovery``（复位后先恢复最后确认位置的物理键）与
    ``score``（按源顺序交付奏谱帧）。无键恢复是零长度相位，构建游标时
    立即切到下一奏谱帧。
    """

    def __init__(self, transfer_id, terminal_code, k, score, channels,
                 frames, confirmed_until, phase, cursor, counter,
                 outstanding):
        self._transfer_id = transfer_id
        self._terminal_code = terminal_code
        self._k = k
        self._score = score
        self._channels = channels
        self._frames = frames
        # 最后确认的整帧奏谱位置在帧数组中的下标；-1 表示尚无整帧确认。
        self._confirmed_until = confirmed_until
        self._phase = phase
        # 游标：当前帧/恢复帧的完整操作序列与下一片号；整帧完成后为 None。
        self._cursor = cursor
        # 代号内下一个可分配包序号（从 1 开始），复位后归 1。
        self._counter = counter
        # 已取出但尚未收到回执的完整包；无在途包时为 None。
        self._outstanding = outstanding

    @property
    def transfer_id(self):
        return self._transfer_id

    @property
    def terminal_code(self):
        return self._terminal_code

    @property
    def packet_limit(self):
        return self._k

    def _packet_time(self):
        if self._phase == RECOVERY_FRAME:
            position = self.confirmed_position()
            return None if position is None else position
        return str(self._frames[self._cursor['frame_index']]['time'])

    def _build_packet(self, fragment_index):
        cursor = self._cursor
        start = (fragment_index - 1) * self._k
        chunk = cursor['operations'][start:start + self._k]
        total_fragments = (len(cursor['operations']) + self._k - 1) // self._k
        return {
            'transfer_id': self._transfer_id,
            'terminal_code': self._terminal_code,
            'packet_index': self._counter,
            'frame_kind': self._phase,
            'time': self._packet_time(),
            'fragment_index': fragment_index,
            'fragment_total': total_fragments,
            'operations': copy.deepcopy(chunk),
        }

    def _start_cursor(self):
        """上一整帧已确认后，构建下一帧游标；无更多帧时返回 None。"""
        if self._phase == RECOVERY_FRAME:
            position_fraction = None
            if self._confirmed_until >= 0:
                position_fraction = self._frames[self._confirmed_until]['time']
            keys = (_keys_on_at(self._frames, position_fraction)
                    if self._frames else set())
            if keys:
                self._cursor = {
                    'next_fragment': 1,
                    'operations': [{'channel': c, 'pitch': p, 'switch': OPEN}
                                   for c, p in sorted(keys)],
                }
                return
            # 无键则跳过恢复帧，回到奏谱相位，从下一奏谱帧继续。
            self._phase = SCORE_FRAME
        # score 相位：从最后确认整帧的下一帧开始。
        frame_index = self._confirmed_until + 1
        if frame_index >= len(self._frames):
            self._cursor = None
            return
        self._cursor = {
            'frame_index': frame_index,
            'next_fragment': 1,
            'operations': copy.deepcopy(
                self._frames[frame_index]['operations']),
        }

    def next_packet(self):
        """取出下一包；无更多包时返回 ``None`` 明确结束。

        未收到回执前反复调用（含从检查点重新载入后）返回同一个完整包；
        非末片确认后才可取同帧下一片，末片确认后才推进到下一帧。
        """
        if self._outstanding is not None:
            return copy.deepcopy(self._outstanding)
        if self._cursor is None:
            return None
        fragment_index = self._cursor['next_fragment']
        total_fragments = (len(self._cursor['operations']) + self._k - 1) \
            // self._k
        packet = self._build_packet(fragment_index)
        self._outstanding = copy.deepcopy(packet)
        self._counter += 1
        return copy.deepcopy(packet)

    def receive_receipt(self, transfer_id, terminal_code=None,
                        packet_index=None):
        """应用一份回执，成功返回 ``None``，不可消费抛 :class:`ReceiptRejected`。

        回执也接受 ``{'transfer_id', 'terminal_code', 'packet_index'}``
        形状的单个字典。当前代号中已确认包的重复回执不改变状态；未发出
        的包、错误传送标识或非当前代号（含复位后的旧代号与尚未获知的
        未来代号）一律拒绝且无副作用。
        """
        if isinstance(transfer_id, dict) and terminal_code is None \
                and packet_index is None:
            receipt = transfer_id
            if set(receipt) != {'transfer_id', 'terminal_code',
                                'packet_index'}:
                raise ReceiptRejected('receipt must carry transfer_id, '
                                      'terminal_code and packet_index')
            transfer_id = receipt['transfer_id']
            terminal_code = receipt['terminal_code']
            packet_index = receipt['packet_index']
        if transfer_id != self._transfer_id:
            raise ReceiptRejected(
                f'receipt transfer {transfer_id!r} does not match open '
                f'transfer {self._transfer_id!r}')
        if type(terminal_code) is not int \
                or terminal_code != self._terminal_code:
            # 旧代号回执在复位后不起作用；未来代号同样拒绝且无副作用。
            raise ReceiptRejected(
                f'receipt terminal code {terminal_code!r} is not the current '
                f'code {self._terminal_code}')
        if type(packet_index) is not int or packet_index < 1:
            raise ReceiptRejected('packet_index must be a positive integer')
        if self._outstanding is None:
            # 没有在途包：任何包号都不可能是"已发出待确认"的包。
            raise ReceiptRejected(
                f'no packet in flight; receipt for packet {packet_index} '
                'is rejected')
        if packet_index > self._outstanding['packet_index']:
            raise ReceiptRejected(
                f'receipt for not-yet-sent packet {packet_index}')
        if packet_index < self._outstanding['packet_index']:
            # 当前代号中已确认包的重复回执：幂等无副作用。
            return
        # 回执确认的正是当前在途包。
        packet = self._outstanding
        self._outstanding = None
        cursor = self._cursor
        is_last = packet['fragment_index'] == packet['fragment_total']
        if not is_last:
            # 非末片只表示该片已暂存：允许取同帧下一片，位置不推进。
            cursor['next_fragment'] = packet['fragment_index'] + 1
            return
        # 末片回执：整帧执行完毕。
        if self._phase == SCORE_FRAME:
            self._confirmed_until = cursor['frame_index']
        else:
            # 恢复帧不推进谱面位置；恢复完成后回到奏谱相位。
            self._phase = SCORE_FRAME
        self._cursor = None
        self._start_cursor()

    def record_reset(self, terminal_code):
        """记录终端复位到更大的代号，返回 ``None``。

        终端已清空所有键及暂存片：保留最后确认的整帧奏谱位置，丢弃旧
        代号的待发包与未确认整帧的分片进度，包序号归 1。重复或更旧的
        复位通知不回退（幂等忽略）。新代号先以恢复帧重开最后确认位置
        应亮的物理键；无键则跳过恢复帧。
        """
        if type(terminal_code) is not int or terminal_code < 0:
            raise InvalidTransfer('terminal code must be a non-negative integer')
        if terminal_code <= self._terminal_code:
            return  # 重复或更旧的复位通知：不回退。
        self._terminal_code = terminal_code
        self._counter = 1
        self._outstanding = None
        self._phase = RECOVERY_FRAME
        self._cursor = None
        self._start_cursor()

    def confirmed_position(self):
        """最后确认的**整帧**奏谱位置（规范分数字符串）；尚无确认时为 ``None``。

        恢复帧与未确认完的奏谱帧都不推进谱面位置。
        """
        if self._confirmed_until < 0:
            return None
        return str(self._frames[self._confirmed_until]['time'])

    def to_json(self):
        """序列化为标准 JSON 字符串（sort_keys，拒绝非有限值）。

        检查点只保存恢复交付所必需的最新状态，不累计已送包、过去的代号
        或历次检查点。
        """
        return json.dumps(
            {'format': 'score-spans.terminal/1',
             'transfer_id': self._transfer_id,
             'terminal_code': self._terminal_code,
             'packet_limit': self._k,
             'score': self._score,
             'channels': copy.deepcopy(self._channels),
             'confirmed_until': self._confirmed_until,
             'phase': self._phase,
             'cursor': copy.deepcopy(self._cursor),
             'packet_counter': self._counter,
             'outstanding': copy.deepcopy(self._outstanding)},
            ensure_ascii=False, sort_keys=True, allow_nan=False)

    @classmethod
    def from_json(cls, payload):
        """从 :meth:`to_json` 的产物恢复；形状或不变量破坏抛 InvalidCheckpoint。"""
        try:
            state = json.loads(payload)
        except (ValueError, TypeError) as e:
            raise InvalidCheckpoint(
                f'checkpoint is not valid JSON: {e}') from e
        try:
            if not isinstance(state, dict):
                raise InvalidCheckpoint('checkpoint root must be an object')
            required = {'format', 'transfer_id', 'terminal_code',
                        'packet_limit', 'score', 'channels',
                        'confirmed_until', 'phase', 'cursor',
                        'packet_counter', 'outstanding'}
            if set(state) != required:
                raise InvalidCheckpoint('checkpoint fields are missing or unknown')
            if state['format'] != 'score-spans.terminal/1':
                raise InvalidCheckpoint(
                    f"unsupported checkpoint format: {state['format']!r}")
            transfer_id = ident(state['transfer_id'])
            terminal_code = integer(state['terminal_code'], 0, 10 ** 9 - 1)
            k = integer(state['packet_limit'], MIN_K, MAX_K)
            if not isinstance(state['channels'], dict):
                raise InvalidCheckpoint('channels must be an object')
            channels = {}
            for voice, channel in state['channels'].items():
                ident(voice)
                channels[voice] = integer(channel, MIN_CHANNEL, MAX_CHANNEL)
            measures, events = _strict_score(state['score'])
            voices = {e['voice'] for e in events if e['pitches']}
            if set(voices) - set(channels):
                raise InvalidCheckpoint(
                    'checkpoint channels miss a sounding voice of the score')
            score_voices = {e['voice'] for e in events}
            if set(channels) - score_voices:
                raise InvalidCheckpoint(
                    'checkpoint channels name a voice absent from the score')
            frames = build_frames(events, channels)
            confirmed_until = state['confirmed_until']
            if type(confirmed_until) is not int \
                    or not -1 <= confirmed_until < len(frames):
                raise InvalidCheckpoint('confirmed_until is out of range')
            phase = state['phase']
            if phase not in (SCORE_FRAME, RECOVERY_FRAME):
                raise InvalidCheckpoint('phase must be score or recovery')
            counter = state['packet_counter']
            if type(counter) is not int or counter < 1:
                raise InvalidCheckpoint(
                    'packet_counter must be a positive integer')
            cursor = state['cursor']
            if cursor is not None:
                total_fragments = _check_cursor(
                    cursor, phase, frames, k, confirmed_until)
                if cursor['next_fragment'] > total_fragments:
                    raise InvalidCheckpoint('frame has no remaining fragment')
            elif phase == RECOVERY_FRAME:
                # 无键恢复是零长度相位：载入时规范化到 score 相位。
                phase = SCORE_FRAME
            outstanding = state['outstanding']
            if outstanding is not None:
                if cursor is None:
                    raise InvalidCheckpoint(
                        'outstanding packet without an active frame')
                if set(outstanding) != {
                        'transfer_id', 'terminal_code', 'packet_index',
                        'frame_kind', 'time', 'fragment_index',
                        'fragment_total', 'operations'}:
                    raise InvalidCheckpoint('outstanding packet fields invalid')
                if outstanding['transfer_id'] != transfer_id \
                        or outstanding['terminal_code'] != terminal_code \
                        or outstanding['frame_kind'] != phase:
                    raise InvalidCheckpoint(
                        'outstanding packet does not match the checkpoint')
                if type(outstanding['packet_index']) is not int \
                        or outstanding['packet_index'] != counter - 1:
                    raise InvalidCheckpoint(
                        'outstanding packet_index must be the last issued one')
                total_fragments = (len(cursor['operations']) + k - 1) // k
                if (type(outstanding['fragment_index']) is not int
                        or outstanding['fragment_index']
                        != cursor['next_fragment']
                        or outstanding['fragment_total'] != total_fragments):
                    raise InvalidCheckpoint(
                        'outstanding packet fragment does not match the cursor')
                start = (outstanding['fragment_index'] - 1) * k
                if outstanding['operations'] != \
                        cursor['operations'][start:start + k]:
                    raise InvalidCheckpoint(
                        'outstanding packet operations do not match the frame')
                expected_time = None
                if phase == SCORE_FRAME:
                    expected_time = str(
                        frames[cursor['frame_index']]['time'])
                elif confirmed_until >= 0:
                    expected_time = str(
                        frames[confirmed_until]['time'])
                if outstanding['time'] != expected_time:
                    raise InvalidCheckpoint(
                        'outstanding packet time does not match the frame')
        except TerminalError:
            raise
        except (TypeError, ValueError, KeyError) as e:
            raise InvalidCheckpoint(f'malformed checkpoint: {e}') from e
        return cls(transfer_id, terminal_code, k,
                   copy.deepcopy(state['score']), channels, frames,
                   confirmed_until, phase, copy.deepcopy(cursor), counter,
                   copy.deepcopy(outstanding))


def open_transfer(transfer_id, terminal_code, score, channels, k):
    """开一次传送，返回 :class:`TerminalDelivery`；非法输入抛 :class:`InvalidTransfer`。

    ``transfer_id`` 走现有 ID 规则（``[A-Za-z0-9_:-]{1,64}``）；
    ``terminal_code`` 是非负整数；``k`` 是 1..64 的每包操作上限；
    ``channels`` 是声部 ID 到 1..16 通道的映射，同一通道可被多个声部
    共用。每个非休止声部必须有映射，可为仅有休止的声部给映射，不接受
    谱外声部。谱面重叠、越界或无效延音拒绝开传；留白、纯休止或空事件
    表允许，此时没有任何指令帧，首次取包即结束。
    """
    try:
        transfer_id = ident(transfer_id)
    except ValueError as e:
        raise InvalidTransfer(f'invalid transfer_id: {e}') from e
    if type(terminal_code) is not int or terminal_code < 0:
        raise InvalidTransfer('terminal code must be a non-negative integer')
    if type(k) is not int or not MIN_K <= k <= MAX_K:
        raise InvalidTransfer(
            f'packet limit K must be an integer {MIN_K}..{MAX_K}')
    if not isinstance(channels, dict):
        raise InvalidTransfer(
            'channels must be an object mapping voice to channel')
    normalized = {}
    for voice, channel in channels.items():
        try:
            voice = ident(voice)
        except ValueError as e:
            raise InvalidTransfer(f'invalid voice id: {e}') from e
        if type(channel) is not int or not MIN_CHANNEL <= channel <= MAX_CHANNEL:
            raise InvalidTransfer(
                f'channel for voice {voice!r} must be an integer '
                f'{MIN_CHANNEL}..{MAX_CHANNEL}')
        normalized[voice] = channel
    _measures, events = _strict_score(score)
    all_voices = {e['voice'] for e in events}
    sounding_voices = {e['voice'] for e in events if e['pitches']}
    missing = sounding_voices - set(normalized)
    if missing:
        raise InvalidTransfer(
            f'every sounding voice needs a channel mapping; missing: '
            f'{sorted(missing)}')
    # 谱外声部（连休止事件都不存在）不接受；仅有休止的声部给映射是允许的。
    unknown = set(normalized) - all_voices
    if unknown:
        raise InvalidTransfer(
            'channel mappings reference voices absent from the score: '
            f'{sorted(unknown)}')
    frames = build_frames(events, normalized)
    delivery = TerminalDelivery(
        transfer_id, terminal_code, k, copy.deepcopy(score), normalized,
        frames, -1, SCORE_FRAME, None, 1, None)
    delivery._start_cursor()
    return delivery
