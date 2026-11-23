"""演奏顺序窗口查询。

时间轴有两条，互不混用：

* ``service.process`` 使用源谱时间轴：小节按 ``measures`` 数组顺序排列，
  事件的绝对时间来自其在源谱中的小节偏移。
* :func:`process_window` 使用演奏时间轴：按 ``sections`` 反复编排后的实际
  出现顺序，每次出现拥有从 0 开始的 ``ordinal`` 与按实际经过小节长度累加的
  全曲起点；事件身份是 ``[ordinal, event_id]``。

编排与延音链均以压缩形式表示，重复轮次不会被逐次展开。
"""

from bisect import bisect_right
from fractions import Fraction

from .checks import fields, ident, integer
from .model import parse

MAX_SECTIONS = 100
MAX_REPEAT = 1_000_000_000
MAX_WINDOW = 8

# 函数图记录字段（固定 8 元组）：
#   term       终止槽位，>=0 表示本节点最终走到空边
#   steps      到终止槽位的边数
#   total_w    到终止槽位累计的轮次回绕边数
#   entry      环入口槽位（term<0 时有效，环上节点为自身）
#   cyc_len    环长（边数）
#   cyc_w      每圈回绕边数
#   pre_steps  到环入口的前缀边数
#   pre_w      前缀上的回绕边数
TERM, STEPS, TOTAL_W, ENTRY, CYC_LEN, CYC_W, PRE_STEPS, PRE_W = range(8)


def _fstr(value):
    return str(value)


def _build_score(score):
    measures, events = parse(score)
    length = {m['id']: m['length'] for m in measures}
    by_id = {e['id']: e for e in events}
    in_measure = {}
    for e in events:
        in_measure.setdefault(e['measure'], []).append(e)
    for items in in_measure.values():
        items.sort(key=lambda e: (e['start'], e['id']))
    eligible = {}
    edge = {}
    for e in events:
        eligible[e['id']] = bool(e['pitches']) and \
            e['start'] + e['duration'] <= length[e['measure']]
    for e in events:
        if e['tie_to'] is None:
            edge[e['id']] = ('none', None, False)
            continue
        t = by_id[e['tie_to']]
        end = e['start'] + e['duration']
        ml = length[e['measure']]
        # 休止或越界来源一律断链（end>ml 不具备发音/延音资格）
        if not eligible[e['id']] or end > ml:
            edge[e['id']] = ('bad', t['id'], False)
        elif end < ml:
            ok = (t['measure'] == e['measure'] and eligible[t['id']]
                  and e['voice'] == t['voice']
                  and e['pitches'] == t['pitches'] and t['start'] == end)
            edge[e['id']] = ('intra', t['id'], ok)
        else:  # end == ml：只看紧接着的下一次小节出现
            ok = (eligible[t['id']] and e['voice'] == t['voice']
                  and e['pitches'] == t['pitches'] and t['start'] == 0)
            edge[e['id']] = ('cross', t['id'], ok)
    # 目标事件 -> 来源事件 id；谱面校验已保证每个目标至多一条入边
    incoming = {e['tie_to']: e['id'] for e in events
                if e['tie_to'] is not None}
    return (measures, events, length, by_id, in_measure, eligible, edge,
            incoming)


def _build_sections(measures, sections, length):
    if not isinstance(sections, list) or not 1 <= len(sections) <= MAX_SECTIONS:
        raise ValueError('sections array size 1..%d required' % MAX_SECTIONS)
    index = {m['id']: i for i, m in enumerate(measures)}
    built = []
    ord_base = 0
    time_base = Fraction(0)
    ord_bases = []
    for raw in sections:
        fields(raw, ('from_measure', 'through_measure', 'repeat'))
        first_id = ident(raw['from_measure'])
        through_id = ident(raw['through_measure'])
        repeat = integer(raw['repeat'], 1, MAX_REPEAT)
        if first_id not in index or through_id not in index:
            raise ValueError('section references unknown measure')
        fi, ti = index[first_id], index[through_id]
        if fi > ti:
            raise ValueError('inverted section range')
        seq = [measures[i]['id'] for i in range(fi, ti + 1)]
        prefix = [Fraction(0)]
        for mid in seq:
            prefix.append(prefix[-1] + length[mid])
        built.append({
            'seq': seq, 'L': len(seq), 'repeat': repeat,
            'ord_base': ord_base, 'time_base': time_base,
            'prefix': prefix, 'cycle_len': prefix[-1],
        })
        ord_bases.append(ord_base)
        ord_base += repeat * len(seq)
        time_base += repeat * prefix[-1]
    return built, ord_bases, ord_base, time_base


