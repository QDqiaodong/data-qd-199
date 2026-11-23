"""起音标注对账：谱面修订与人工工作单的本地对账接口。

校订方会重写同一段延音的记法，标注方拿着先前分配的工作单判断本次发音
的起音标签。本模块在不改动旧谱面处理、演奏窗、语料发布入口与 CLI 的
前提下，提供四个可直接从 Python 调用的入口：

* :func:`open_batch`：用调用方提供的唯一批号和初始谱开批（修订号 0）；
* :meth:`Reconciler.apply_revision`：应用相邻谱面修订（成功则修订号加一）；
* :meth:`Reconciler.submit`：接收人工结果（不增加谱面修订号）；
* :meth:`Reconciler.delivery`：读取当前待回传工作单与已确认标注。

状态只保存在调用方进程里，调用方串行更新并自行保存最新状态：
``reconciler.to_json()`` 产出标准 JSON 字符串，:meth:`Reconciler.from_json`
从该字符串原样恢复。检查点不保存历次谱、来源关系、已销票明细或任何
随修订/回传次数增长的集合，仅保留当前谱、当前链与固定数量的递增计数。

业务规则、沿用条件、错误类型与检查点格式见 docs/reconcile.md。
"""

import copy
import json
from fractions import Fraction

from .audit import audit_measures, chains
from .checks import ident
from .model import parse

LABELS = ('plain', 'accent', 'tenuto')
MAX_SOURCES = 4000


class ReconcileError(Exception):
    """对账错误基类，``code`` 为稳定机器可读错误码。"""

    code = 'reconcile_error'


class InvalidScore(ReconcileError, ValueError):
    """谱面结构非法，或含重叠、越界、未兑现延音（含非法来源对）。"""

    code = 'invalid_score'


class InvalidSource(ReconcileError, ValueError):
    """来源关系数组形状非法，或引用不存在、休止、重复、跨谱不匹配。"""

    code = 'invalid_source'


class StaleRevision(ReconcileError):
    """修订输入号与当前修订号不相邻（过期修订）。"""

    code = 'stale_revision'


class TicketRejected(ReconcileError):
    """回传票不可消费：票不存在、已作废、跨批或重复回传。"""

    code = 'ticket_rejected'


class InvalidLabel(ReconcileError, ValueError):
    """回传标签不是 plain/accent/tenuto 之一。"""

    code = 'invalid_label'


class InvalidCheckpoint(ReconcileError):
    """JSON 检查点形状或不变量被破坏，无法恢复。"""

    code = 'invalid_checkpoint'


def _strict_score(score):
    """解析并严格校验一份谱，返回 (measures, 按源序解析的 events)。

    旧入口允许有诊断地继续出报告；对账入口不接受重叠、越界或未兑现
    延音的谱。留白（gap）与纯休止、空事件表不阻止开批或修订。
    """
    try:
        measures, events = parse(score)
    except ValueError as e:
        raise InvalidScore(f'invalid score: {e}') from e
    for diagnostic in audit_measures(measures, events):
        if diagnostic['kind'] in ('overlap', 'overrun'):
            raise InvalidScore(
                f"score not admitted: {diagnostic['kind']} in voice "
                f"{diagnostic['voice']} measure {diagnostic['measure']}")
    _chain_list, tie_diagnostics = chains(events)
    if tie_diagnostics:
        raise InvalidScore('score not admitted: unfulfilled tie')
    return measures, events


def _parsed_events(score):
    """重新解析已保存的当前谱，得到按源序解析的事件。"""
    return parse(score)[1]


def _chain_view(events):
    """合法延音合并后的当前发音链（复用源序链定义），休止不产生工作项。"""
    chain_list, _tie_diagnostics = chains(events)
    return chain_list


def _review_equal(old_chain, new_chain):
    """复核内容比较：voice、全曲绝对 start/end、移调后完整 pitches。

    事件 ID 只用于来源追溯，不是复核内容，故不参与比较。
    """
    return (old_chain['voice'] == new_chain['voice']
            and str(old_chain['start']) == str(new_chain['start'])
            and str(old_chain['end']) == str(new_chain['end'])
            and list(old_chain['pitches']) == list(new_chain['pitches']))


