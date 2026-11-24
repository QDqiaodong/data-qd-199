"""带踏板键盘补录：先预览声部放置，再把已预览的计划写回当前谱。

本模块在旧谱面处理、演奏窗、语料发布、对账与终端入口之外，新增两个
纯 Python、只依赖标准库的本地数据接口：

* :func:`plan_overdub`：把按键/抬键/踏板采集消息整理成可 JSON 保存的
  补录计划（不返回谱面，不修改输入谱）；
* :func:`apply_overdub`：把一份按原样保存并重载的计划应用到当前谱，
  只写入计划中的新增事件，不重新做声部选择。

语义、计划格式与错误码见 docs/overdub.md。实现不碰音频、设备、文件或
常驻状态；调用方负责保存计划、选择当前谱和最终持久化。
"""

import copy
import hashlib
import re
from fractions import Fraction

from .audit import audit_measures, chains
from .checks import ident, integer
from .model import parse

TPQ_VALUES = (48, 96, 480, 960)
MAX_BATCH = 32
MAX_MESSAGES = 256
MAX_ONSETS = 24
MAX_VOICES = 4
MAX_EVENTS = 2000
MAX_MEASURES = 200
MAX_TICK = 2 ** 53
PLAN_FORMAT = 'score-spans.overdub/1'

_CANONICAL = re.compile(r'(?:0|[1-9][0-9]*)(?:/[1-9][0-9]*)?')

_DOWN = ('key_down', 'note_on', 'down', 'press')
_UP = ('key_up', 'note_off', 'up', 'release')
_PEDAL = ('pedal', 'sustain', 'pedal_change')


class OverdubError(Exception):
    """补录错误基类，``code`` 为稳定机器可读错误码。"""

    code = 'overdub_error'


class InvalidOverdub(OverdubError, ValueError):
    """非法输入：参数、采集消息、旧谱或计划形状不合法。"""

    code = 'invalid_input'


class NoPlacement(OverdubError):
    """不存在完整的声部放置；不返回局部补录。"""

    code = 'no_placement'


class ApplyConflict(OverdubError):
    """当前谱与计划不匹配或已被占用；整份计划冲突。"""

    code = 'apply_conflict'


def _strict_score(score, error):
    """按现有严格谱面口径解析：拒绝重叠、越界和未兑现延音。

    留白、纯休止与空事件表允许。``error`` 是失败时抛出的异常类型：
    生成计划用 InvalidOverdub，应用计划用 ApplyConflict。
    """
    if not isinstance(score, dict):
        raise error('score must be an object')
    try:
        measures, events = parse(score)
    except ValueError as e:
        raise error(f'invalid score: {e}') from e
    for diagnostic in audit_measures(measures, events):
        if diagnostic['kind'] in ('overlap', 'overrun'):
            raise error(
                f"score not admitted: {diagnostic['kind']} in voice "
                f"{diagnostic['voice']} measure {diagnostic['measure']}")
    _chain_list, tie_diagnostics = chains(events)
    if tie_diagnostics:
        raise error('score not admitted: unfulfilled tie')
    return measures, events


def _fraction_string(value):
    if value.denominator == 1:
        return str(value.numerator)
    return f'{value.numerator}/{value.denominator}'


def _string_fraction(value, what, positive=False):
    """解析规范分数字符串（整数如 ``"4"``，否则约分 ``"n/d"``）。"""
    if not isinstance(value, str) or not _CANONICAL.fullmatch(value):
        raise InvalidOverdub(f'{what} must be a canonical fraction string')
    fraction = Fraction(value)
    if _fraction_string(fraction) != value:
        raise InvalidOverdub(f'{what} must be a reduced fraction string')
    if positive and fraction <= 0:
        raise InvalidOverdub(f'{what} must be positive')
    return fraction