def _locate(built, ord_bases, ordinal):
    """ordinal -> (段下标, 轮次, 轮内位置, 源小节 id, 全曲起点, 小节长度)。"""
    si = bisect_right(ord_bases, ordinal) - 1
    section = built[si]
    rnd, pos = divmod(ordinal - section['ord_base'], section['L'])
    mid = section['seq'][pos]
    start = (section['time_base'] + rnd * section['cycle_len']
             + section['prefix'][pos])
    ml = section['prefix'][pos + 1] - section['prefix'][pos]
    return si, rnd, pos, mid, start, ml


def _functional_records(succ):
    """对出度至多为 1 的槽位图做函数图分析，结果为固定 8 元组列表。"""
    n = len(succ)
    state = [0] * n          # 0 未访问，1 在当前路径，2 已完成
    rec = [None] * n
    for start in range(n):
        if state[start]:
            continue
        path = []
        cur = start
        while cur != -1 and state[cur] == 0:
            state[cur] = 1
            path.append(cur)
            edge = succ[cur]
            cur = edge[0] if edge is not None else -1
        if cur == -1:
            # 整条路径终止于空边：path[-1] 是终止槽位，从倒数第二个节点
            # 反向累计边数与回绕权重。
            steps = 0
            total_w = 0
            rec[path[-1]] = (path[-1], 0, 0, -1, 0, 0, 0, 0)
            for i in range(len(path) - 2, -1, -1):
                node = path[i]
                steps += 1
                total_w += succ[node][1]
                rec[node] = (path[-1], steps, total_w, -1, 0, 0, 0, 0)
        elif state[cur] == 1:
            # 发现新环
            mark = path.index(cur)
            cycle = path[mark:]
            cyc_len = len(cycle)
            cyc_w = sum(succ[node][1] for node in cycle)
            for node in cycle:
                rec[node] = (-1, 0, 0, node, cyc_len, cyc_w, 0, 0)
            pre_steps = 0
            pre_w = 0
            for i in range(mark - 1, -1, -1):
                node = path[i]
                pre_steps += 1
                pre_w += succ[node][1]
                rec[node] = (-1, 0, 0, cur, cyc_len, cyc_w,
                             pre_steps, pre_w)
        else:
            # 汇入已完成的组件：path[-1] 仍有一条边指向 cur
            base = rec[cur]
            run_st = 1
            run_w = succ[path[-1]][1]
            for i in range(len(path) - 2, -1, -1):
                node = path[i]
                if base[TERM] >= 0:
                    rec[node] = (base[TERM], base[STEPS] + run_st,
                                 base[TOTAL_W] + run_w, -1, 0, 0, 0, 0)
                else:
                    rec[node] = (-1, 0, 0, base[ENTRY], base[CYC_LEN],
                                 base[CYC_W], base[PRE_STEPS] + run_st,
                                 base[PRE_W] + run_w)
                run_st += 1
                run_w += succ[node][1]
            if base[TERM] >= 0:
                rec[path[-1]] = (base[TERM], base[STEPS] + 1,
                                 base[TOTAL_W] + succ[path[-1]][1],
                                 -1, 0, 0, 0, 0)
            else:
                rec[path[-1]] = (-1, 0, 0, base[ENTRY], base[CYC_LEN],
                                 base[CYC_W], base[PRE_STEPS] + 1,
                                 base[PRE_W] + succ[path[-1]][1])
        for node in path:
            state[node] = 2
    return rec