def _validate_sources(sources, old_events, new_events):
    """解析来源对，返回 set((old_id, new_id))；任何非法输入整体拒绝。"""
    if not isinstance(sources, list) or not 0 <= len(sources) <= MAX_SOURCES:
        raise InvalidSource(
            f'sources must be a list of 0..{MAX_SOURCES} pairs')
    old_sounding = {e['id'] for e in old_events if e['pitches']}
    new_sounding = {e['id'] for e in new_events if e['pitches']}
    old_ids = {e['id'] for e in old_events}
    new_ids = {e['id'] for e in new_events}
    pairs = set()
    for raw in sources:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise InvalidSource('each source must be a [old_id, new_id] pair')
        left, right = raw
        for value in (left, right):
            if not isinstance(value, str):
                raise InvalidSource('source event ids must be strings')
        try:
            ident(left)
            ident(right)
        except ValueError as e:
            raise InvalidSource(f'invalid source id: {e}') from e
        if (left, right) in pairs:
            raise InvalidSource(f'duplicate source pair: [{left}, {right}]')
        pairs.add((left, right))
        if left not in old_ids:
            raise InvalidSource(
                f'left source {left!r} does not resolve in the old score')
        if right not in new_ids:
            raise InvalidSource(
                f'right source {right!r} does not resolve in the new score')
        if left not in old_sounding:
            raise InvalidSource(
                f'left source {left!r} is a rest: rest events cannot be '
                'sources')
        if right not in new_sounding:
            raise InvalidSource(
                f'right source {right!r} is a rest: rest events cannot be '
                'sources')
    return pairs


def _carry_map(old_chain_list, new_chain_list, sources):
    """按来源关系计算可沿用的新链下标 -> 旧链下标映射。

    沿用必须同时满足：旧链成员在来源里只连到一条新链、新链成员也只
    连回这一条旧链；两链成员都被来源对完整覆盖；两链复核内容完全
    相同。存在任何歧义（多对一、一对多共享、未覆盖成员、复核不同）
    都不沿用，调用方据此为新链发新票。只看声明证据，不做相似度挑选。
    """
    old_index = {eid: i for i, c in enumerate(old_chain_list)
                 for eid in c['events']}
    new_index = {eid: i for i, c in enumerate(new_chain_list)
                 for eid in c['events']}
    old_to_new = {}   # 旧链下标 -> 该链来源所连到的新链下标集合
    new_to_old = {}   # 新链下标 -> 连回的旧链下标集合
    old_covered = set()
    new_covered = set()
    for left, right in sources:
        oi, ni = old_index[left], new_index[right]
        old_to_new.setdefault(oi, set()).add(ni)
        new_to_old.setdefault(ni, set()).add(oi)
        old_covered.add((oi, left))
        new_covered.add((ni, right))
    carried = {}
    for ni, new_chain in enumerate(new_chain_list):
        old_set = new_to_old.get(ni)
        if old_set is None or len(old_set) != 1:
            continue  # 新链无来源或连到多条旧链：不能沿用
        (oi,) = old_set
        if old_to_new.get(oi) != {ni}:
            continue  # 该旧链还连到别的新链：存在歧义，不能挑较像的一条
        if any((oi, eid) not in old_covered
               for eid in old_chain_list[oi]['events']):
            continue  # 旧链有未被来源对覆盖的成员
        if any((ni, eid) not in new_covered
               for eid in new_chain['events']):
            continue  # 新链有未被来源对覆盖的成员
        if not _review_equal(old_chain_list[oi], new_chain):
            continue  # 复核内容不同：只对齐音高/时间/同名都不够
        carried[ni] = oi
    return carried


def _sort_key(entry):
    return (Fraction(entry['start']), entry['voice'], entry['events'][0])