def _pair_fraction(value, what, allow_zero=True):
    """解析源谱时间对 ``[分子, 分母]``，限定在源谱模型边界内。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise InvalidOverdub(f'{what} must be a [numerator, denominator] pair')
    try:
        numerator = integer(value[0], 0, 100000)
        denominator = integer(value[1], 1, 10000)
    except ValueError as e:
        raise InvalidOverdub(f'{what} is out of the source time model') from e
    if not allow_zero and numerator == 0:
        raise InvalidOverdub(f'{what} must be positive')
    return Fraction(numerator, denominator)


def _bounded(value, low, high, what):
    try:
        return integer(value, low, high)
    except ValueError as e:
        raise InvalidOverdub(f'{what} must be an integer {low}..{high}') from e


def _component(message, names, what, index):
    """取消息里唯一的字段别名；缺失或同时出现多个别名都报非法输入。"""
    present = [name for name in names if name in message]
    if not present:
        raise InvalidOverdub(f'message {index} is missing {what}')
    if len(present) > 1:
        raise InvalidOverdub(
            f'message {index} sets {what} more than once: {present}')
    return message[present[0]]


# ---------------------------------------------------------------------------
# 生成计划：参数与采集消息
# ---------------------------------------------------------------------------

def _batch_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_BATCH:
        raise InvalidOverdub(f'batch id must be 1..{MAX_BATCH} characters')
    try:
        ident(value)
    except ValueError as e:
        raise InvalidOverdub(f'invalid batch id: {e}') from e
    return value


def _ticks_per_quarter(value):
    if type(value) is not int or value not in TPQ_VALUES:
        raise InvalidOverdub(f'ticks_per_quarter must be one of {TPQ_VALUES}')
    return value


def _message_tick(message, index):
    if 'tick' not in message:
        raise InvalidOverdub(f'message {index} is missing a tick')
    try:
        return integer(message['tick'], 0, MAX_TICK)
    except ValueError as e:
        raise InvalidOverdub(
            f'message {index} tick must be a non-negative integer') from e


def _parse_messages(messages):
    """校验采集消息形状与配对，返回起音表、起音顺序与踏板消息。

    消息按数组顺序即 ``(tick, 数组下标)`` 顺序解释；tick 必须非降序。
    每条起音记录含 ``onset_id``、``tick``、``pitch``、``candidates``、
    ``index``（按键消息下标）与 ``release_tick`` / ``release_index``。
    """
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise InvalidOverdub(
            f'messages must be an array of 1..{MAX_MESSAGES} items')
    onsets = {}
    order = []
    pedals = []
    previous_tick = None
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or 'kind' not in message:
            raise InvalidOverdub(
                f'message {index} must be an object with a kind')
        kind = message['kind']
        tick = _message_tick(message, index)
        if previous_tick is not None and tick < previous_tick:
            raise InvalidOverdub('messages must be in non-decreasing tick order')
        previous_tick = tick
        if kind in _PEDAL:
            _check_fields(message, ('kind', 'tick'), ('on', 'down', 'state'),
                          index)
            state = _component(message, ('on', 'down', 'state'),
                               'pedal state', index)
            if type(state) is not bool:
                raise InvalidOverdub(
                    f'pedal message {index} needs a boolean state')
            pedals.append((index, tick, state))
        elif kind in _DOWN:
            _check_fields(message, ('kind', 'tick'),
                          ('onset_id', 'id', 'key', 'pitch', 'midi',
                           'candidates', 'voices'), index)
            onset_id = _onset_ident(message, index)
            if onset_id in onsets:
                raise InvalidOverdub(f'duplicate onset id: {onset_id}')
            pitch = _bounded(_component(message, ('pitch', 'midi'),
                                       'pitch', index), 0, 127,
                             f'onset {onset_id} pitch')
            raw = _component(message, ('candidates', 'voices'),
                             'candidates', index)
            candidates = _candidates(raw, onset_id)
            onsets[onset_id] = {
                'onset_id': onset_id, 'tick': tick, 'pitch': pitch,
                'candidates': candidates, 'index': index,
                'release_tick': None, 'release_index': None}
            order.append(onset_id)
        elif kind in _UP:
            _check_fields(message, ('kind', 'tick'),
                          ('onset_id', 'id', 'key'), index)
            onset_id = _onset_ident(message, index)
            record = onsets.get(onset_id)
            if record is None:
                raise InvalidOverdub(f'key up for unknown onset: {onset_id}')
            if record['release_tick'] is not None:
                raise InvalidOverdub(
                    f'onset {onset_id} is released more than once')
            if tick <= record['tick']:
                raise InvalidOverdub(
                    f'onset {onset_id} must be released later than its down')
            record['release_tick'] = tick
            record['release_index'] = index
        else:
            raise InvalidOverdub(f'message {index} has unknown kind: {kind!r}')
    if not 1 <= len(onsets) <= MAX_ONSETS:
        raise InvalidOverdub(f'batch needs 1..{MAX_ONSETS} onsets')
    for onset_id in order:
        if onsets[onset_id]['release_tick'] is None:
            raise InvalidOverdub(f'onset {onset_id} is never released')
    if len({voice for onset_id in order
            for voice in onsets[onset_id]['candidates']}) > MAX_VOICES:
        raise InvalidOverdub(
            f'at most {MAX_VOICES} distinct candidate voices per batch')
    return onsets, order, pedals


def _check_fields(message, required, allowed, index):
    for key in required:
        if key not in message:
            raise InvalidOverdub(f'message {index} is missing {key}')
    extra = set(message) - set(required) - set(allowed)
    if extra:
        raise InvalidOverdub(
            f'message {index} has unexpected fields: {sorted(extra)}')


def _onset_ident(message, index):
    value = _component(message, ('onset_id', 'id', 'key'), 'onset id', index)
    try:
        return ident(value)
    except ValueError as e:
        raise InvalidOverdub(
            f'message {index} has an invalid onset id: {e}') from e


def _candidates(raw, onset_id):
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_VOICES:
        raise InvalidOverdub(
            f'onset {onset_id} needs 1..{MAX_VOICES} candidates')
    candidates = []
    for value in raw:
        try:
            candidates.append(ident(value))
        except ValueError as e:
            raise InvalidOverdub(
                f'onset {onset_id} has an invalid candidate: {e}') from e
    if len(set(candidates)) != len(candidates):
        raise InvalidOverdub(f'onset {onset_id} repeats a candidate voice')
    return candidates


def _pedal_state_before(pedals, index):
    """按键/抬键消息下标 ``index`` 之前（数组顺序）的踏板状态。"""
    state = False
    for pedal_index, _tick, value in pedals:
        if pedal_index < index:
            state = value
        else:
            break
    return state


def _sounding_end(record, pedals):
    """抬键后的实际结束 tick；踏板踩下时延长到下一次抬起踏板。

    抬键消息之前（含同 tick 但更早的数组位）踏板为踩下时，找抬键之后
    第一条抬起踏板的消息。找不到即整批拒绝——不把缺失的结束时间补成
    谱尾。抬踏板不结束仍按住的键。
    """
    if not _pedal_state_before(pedals, record['release_index']):
        return record['release_tick']
    for pedal_index, tick, value in pedals:
        if pedal_index > record['release_index'] and not value:
            return tick
    raise InvalidOverdub(
        f'onset {record["onset_id"]} is still waiting for pedal release')


def _resolve_onsets(onsets, order, pedals, tpq, origin, limit):
    """计算每次起音按谱面绝对坐标的发音区间，按 (tick, 起音 ID) 排序。"""
    result = []
    for onset_id in order:
        record = onsets[onset_id]
        if record['tick'] > limit:
            raise InvalidOverdub(
                f'onset {onset_id} tick is outside the score span')
        if record['release_tick'] > limit:
            raise InvalidOverdub(
                f'onset {onset_id} release is outside the score span')
        end_tick = _sounding_end(record, pedals)
        if end_tick > limit:
            raise InvalidOverdub(
                f'onset {onset_id} sounding interval leaves the score span')
        start = origin + Fraction(record['tick'], tpq)
        end = origin + Fraction(end_tick, tpq)
        if end <= start:
            raise InvalidOverdub(
                f'onset {onset_id} has no positive sounding interval')
        result.append({**record, 'end_tick': end_tick,
                       'start': start, 'end': end})
    result.sort(key=lambda row: (row['tick'], row['onset_id']))
    return result


# ---------------------------------------------------------------------------
# 声部放置
# ---------------------------------------------------------------------------

def _occupancy(events, voices):
    """按声部记录原有事件（含休止）的绝对半开区间。"""
    occupied = {voice: [] for voice in voices}
    for event in events:
        if event['voice'] not in occupied:
            continue
        start = event['absolute']
        occupied[event['voice']].append((start, start + event['duration']))
    for slots in occupied.values():
        slots.sort()
    return occupied


def _free(start, end, slots):
    for other_start, other_end in slots:
        if other_start < end and start < other_end:
            return False
    return True


def _place(onsets, occupied):
    """在全部候选的笛卡尔积中找字典序最小的完整放置。

    按 ``(按键 tick, 起音 ID)`` 顺序逐条选声部；每条从按键到实际结束的
    整个半开区间都必须在所选声部，且与同声部原有事件、已选新起音都不
    相交（端点相接可以）。候选按 ID 升序尝试，首个完整解即字典序最小
    的声部 ID 序列。无完整解抛 :class:`NoPlacement`。

    起音已按起点排序，因此同声部是否需要回避只需比较「该声部已选区间的
    最大终点」与当前起点：更早的起点不会与当前区间相交。以
    ``(下标, 各声部最大终点)`` 作记忆化状态，精确求解而不做指数回溯。
    """
    voices = sorted(occupied)
    starts = [onset['start'] for onset in onsets]
    ends = [onset['end'] for onset in onsets]
    choices = []
    for onset in onsets:
        options = []
        for voice in sorted(set(onset['candidates'])):
            if _free(onset['start'], onset['end'], occupied[voice]):
                options.append(voice)
        choices.append(options)

    voice_index = {voice: index for index, voice in enumerate(voices)}
    # 共享记忆：状态为 (起音下标, 各声部最大终点)。最大终点 ≤ 当前起点
    # 的声部一律记为 None，因为后续起点非降、再不会与之相交。
    cache = {}

    def canonical(index, maxends):
        start = starts[index]
        return tuple(None if value is None or value <= start else value
                     for value in maxends)

    def feasible(index, maxends):
        if index == len(onsets):
            return True
        key = (index, canonical(index, maxends))
        cached = cache.get(key)
        if cached is not None:
            return cached
        start = starts[index]
        for voice in choices[index]:
            slot = voice_index[voice]
            current = maxends[slot]
            if current is not None and current > start:
                continue
            advanced = list(maxends)
            advanced[slot] = ends[index]
            if feasible(index + 1, tuple(advanced)):
                cache[key] = True
                return True
        cache[key] = False
        return False

    chosen = []
    maxends = (None,) * len(voices)
    for index in range(len(onsets)):
        start = starts[index]
        picked = False
        for voice in choices[index]:
            slot = voice_index[voice]
            current = maxends[slot]
            if current is not None and current > start:
                continue
            advanced = list(maxends)
            advanced[slot] = ends[index]
            if feasible(index + 1, tuple(advanced)):
                chosen.append(voice)
                maxends = tuple(advanced)
                picked = True
                break
        if not picked:
            raise NoPlacement('no complete placement for the recorded batch')
    return chosen


# ---------------------------------------------------------------------------
# 切分与源谱事件
# ---------------------------------------------------------------------------

def _measure_at(measures, absolute):
    """包含绝对位置的小节；小节边界归后一小节。"""
    for measure in measures:
        if measure['offset'] <= absolute < measure['offset'] + measure['length']:
            return measure
    raise InvalidOverdub('position outside the score measures')


def _split(start, end, measures):
    """按小节边界切分为 ``[(measure, local_start, duration)]``。"""
    pieces = []
    cursor = start
    while cursor < end:
        measure = _measure_at(measures, cursor)
        boundary = measure['offset'] + measure['length']
        piece_end = min(end, boundary)
        pieces.append((measure, cursor - measure['offset'],
                       piece_end - cursor))
        cursor = piece_end
    return pieces


def _fragment_id(batch_id, onset_id, piece_index, salt=0):
    """稳定的新事件 ID：由批号、起音 ID、片段序号与冲突盐值决定。

    盐值只在基础 ID 与旧谱或同批其它事件相同时递增，避免因偶然同名整批
    失败；对同样的旧谱 ID 集合与候选集合，结果仍然可复现。
    """
    material = f'{batch_id}\x00{onset_id}\x00{piece_index}'
    if salt:
        material += f'\x00{salt}'
    digest = hashlib.sha256(material.encode('utf-8')).hexdigest()
    return f'ob_{digest[:24]}'


def _allocate_id(existing, used, batch_id, onset_id, piece_index):
    salt = 0
    while True:
        candidate = _fragment_id(batch_id, onset_id, piece_index, salt)
        if candidate not in existing and candidate not in used:
            return candidate
        salt += 1
        if salt > 1024:
            raise InvalidOverdub(
                f'cannot allocate a non-colliding id for onset {onset_id}')


def _source_pitch(onset, transpose):
    pitch = onset['pitch'] - transpose
    if pitch < 0 or pitch > 127:
        raise InvalidOverdub(
            f'onset {onset["onset_id"]} pitch {onset["pitch"]} cannot be '
            f'written with transpose {transpose}')
    return pitch


def _build_rows(onsets, chosen, batch_id, measures, transpose, existing):
    """把每条起音切成跨小节片段，返回按发音顺序排列的新事件行。"""
    rows = []
    used = set()
    for onset, voice in zip(onsets, chosen):
        pieces = _split(onset['start'], onset['end'], measures)
        pitch = _source_pitch(onset, transpose)
        ids = []
        for index in range(len(pieces)):
            event_id = _allocate_id(existing, used, batch_id,
                                    onset['onset_id'], index)
            ids.append(event_id)
        used.update(ids)
        for index, (measure, local_start, duration) in enumerate(pieces):
            rows.append({
                'id': ids[index], 'onset_id': onset['onset_id'],
                'voice': voice, 'measure': measure['id'],
                'start': local_start, 'duration': duration,
                'pitches': [pitch],
                'tie_to': ids[index + 1] if index + 1 < len(ids) else None})
    return rows


def _source_event(row):
    """新事件行 -> 源谱 event（只含源谱模型字段）。"""
    record = {'id': row['id'], 'voice': row['voice'],
              'measure': row['measure'],
              'start': [row['start'].numerator, row['start'].denominator],
              'duration': [row['duration'].numerator,
                           row['duration'].denominator],
              'pitches': list(row['pitches'])}
    if row['tie_to'] is not None:
        record['tie_to'] = row['tie_to']
    return record


def _plan_event(row):
    """新事件行 -> 计划里的 new_events 行（附起音归属）。"""
    record = _source_event(row)
    record['onset_id'] = row['onset_id']
    return record


def _merged_score(score, rows):
    """构造写入后的源谱（用于可表示性与严格审核检查）。"""
    data = copy.deepcopy(score)
    data['events'] = list(data['events']) + [_source_event(row)
                                             for row in rows]
    return data


# ---------------------------------------------------------------------------
# 计划生成
# ---------------------------------------------------------------------------

def plan_overdub(score, batch_id, start_measure, ticks_per_quarter, messages):
    """生成补录计划，返回可 JSON 序列化的字典，不修改 ``score``。

    ``tick 0`` 是 ``start_measure`` 的开头；每四分音符
    ``ticks_per_quarter`` 个 tick。所有起音必须能完整放入某个候选声部，
    否则抛 :class:`NoPlacement`，不返回局部补录；非法输入抛
    :class:`InvalidOverdub`。

    计划包含每次起音的绝对发音区间（规范分数字符串）、所选声部、按发音
    顺序的新事件 ID，以及把新增事件写回源谱所需的全部信息（不含整份
    旧谱）。计划经标准 JSON 保存和载入后可交给 :func:`apply_overdub`。
    """
    batch_id = _batch_id(batch_id)
    ticks_per_quarter = _ticks_per_quarter(ticks_per_quarter)
    try:
        start_id = ident(start_measure)
    except ValueError as e:
        raise InvalidOverdub(f'invalid start measure: {e}') from e
    measures, events = _strict_score(score, InvalidOverdub)
    by_id = {measure['id']: measure for measure in measures}
    if start_id not in by_id:
        raise InvalidOverdub(f'unknown start measure: {start_id}')
    origin = by_id[start_id]['offset']
    tail = measures[-1]['offset'] + measures[-1]['length']
    limit = (tail - origin) * ticks_per_quarter
    transpose = _bounded(score.get('transpose'), -127, 127, 'transpose')

    onsets, order, pedals = _parse_messages(messages)
    for _index, tick, _state in pedals:
        if tick > limit:
            raise InvalidOverdub('pedal tick is outside the score span')
    resolved = _resolve_onsets(onsets, order, pedals, ticks_per_quarter,
                               origin, limit)

    voices = sorted({voice for onset in resolved
                     for voice in onset['candidates']})
    occupied = _occupancy(events, voices)
    chosen = _place(resolved, occupied)
    existing = {event['id'] for event in events}
    rows = _build_rows(resolved, chosen, batch_id, measures, transpose,
                       existing)
    if len(events) + len(rows) > MAX_EVENTS:
        raise InvalidOverdub(
            f'event count would exceed the {MAX_EVENTS} event model limit')
    _strict_score(_merged_score(score, rows), InvalidOverdub)

    index_of = {measure['id']: index for index, measure in enumerate(measures)}
    last_index = max(index_of[row['measure']] for row in rows)
    prefix = [{'id': measure['id'],
               'length': _fraction_string(measure['length'])}
              for measure in measures[:last_index + 1]]
    preview = []
    cursor = 0
    for onset, voice in zip(resolved, chosen):
        count = len(_split(onset['start'], onset['end'], measures))
        chunk = rows[cursor:cursor + count]
        cursor += count
        preview.append({'onset_id': onset['onset_id'], 'voice': voice,
                        'pitch': onset['pitch'],
                        'start': _fraction_string(onset['start']),
                        'end': _fraction_string(onset['end']),
                        'events': [row['id'] for row in chunk]})
    return {'format': PLAN_FORMAT,
            'batch_id': batch_id,
            'start_measure': start_id,
            'ticks_per_quarter': ticks_per_quarter,
            'transpose': transpose,
            'prefix': prefix,
            'onsets': preview,
            'new_events': [_plan_event(row) for row in rows]}


# ---------------------------------------------------------------------------
# 应用计划
# ---------------------------------------------------------------------------

def _validate_plan(plan):
    """校验计划形状与内部一致性，返回规范化后的计划。"""
    if not isinstance(plan, dict):
        raise InvalidOverdub('plan must be an object')
    required = ('format', 'batch_id', 'start_measure', 'ticks_per_quarter',
                'transpose', 'prefix', 'onsets', 'new_events')
    for key in required:
        if key not in plan:
            raise InvalidOverdub(f'plan is missing field: {key}')
    if set(plan) - set(required):
        raise InvalidOverdub(
            f'plan has unexpected fields: {sorted(set(plan) - set(required))}')
    if plan['format'] != PLAN_FORMAT:
        raise InvalidOverdub(f'unsupported plan format: {plan["format"]!r}')
    batch_id = _batch_id(plan['batch_id'])
    ticks_per_quarter = _ticks_per_quarter(plan['ticks_per_quarter'])
    try:
        start_id = ident(plan['start_measure'])
    except ValueError as e:
        raise InvalidOverdub(f'invalid start measure: {e}') from e
    transpose = _bounded(plan['transpose'], -127, 127, 'plan transpose')

    prefix_raw = plan['prefix']
    if not isinstance(prefix_raw, list) or not 1 <= len(prefix_raw) \
            <= MAX_MEASURES:
        raise InvalidOverdub(
            f'prefix must be an array of 1..{MAX_MEASURES} measures')
    prefix = []
    for index, row in enumerate(prefix_raw):
        if not isinstance(row, dict) or set(row) != {'id', 'length'}:
            raise InvalidOverdub(f'prefix row {index} must have id and length')
        try:
            measure_id = ident(row['id'])
        except ValueError as e:
            raise InvalidOverdub(f'invalid prefix row {index}: {e}') from e
        length = _string_fraction(row['length'],
                                  f'prefix row {index} length', positive=True)
        prefix.append({'id': measure_id, 'length': length})
    prefix_ids = [row['id'] for row in prefix]
    if len(set(prefix_ids)) != len(prefix_ids):
        raise InvalidOverdub('prefix repeats a measure id')
    if start_id not in prefix_ids:
        raise InvalidOverdub('plan start measure is outside its prefix')

    events_raw = plan['new_events']
    if not isinstance(events_raw, list) or not 1 <= len(events_raw) \
            <= MAX_EVENTS:
        raise InvalidOverdub(
            f'new_events must be an array of 1..{MAX_EVENTS} items')
    new_events = {}
    order = []
    for index, row in enumerate(events_raw):
        if not isinstance(row, dict):
            raise InvalidOverdub(f'new event {index} must be an object')
        allowed = {'id', 'onset_id', 'voice', 'measure', 'start',
                   'duration', 'pitches', 'tie_to'}
        if set(row) - allowed or not {'id', 'onset_id', 'voice', 'measure',
                                      'start', 'duration',
                                      'pitches'} <= set(row):
            raise InvalidOverdub(f'invalid new event {index} fields')
        try:
            event_id = ident(row['id'])
            onset_id = ident(row['onset_id'])
            voice = ident(row['voice'])
            measure_id = ident(row['measure'])
        except ValueError as e:
            raise InvalidOverdub(f'invalid new event {index}: {e}') from e
        if event_id in new_events:
            raise InvalidOverdub(f'duplicate new event id: {event_id}')
        start = _pair_fraction(row['start'], f'new event {event_id} start')
        duration = _pair_fraction(row['duration'],
                                  f'new event {event_id} duration',
                                  allow_zero=False)
        raw_pitches = row['pitches']
        if not isinstance(raw_pitches, list) or not 1 <= len(raw_pitches) <= 16:
            raise InvalidOverdub(f'new event {event_id} needs 1..16 pitches')
        pitches = [_bounded(value, 0, 127, f'new event {event_id} pitch')
                   for value in raw_pitches]
        if pitches != sorted(set(pitches)):
            raise InvalidOverdub(
                f'new event {event_id} pitches must be sorted and unique')
        tie_to = row.get('tie_to')
        if tie_to is not None:
            try:
                tie_to = ident(tie_to)
            except ValueError as e:
                raise InvalidOverdub(
                    f'new event {event_id} has an invalid tie target: {e}') \
                    from e
        new_events[event_id] = {
            'id': event_id, 'onset_id': onset_id, 'voice': voice,
            'measure': measure_id, 'start': start, 'duration': duration,
            'pitches': pitches, 'tie_to': tie_to}
        order.append(event_id)

    onsets_raw = plan['onsets']
    if not isinstance(onsets_raw, list) or not 1 <= len(onsets_raw) \
            <= MAX_ONSETS:
        raise InvalidOverdub(f'onsets must be an array of 1..{MAX_ONSETS}')
    offsets = {}
    cursor = Fraction(0)
    for row in prefix:
        offsets[row['id']] = cursor
        cursor += row['length']
    length_of = {row['id']: row['length'] for row in prefix}

    onsets = []
    assigned = set()
    for index, row in enumerate(onsets_raw):
        if not isinstance(row, dict) or set(row) != {'onset_id', 'voice',
                                                     'pitch', 'start', 'end',
                                                     'events'}:
            raise InvalidOverdub(f'invalid onset {index} fields')
        try:
            onset_id = ident(row['onset_id'])
            voice = ident(row['voice'])
        except ValueError as e:
            raise InvalidOverdub(f'invalid onset {index}: {e}') from e
        if any(prior['onset_id'] == onset_id for prior in onsets):
            raise InvalidOverdub(f'duplicate onset id: {onset_id}')
        pitch = _bounded(row['pitch'], 0, 127, f'onset {onset_id} pitch')
        start = _string_fraction(row['start'], f'onset {onset_id} start')
        end = _string_fraction(row['end'], f'onset {onset_id} end',
                               positive=True)
        if end <= start:
            raise InvalidOverdub(
                f'onset {onset_id} has a non-positive interval')
        members = row['events']
        if not isinstance(members, list) or not members:
            raise InvalidOverdub(
                f'onset {onset_id} needs a non-empty event list')
        expected_pitch = pitch - transpose
        if not 0 <= expected_pitch <= 127:
            raise InvalidOverdub(f'onset {onset_id} pitch is unrepresentable')
        previous_end = None
        last_measure = None
        for position, event_id in enumerate(members):
            if not isinstance(event_id, str) or event_id not in new_events:
                raise InvalidOverdub(
                    f'onset {onset_id} references an unknown event')
            if event_id in assigned:
                raise InvalidOverdub(
                    f'new event {event_id} belongs to more than one onset')
            assigned.add(event_id)
            event = new_events[event_id]
            if event['onset_id'] != onset_id or event['voice'] != voice:
                raise InvalidOverdub(
                    f'new event {event_id} disagrees with its onset')
            if event['pitches'] != [expected_pitch]:
                raise InvalidOverdub(
                    f'new event {event_id} pitch does not match the plan')
            if event['measure'] not in offsets:
                raise InvalidOverdub(
                    f'new event {event_id} is outside the plan prefix')
            if event['start'] + event['duration'] > length_of[event['measure']]:
                raise InvalidOverdub(
                    f'new event {event_id} overruns its measure')
            absolute = offsets[event['measure']] + event['start']
            if previous_end is None:
                if absolute != start:
                    raise InvalidOverdub(
                        f'onset {onset_id} start disagrees with its first '
                        'new event')
            elif absolute != previous_end:
                raise InvalidOverdub(
                    f'onset {onset_id} has a gap between new events')
            previous_end = absolute + event['duration']
            expected_tie = (members[position + 1]
                            if position + 1 < len(members) else None)
            if event['tie_to'] != expected_tie:
                raise InvalidOverdub(
                    f'new event {event_id} has an inconsistent tie target')
            last_measure = event['measure']
        if previous_end != end:
            raise InvalidOverdub(
                f'onset {onset_id} end disagrees with its last new event')
        onsets.append({'onset_id': onset_id, 'voice': voice, 'pitch': pitch,
                       'start': start, 'end': end, 'events': list(members),
                       'last_measure': last_measure})
    if assigned != set(new_events):
        raise InvalidOverdub('plan has unassigned new events')
    if max(prefix_ids.index(onset['last_measure'])
           for onset in onsets) != len(prefix_ids) - 1:
        raise InvalidOverdub(
            'plan prefix must end at the last new fragment measure')
    # 计划内选定声部的新音不得互相重叠（与旧谱同口径，防伪造计划）。
    by_voice = {}
    for onset in onsets:
        by_voice.setdefault(onset['voice'], []).append(
            (onset['start'], onset['end']))
    for slots in by_voice.values():
        slots.sort()
        for left, right in zip(slots, slots[1:]):
            if right[0] < left[1]:
                raise InvalidOverdub(
                    'plan onsets overlap in a selected voice')
    return {'batch_id': batch_id, 'start_measure': start_id,
            'ticks_per_quarter': ticks_per_quarter, 'transpose': transpose,
            'prefix': prefix, 'onsets': onsets, 'new_events': new_events,
            'order': order}


def apply_overdub(score, plan):
    """把一份已预览的计划应用到当前谱，返回新谱，不修改传入对象。

    只写入计划中的新增事件，不重新跑声部选择。当前谱必须仍符合严格
    谱面口径，且移调、从首小节到最后一个新增片段所在小节的有序小节
    ID 与精确长度必须与预览时一致；该前缀内原事件可以变，但不能占用
    计划选定声部中的新增区间、不能撞新 ID，且应用后仍满足源谱上限与
    严格审核。任何冲突都抛 :class:`ApplyConflict`，整份计划不落谱；
    计划自身形状非法抛 :class:`InvalidOverdub`。
    """
    normalized = _validate_plan(plan)
    measures, events = _strict_score(score, ApplyConflict)
    transpose = _bounded(score.get('transpose'), -127, 127, 'transpose')
    if transpose != normalized['transpose']:
        raise ApplyConflict(
            f'transpose changed since preview: {transpose} != '
            f'{normalized["transpose"]}')

    prefix = normalized['prefix']
    if len(measures) < len(prefix):
        raise ApplyConflict('current score is shorter than the plan prefix')
    for measure, expected in zip(measures, prefix):
        if measure['id'] != expected['id'] \
                or measure['length'] != expected['length']:
            raise ApplyConflict(
                f'measure prefix changed at {expected["id"]}')

    existing = {event['id'] for event in events}
    clashes = existing & set(normalized['new_events'])
    if clashes:
        raise ApplyConflict(
            f'new event id already exists: {sorted(clashes)[0]}')

    occupied = _occupancy(events, sorted({onset['voice']
                                         for onset in normalized['onsets']}))
    for onset in normalized['onsets']:
        if not _free(onset['start'], onset['end'],
                     occupied[onset['voice']]):
            raise ApplyConflict(
                f'planned interval of onset {onset["onset_id"]} is occupied '
                f'in voice {onset["voice"]}')

    rows = [normalized['new_events'][event_id]
            for event_id in normalized['order']]
    if len(events) + len(rows) > MAX_EVENTS:
        raise ApplyConflict(
            f'event count would exceed the {MAX_EVENTS} event model limit')
    data = copy.deepcopy(score)
    appended = []
    for row in rows:
        record = {'id': row['id'], 'voice': row['voice'],
                  'measure': row['measure'],
                  'start': [row['start'].numerator, row['start'].denominator],
                  'duration': [row['duration'].numerator,
                               row['duration'].denominator],
                  'pitches': list(row['pitches'])}
        if row['tie_to'] is not None:
            record['tie_to'] = row['tie_to']
        appended.append(record)
    data['events'] = list(data['events']) + appended
    _strict_score(data, ApplyConflict)
    return data