def _build_table(section, in_measure, eligible, edge, by_id, incoming):
    """为一个编排段构建单轮槽位函数图（回绕边带权重 1）。

    跨小节边若无法在段内兑现，记录为段出口/入口待决边
    （``out_pending``/``in_pending``），由段间走链按实际相邻段判定。
    """
    seq = section['seq']
    size = section['L']
    slots = []
    node_of = {}
    for pos, mid in enumerate(seq):
        for ev in in_measure.get(mid, ()):
            if eligible[ev['id']]:
                node_of[(pos, ev['id'])] = len(slots)
                slots.append((pos, ev['id']))
    event_slots = {eid for _pos, eid in slots}
    succ = [None] * len(slots)
    out_pending = {}   # 槽位 -> 目标源事件 id（段出口待决）
    for idx, (pos, eid) in enumerate(slots):
        kind, tid, ok = edge[eid]
        if not ok:
            continue
        if kind == 'intra':
            target = node_of.get((pos, tid))
            if target is not None:
                succ[idx] = (target, 0)
        elif kind == 'cross':
            target_measure = by_id[tid]['measure']
            if pos + 1 < size:
                if seq[pos + 1] == target_measure:
                    succ[idx] = (node_of[(pos + 1, tid)], 0)
                # 下一次小节在段内但小节不匹配：断链
            elif seq[0] == target_measure:
                # 末槽且新一轮首小节匹配：段内回绕边；末轮跨段由走链处理
                succ[idx] = (node_of[(0, tid)], 1)
            else:
                # 末槽且目标只可能在下一段：段出口待决
                out_pending[idx] = tid
    pred = [None] * len(slots)
    for idx, out in enumerate(succ):
        if out is not None:
            pred[out[0]] = (idx, out[1])
    # 段入口待决：段首（pos==0）合格事件若被段外来源的跨小节声明指向，
    # 且该来源事件不在本段（否则已由段内边表达）。入边唯一性已在谱面校验。
    in_pending = {}
    for idx, (pos, eid) in enumerate(slots):
        if pos != 0:
            continue
        source = incoming.get(eid)
        if source is None:
            continue
        kind, _tid, ok = edge[source]
        if ok and kind == 'cross' and source not in event_slots:
            in_pending[idx] = source
    # 回绕边：顺溯跨段需要目标事件 id，回溯跨段需要来源事件 id。
    wrap_target = {}
    wrap_source = {}
    for idx, out in enumerate(succ):
        if out is not None and out[1] == 1:
            wrap_target[idx] = slots[out[0]][1]
            wrap_source[out[0]] = slots[idx][1]
    return {'slots': slots, 'node_of': node_of, 'succ': succ,
            'pred': pred, 'rec': _functional_records(succ),
            'prec': _functional_records(pred),
            'out_pending': out_pending, 'in_pending': in_pending,
            'wrap_target': wrap_target, 'wrap_source': wrap_source}


def _to_boundary(table, slot, rnd, repeat, backward):
    """沿同一编排段走到段边界，整圈循环按算术跳过。

    返回 (边界内侧槽位, 本段经过的节点数, 跨段信号, 停止轮次)。
    跨段信号为 ``('wrap', 事件id)``（末轮回绕边）、``('pending', 事件id)``
    （段外待决边）或 None；信号能否兑现由调用方按相邻段判定。
    """
    if backward:
        edges, records, pending = table['pred'], table['prec'], \
            table['in_pending']
        budget = rnd + 1
    else:
        edges, records, pending = table['succ'], table['rec'], \
            table['out_pending']
        budget = repeat - rnd
    rec = records[slot]
    count = 1
    cur = slot
    if rec[TERM] == -1:
        # 先显式走完环前缀（至多单轮槽位数），途中可能先碰到边界
        for _ in range(rec[PRE_STEPS]):
            nxt, weight = edges[cur]
            if weight == 1:
                budget -= 1
                if budget == 0:
                    stop_rnd = 0 if backward else repeat - 1
                    eid = (table['wrap_source'][cur] if backward
                           else table['wrap_target'][cur])
                    return cur, count, ('wrap', eid), stop_rnd
            cur = nxt
            count += 1
        # 已在环入口，按整圈压缩
        cyc_w = rec[CYC_W]
        skip = (budget - 1) // cyc_w
        count += skip * rec[CYC_LEN]
        budget -= skip * cyc_w
    while True:
        out = edges[cur]
        if out is None:
            stop_rnd = budget - 1 if backward else repeat - budget
            if budget == 1:
                pid = pending.get(cur)
                if pid is not None:
                    return cur, count, ('pending', pid), stop_rnd
            return cur, count, None, stop_rnd
        nxt, weight = out
        if weight == 1:
            budget -= 1
            if budget == 0:
                stop_rnd = 0 if backward else repeat - 1
                eid = (table['wrap_source'][cur] if backward
                       else table['wrap_target'][cur])
                return cur, count, ('wrap', eid), stop_rnd
        cur = nxt
        count += 1


def _ordinal_of(section, table, slot, rnd):
    pos = table['slots'][slot][0]
    return section['ord_base'] + rnd * section['L'] + pos