class Reconciler:
    """单批号的对账状态机；调用方串行更新，不做并发写入。"""

    def __init__(self, batch_id, revision, next_ticket, score, entries):
        self._batch_id = batch_id
        self._revision = revision
        self._next_ticket = next_ticket
        self._score = score
        # 每条当前链一项：{events, voice, pitches, start, end, head,
        #                   open_ticket, label}；open_ticket 与 label 互斥。
        self._entries = entries

    @property
    def batch_id(self):
        return self._batch_id

    @property
    def revision(self):
        return self._revision

    def apply_revision(self, revision, score, sources):
        """应用一次相邻谱面修订，成功后修订号加一，返回 None。

        ``revision`` 必须恰为当前修订号加一，否则抛 :class:`StaleRevision`；
        谱面或来源关系非法抛 :class:`InvalidScore` / :class:`InvalidSource`。
        任何拒绝都不改写原谱、修订号、有效票与已确认标注。
        """
        if type(revision) is not int or revision != self._revision + 1:
            raise StaleRevision(
                f'revision {revision!r} must be current revision '
                f'{self._revision} + 1')
        old_events = _parsed_events(self._score)
        _measures, new_events = _strict_score(score)
        old_chain_list = _chain_view(old_events)
        new_chain_list = _chain_view(new_events)
        source_pairs = _validate_sources(sources, old_events, new_events)
        carried = _carry_map(old_chain_list, new_chain_list, source_pairs)

        entries = []
        next_ticket = self._next_ticket
        for ni, chain in enumerate(new_chain_list):
            base = {'events': list(chain['events']),
                    'voice': chain['voice'],
                    'pitches': list(chain['pitches']),
                    'start': str(chain['start']),
                    'end': str(chain['end']),
                    'head': chain['events'][0]}
            if ni in carried:
                old = self._entries[carried[ni]]
                # 满足沿用条件：保留未消费票的回传资格或已确认标签，
                # 交付行始终改用新谱的事件成员。
                if old['open_ticket'] is not None:
                    base['open_ticket'] = old['open_ticket']
                    base['label'] = None
                else:
                    base['open_ticket'] = None
                    base['label'] = old['label']
            else:
                # 不能沿用的新链产生一张新的待回传工作单；批内用过的
                # 票号永不再分配（next_ticket 单调递增）。
                base['open_ticket'] = next_ticket
                base['label'] = None
                next_ticket += 1
            entries.append(base)
        # 全部校验通过后才一次性改写状态，保证失败原子性。
        self._score = copy.deepcopy(score)
        self._entries = entries
        self._next_ticket = next_ticket
        self._revision = revision

    def submit(self, batch_id, ticket, label):
        """接收一张人工回传，成功后关闭工作单并交付标签，返回 None。

        只有当前仍待回传的本批票号可消费；同票再次回传、其他批的票或
        已作废的票抛 :class:`TicketRejected`，非法标签抛
        :class:`InvalidLabel`。失败不改变任何状态，也不推进修订号。
        """
        if not isinstance(label, str) or label not in LABELS:
            raise InvalidLabel(
                f'label must be one of {LABELS}, got {label!r}')
        if batch_id != self._batch_id:
            raise TicketRejected(
                f'ticket batch {batch_id!r} does not match open batch '
                f'{self._batch_id!r}')
        if type(ticket) is not int:
            raise TicketRejected(f'ticket must be an integer, got {ticket!r}')
        for entry in self._entries:
            if entry['open_ticket'] == ticket:
                entry['open_ticket'] = None
                entry['label'] = label
                return
        raise TicketRejected(
            f'ticket {ticket} is not an open ticket of batch '
            f'{self._batch_id!r}')

    def delivery(self):
        """读取当前交付：修订号、待回传工作单、已确认标注两份数组。"""
        pending, confirmed = [], []
        for entry in self._entries:
            row = {'voice': entry['voice'],
                   'pitches': list(entry['pitches']),
                   'start': entry['start'], 'end': entry['end'],
                   'events': list(entry['events'])}
            if entry['open_ticket'] is not None:
                pending.append({**row, 'batch_id': self._batch_id,
                                'ticket': entry['open_ticket']})
            else:
                confirmed.append({**row, 'label': entry['label']})
        pending.sort(key=_sort_key)
        confirmed.sort(key=_sort_key)
        return {'revision': self._revision,
                'pending': pending, 'confirmed': confirmed}

    def to_json(self):
        """序列化为标准 JSON 字符串（sort_keys，无历史、无已销票明细）。"""
        return json.dumps(
            {'format': 'score-spans.reconcile/1',
             'batch_id': self._batch_id,
             'revision': self._revision,
             'next_ticket': self._next_ticket,
             'score': self._score,
             'entries': copy.deepcopy(self._entries)},
            ensure_ascii=False, sort_keys=True, allow_nan=False)

    @classmethod
    def from_json(cls, payload):
        """从 :meth:`to_json` 的产物恢复；形状或不变量破坏抛 InvalidCheckpoint。"""
        try:
            state = json.loads(payload)
        except (ValueError, TypeError) as e:
            raise InvalidCheckpoint(f'checkpoint is not valid JSON: {e}') \
                from e
        try:
            if not isinstance(state, dict):
                raise InvalidCheckpoint('checkpoint root must be an object')
            for key in ('format', 'batch_id', 'revision', 'next_ticket',
                        'score', 'entries'):
                if key not in state:
                    raise InvalidCheckpoint(f'missing checkpoint field: {key}')
            if state['format'] != 'score-spans.reconcile/1':
                raise InvalidCheckpoint(
                    f"unsupported checkpoint format: {state['format']!r}")
            ident(state['batch_id'])
            if type(state['revision']) is not int or state['revision'] < 0:
                raise InvalidCheckpoint('revision must be a non-negative int')
            if (type(state['next_ticket']) is not int
                    or state['next_ticket'] < 0):
                raise InvalidCheckpoint('next_ticket must be a non-negative int')
            entries = state['entries']
            if not isinstance(entries, list):
                raise InvalidCheckpoint('entries must be a list')
            try:
                _measures, events = _strict_score(state['score'])
            except InvalidScore as e:
                raise InvalidCheckpoint(f'checkpoint score is invalid: {e}') \
                    from e
            chain_list = _chain_view(events)
            if len(entries) != len(chain_list):
                raise InvalidCheckpoint(
                    'entries do not match the current score chains')
            open_tickets = set()
            for i, (entry, chain) in enumerate(zip(entries, chain_list)):
                if not isinstance(entry, dict):
                    raise InvalidCheckpoint(f'entry {i} must be an object')
                for key in ('events', 'voice', 'pitches', 'start', 'end',
                            'head', 'open_ticket', 'label'):
                    if key not in entry:
                        raise InvalidCheckpoint(
                            f'entry {i} missing field: {key}')
                if entry['events'] != chain['events'] \
                        or entry['voice'] != chain['voice'] \
                        or entry['pitches'] != chain['pitches'] \
                        or entry['start'] != str(chain['start']) \
                        or entry['end'] != str(chain['end']) \
                        or entry['head'] != chain['events'][0]:
                    raise InvalidCheckpoint(
                        f'entry {i} does not match its current score chain')
                ticket, label = entry['open_ticket'], entry['label']
                if (ticket is None) == (label is None):
                    raise InvalidCheckpoint(
                        f'entry {i} must have exactly one of ticket/label')
                if ticket is not None:
                    if type(ticket) is not int or not 0 <= ticket \
                            < state['next_ticket']:
                        raise InvalidCheckpoint(
                            f'entry {i} has ticket outside counter range')
                    if ticket in open_tickets:
                        raise InvalidCheckpoint(
                            f'duplicate open ticket: {ticket}')
                    open_tickets.add(ticket)
                elif label not in LABELS:
                    raise InvalidCheckpoint(
                        f'entry {i} has invalid label: {label!r}')
        except ReconcileError:
            raise
        except (TypeError, ValueError, KeyError) as e:
            raise InvalidCheckpoint(f'malformed checkpoint: {e}') from e
        return cls(state['batch_id'], state['revision'],
                   state['next_ticket'], state['score'],
                   copy.deepcopy(entries))


def open_batch(batch_id, score):
    """用唯一批号和初始谱开批，初始修订号为 0，返回 :class:`Reconciler`。

    谱面结构、重叠、越界、未兑现延音检查与修订入口一致；留白、纯休止或
    空事件表不妨碍开批，休止不产生工作项。
    """
    try:
        ident(batch_id)
    except ValueError as e:
        raise ReconcileError(f'invalid batch_id: {e}') from e
    _measures, events = _strict_score(score)
    entries = []
    for ticket, chain in enumerate(_chain_view(events)):
        entries.append({'events': list(chain['events']),
                        'voice': chain['voice'],
                        'pitches': list(chain['pitches']),
                        'start': str(chain['start']),
                        'end': str(chain['end']),
                        'head': chain['events'][0],
                        'open_ticket': ticket, 'label': None})
    return Reconciler(batch_id, 0, len(entries),
                      copy.deepcopy(score), entries)