def _find_head(built, tables, si, rnd, slot, by_id):
    """回溯链首，返回 (ordinal, event_id, 从起点到链首的节点总数)。"""
    counted = 0
    while True:
        section = built[si]
        table = tables[si]
        rec = table['prec'][slot]
        if rec[TERM] >= 0 and rnd >= rec[TOTAL_W]:
            head_rnd = rnd - rec[TOTAL_W]
            head_slot = rec[TERM]
            source_id = table['in_pending'].get(head_slot)
            can_cross = (source_id is not None and head_rnd == 0
                         and si > 0
                         and by_id[source_id]['measure']
                         == built[si - 1]['seq'][-1])
            if can_cross:
                prev = built[si - 1]
                slot = tables[si - 1]['node_of'][
                    (prev['L'] - 1, source_id)]
                counted += rec[STEPS] + 1
                # 下一段第 0 轮的前驱，是上一段的最后一轮。
                rnd = prev['repeat'] - 1
                si -= 1
                continue
            if source_id is None or head_rnd == 0 or si == 0:
                # 无待决边：真链首。第 0 轮待决边但无合格上一段：同样终止。
                return (_ordinal_of(section, table, head_slot, head_rnd),
                        table['slots'][head_slot][1],
                        counted + rec[STEPS] + 1)
            # 非第 0 轮：该槽位经段内回绕边继续回溯，走边界逻辑。
        inside, n_nodes, signal, stop_rnd = _to_boundary(
            table, slot, rnd, section['repeat'], backward=True)
        counted += n_nodes
        if signal is None or si == 0:
            return (_ordinal_of(section, table, inside, stop_rnd),
                    table['slots'][inside][1], counted)
        kind, pid = signal
        if by_id[pid]['measure'] != built[si - 1]['seq'][-1]:
            return (_ordinal_of(section, table, inside, stop_rnd),
                    table['slots'][inside][1], counted)
        prev = built[si - 1]
        slot = tables[si - 1]['node_of'][(prev['L'] - 1, pid)]
        # 下一段第 0 轮的前驱，是上一段的最后一轮。
        rnd = prev['repeat'] - 1
        si -= 1


def _find_tail(built, tables, si, rnd, slot, by_id):
    """顺溯链尾，返回 (ordinal, event_id, 从起点到链尾的节点总数)。"""
    counted = 0
    while True:
        section = built[si]
        table = tables[si]
        rec = table['rec'][slot]
        remaining = section['repeat'] - 1 - rnd
        if rec[TERM] >= 0 and rec[TOTAL_W] <= remaining:
            tail_rnd = rnd + rec[TOTAL_W]
            tail_slot = rec[TERM]
            target_id = table['out_pending'].get(tail_slot)
            at_last_round = tail_rnd == section['repeat'] - 1
            can_cross = (target_id is not None and at_last_round
                         and si < len(built) - 1
                         and by_id[target_id]['measure']
                         == built[si + 1]['seq'][0])
            if not can_cross:
                if target_id is None or at_last_round:
                    # 无待决边：真终止。末轮待决边但无合格下一段：同样终止。
                    return (_ordinal_of(section, table, tail_slot,
                                        tail_rnd),
                            table['slots'][tail_slot][1],
                            counted + rec[STEPS] + 1)
                # 非末轮：该槽位经段内回绕边继续，落到边界走链处理。
            else:
                slot = tables[si + 1]['node_of'][(0, target_id)]
                counted += rec[STEPS] + 1
                # 上一段最后一轮的后继，是下一段的第 0 轮。
                rnd = 0
                si += 1
                continue
        inside, n_nodes, signal, stop_rnd = _to_boundary(
            table, slot, rnd, section['repeat'], backward=False)
        counted += n_nodes
        if signal is None or si == len(built) - 1:
            return (_ordinal_of(section, table, inside, stop_rnd),
                    table['slots'][inside][1], counted)
        _kind, tid = signal
        nxt_section = built[si + 1]
        if by_id[tid]['measure'] != nxt_section['seq'][0]:
            return (_ordinal_of(section, table, inside, stop_rnd),
                    table['slots'][inside][1], counted)
        slot = tables[si + 1]['node_of'][(0, tid)]
        # 上一段最后一轮的后继，是下一段的第 0 轮。
        rnd = 0
        si += 1


def _tie_fulfilled(e, ordinal, total_ord, built, ord_bases, by_id, edge):
    kind, _tid, ok = edge[e['id']]
    if not ok:
        return False
    if kind == 'intra':
        return True
    if kind != 'cross':
        return False
    if ordinal + 1 >= total_ord:
        return False
    next_measure = _locate(built, ord_bases, ordinal + 1)[3]
    return by_id[e['tie_to']]['measure'] == next_measure

def _coverage_diagnostics(ordinal, mid, events, voices, length):
    out = []
    ml = length[mid]
    for voice in voices:
        cursor = Fraction(0)
        selected = [e for e in events if e['measure'] == mid
                    and e['voice'] == voice]
        selected.sort(key=lambda e: (e['start'], e['id']))
        for e in selected:
            start = e['start']
            end = start + e['duration']
            if start > cursor and cursor < ml:
                out.append({'kind': 'gap', 'voice': voice, 'measure': mid,
                            'ordinal': ordinal,
                            'start': _fstr(cursor),
                            'end': _fstr(min(start, ml))})
            if start < cursor:
                out.append({'kind': 'overlap', 'event': e['id'],
                            'voice': voice, 'measure': mid,
                            'ordinal': ordinal})
            if end > ml:
                out.append({'kind': 'overrun', 'event': e['id'],
                            'voice': voice, 'measure': mid,
                            'ordinal': ordinal})
            cursor = max(cursor, end)
        if cursor < ml:
            out.append({'kind': 'gap', 'voice': voice, 'measure': mid,
                        'ordinal': ordinal,
                        'start': _fstr(cursor), 'end': _fstr(ml)})
    return out


def process_window(score, sections, first, stop):
    if type(first) is not int or type(stop) is not int:
        raise ValueError('window bounds must be integers')
    (measures, events, length, by_id, in_measure, eligible, edge,
     incoming) = _build_score(score)
    built, ord_bases, total_ord, total_time = _build_sections(
        measures, sections, length)
    if not (0 <= first < stop <= total_ord
            and 1 <= stop - first <= MAX_WINDOW):
        raise ValueError('window must cover 1..%d measures within arrangement'
                         % MAX_WINDOW)
    tables = [_build_table(s, in_measure, eligible, edge, by_id, incoming)
              for s in built]
    voices = sorted({e['voice'] for e in events})

    window_start = _locate(built, ord_bases, first)[4]
    window_end = (total_time if stop == total_ord
                  else _locate(built, ord_bases, stop)[4])

    out_measures = []
    groups = {}
    visible_order = []
    diagnostics = []
    for ordinal in range(first, stop):
        si, rnd, pos, mid, occ_start, ml = _locate(
            built, ord_bases, ordinal)
        out_measures.append({
            'ordinal': ordinal, 'measure': mid,
            'start': _fstr(occ_start), 'length': _fstr(ml),
        })
        diagnostics.extend(_coverage_diagnostics(
            ordinal, mid, events, voices, length))
        table = tables[si]
        for ev in in_measure.get(mid, ()):
            if ev['tie_to'] is not None and not _tie_fulfilled(
                    ev, ordinal, total_ord, built, ord_bases,
                    by_id, edge):
                diagnostics.append({
                    'kind': 'invalid_tie', 'ordinal': ordinal,
                    'from': ev['id'], 'to': ev['tie_to'],
                })
            if not eligible[ev['id']]:
                continue
            slot = table['node_of'][(pos, ev['id'])]
            key = _find_head(built, tables, si, rnd, slot, by_id)[:2]
            if key not in groups:
                groups[key] = []
                visible_order.append(key)
            groups[key].append((ordinal, ev['id'], ev['start']))

    spans = []
    for head_ord, head_id in visible_order:
        hsi, hrnd, hpos, _mid, head_occ_start, _ = _locate(
            built, ord_bases, head_ord)
        head_table = tables[hsi]
        head_slot = head_table['node_of'][(hpos, head_id)]
        tail_ord, tail_id, member_count = _find_tail(
            built, tables, hsi, hrnd, head_slot, by_id)
        tail_occ_start = _locate(built, ord_bases, tail_ord)[4]
        head_ev = by_id[head_id]
        tail_ev = by_id[tail_id]
        full_start = head_occ_start + head_ev['start']
        full_end = tail_occ_start + tail_ev['start'] + tail_ev['duration']
        clip_start = max(full_start, window_start)
        clip_end = min(full_end, window_end)
        if clip_end <= clip_start:
            continue
        visible = sorted(groups[(head_ord, head_id)],
                         key=lambda item: (item[0], item[2], item[1]))
        spans.append({
            'voice': head_ev['voice'],
            'pitches': head_ev['pitches'],
            'start': _fstr(clip_start),
            'end': _fstr(clip_end),
            'full_start': _fstr(full_start),
            'full_end': _fstr(full_end),
            'first_event': [head_ord, head_id],
            'last_event': [tail_ord, tail_id],
            'member_count': member_count,
            'visible_events': [[o, eid] for o, eid, _ in visible],
        })
    spans.sort(key=lambda s: (Fraction(s['start']), s['voice'],
                              s['first_event'][0], s['first_event'][1]))
    return {'measures': out_measures, 'spans': spans,
            'diagnostics': diagnostics}
