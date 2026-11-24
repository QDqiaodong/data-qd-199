import copy
import json
import time
import unittest
from fractions import Fraction

from score_spans.recording import (
    AWAIT_ANCHOR, AWAIT_BLOCK, CLOSED, EMPTY_INTERVAL, READY, SEALED_GAP,
    UNDELIVERABLE, InvalidSnapshot, RecordingSnapshot, TicketNotReady,
    UnknownTicket, plan_snapshot, read_manifest)
from score_spans.service import process


def fp(value):
    f = Fraction(value)
    return [f.numerator, f.denominator]


def note(eid, start, duration, pitch=60, voice='v1', measure='m1',
         tie_to=None):
    event = {'id': eid, 'voice': voice, 'measure': measure,
             'start': fp(start), 'duration': fp(duration),
             'pitches': [pitch] if pitch is not None else []}
    if tie_to is not None:
        event['tie_to'] = tie_to
    return event


def rest(eid, start, duration, voice='v1', measure='m1'):
    return {'id': eid, 'voice': voice, 'measure': measure,
            'start': fp(start), 'duration': fp(duration), 'pitches': []}


def score(events, measures=None, transpose=0):
    return {'measures': measures
            or [{'id': 'm1', 'numerator': 4, 'denominator': 4}],
            'events': events, 'transpose': transpose}


def anchor(position, sample):
    return {'position': fp(position), 'sample': sample}


def block(bid, start, end):
    return {'id': bid, 'start': start, 'end': end}


def ticket(tid, start, end, closed=False):
    return {'id': tid, 'start': fp(start), 'end': fp(end), 'closed': closed}


def make_snapshot(score_obj=None, anchors=None, blocks=None, seal=8,
                  tickets=None, recording_id='R1'):
    if score_obj is None:
        # 验收谱：v1 链 n1->n2 从 0 跨过 1 到 2；v2 的 m 在 1 起音到 2。
        score_obj = score([
            note('n1', 0, 1, 60, voice='v1', tie_to='n2'),
            note('n2', 1, 1, 60, voice='v1'),
            note('m', 1, 1, 64, voice='v2')])
    if anchors is None:
        anchors = [anchor(0, 0), anchor(1, 3), anchor(2, 8)]
    if blocks is None:
        blocks = [block('X', 0, 2), block('Y', 2, 4),
                  block('Z', 4, 6), block('W', 6, 8)]
    if tickets is None:
        tickets = [ticket('B', 0, Fraction(1, 2)),
                   ticket('A', Fraction(1, 2), Fraction(3, 2))]
    return {'recording_id': recording_id, 'score': score_obj,
            'anchors': anchors, 'blocks': blocks, 'seal': seal,
            'tickets': tickets}


def status_map(plan):
    return {row['id']: row['status'] for row in plan['tickets']}


class AcceptanceExample1Tests(unittest.TestCase):
    """验收 1：完整锚点下两单就绪、W 回收、标签与分段换算。"""

    def setUp(self):
        self.snapshot = RecordingSnapshot(make_snapshot())
        self.plan = self.snapshot.plan()

    def test_statuses_and_reclaim(self):
        self.assertEqual(status_map(self.plan), {'A': READY, 'B': READY})
        self.assertEqual(self.plan['reclaimable_blocks'], ['W'])
        rows = {row['id']: row for row in self.plan['tickets']}
        self.assertEqual([rows['B']['sample_start'], rows['B']['sample_end']],
                         [0, 2])
        self.assertEqual([rows['A']['sample_start'], rows['A']['sample_end']],
                         [2, 6])

    def test_manifest_b_uses_only_x(self):
        manifest = self.snapshot.manifest('B')
        self.assertEqual(manifest['sample_start'], 0)
        self.assertEqual(manifest['sample_end'], 2)
        self.assertEqual(manifest['blocks'],
                         [{'id': 'X', 'offset_start': 0, 'offset_end': 2}])
        self.assertEqual(len(manifest['labels']), 1)
        label = manifest['labels'][0]
        self.assertEqual(label['voice'], 'v1')
        self.assertEqual(label['pitches'], [60])
        self.assertEqual(label['events'], ['n1', 'n2'])
        self.assertEqual((label['relative_start'], label['relative_end']),
                         (0, 2))
        self.assertTrue(label['contains_onset'])

    def test_manifest_a_uses_y_z_and_segment_wise_labels(self):
        manifest = self.snapshot.manifest('A')
        self.assertEqual(manifest['blocks'], [
            {'id': 'Y', 'offset_start': 0, 'offset_end': 2},
            {'id': 'Z', 'offset_start': 0, 'offset_end': 2}])
        first, second = manifest['labels']
        # 裁入的延音：相对采样点 0 开始，但不是本切片的起音。
        self.assertEqual(first['voice'], 'v1')
        self.assertEqual(first['events'], ['n1', 'n2'])
        self.assertEqual((first['relative_start'], first['relative_end']),
                         (0, 4))
        self.assertFalse(first['contains_onset'])
        # 另一声部恰在谱面 1 起音：段 (0,0)-(1,3) 下 F(1)=3，相对为 1；
        # 若错误地用切片两端拟合直线会得到 2。
        self.assertEqual(second['voice'], 'v2')
        self.assertEqual(second['events'], ['m'])
        self.assertEqual((second['relative_start'], second['relative_end']),
                         (1, 4))
        self.assertTrue(second['contains_onset'])

    def test_fractional_boundary_neither_refetched_nor_missed(self):
        b = self.snapshot.manifest('B')
        a = self.snapshot.manifest('A')
        # 相邻切片在分数边界 1/2（采样点 2）处首尾相接、不重不漏。
        self.assertEqual(b['sample_end'], a['sample_start'])
        fetched = [row['id'] for row in b['blocks']] + \
                  [row['id'] for row in a['blocks']]
        self.assertEqual(fetched, ['X', 'Y', 'Z'])
        self.assertEqual(len(set(fetched)), 3)

    def test_plan_does_not_prebuild_manifests(self):
        for row in self.plan['tickets']:
            self.assertNotIn('labels', row)
            self.assertNotIn('blocks', row)


class AcceptanceExample2Tests(unittest.TestCase):
    """验收 2：锚点追加、结束标记、删块、封口 4 与 8 的对比。"""

    def test_missing_last_anchor_keeps_y_z_w(self):
        snap = make_snapshot(anchors=[anchor(0, 0), anchor(1, 3)])
        plan = RecordingSnapshot(snap).plan()
        self.assertEqual(status_map(plan),
                         {'A': AWAIT_ANCHOR, 'B': READY})
        self.assertEqual(plan['reclaimable_blocks'], [])

    def test_closing_b_frees_x_but_keeps_anchor_retention(self):
        snap = make_snapshot(anchors=[anchor(0, 0), anchor(1, 3)],
                             tickets=[ticket('B', 0, Fraction(1, 2), True),
                                      ticket('A', Fraction(1, 2),
                                             Fraction(3, 2))])
        plan = RecordingSnapshot(snap).plan()
        self.assertEqual(status_map(plan),
                         {'A': AWAIT_ANCHOR, 'B': CLOSED})
        self.assertEqual(plan['reclaimable_blocks'], ['X'])

    def test_removing_x_then_completing_anchors_reproduces_manifest(self):
        first = RecordingSnapshot(make_snapshot())
        manifest_before = first.manifest('A')
        snap = make_snapshot(
            blocks=[block('Y', 2, 4), block('Z', 4, 6), block('W', 6, 8)])
        snapshot = RecordingSnapshot(snap)
        plan = snapshot.plan()
        self.assertEqual(plan['reclaimable_blocks'], ['W'])
        self.assertEqual(snapshot.manifest('A'), manifest_before)

    def test_seal_four_awaits_block_and_retains_y(self):
        snap = make_snapshot(
            blocks=[block('X', 0, 2), block('Y', 2, 4)], seal=4)
        plan = RecordingSnapshot(snap).plan()
        self.assertEqual(status_map(plan),
                         {'A': AWAIT_BLOCK, 'B': READY})
        # Y 必须为等块的 A 保留，X 为就绪的 B 保留。
        self.assertEqual(plan['reclaimable_blocks'], [])

    def test_seal_eight_makes_a_undeliverable_and_releases_y(self):
        snap = make_snapshot(
            blocks=[block('X', 0, 2), block('Y', 2, 4)], seal=8)
        plan = RecordingSnapshot(snap).plan()
        rows = {row['id']: row for row in plan['tickets']}
        self.assertEqual(rows['A']['status'], UNDELIVERABLE)
        self.assertEqual(rows['A']['reason'], SEALED_GAP)
        self.assertEqual(rows['B']['status'], READY)
        # A 不可交付放开 Y；B 仍就绪，X 仍不可回收。
        self.assertEqual(plan['reclaimable_blocks'], ['Y'])

    def test_read_manifest_rejects_non_ready_tickets(self):
        waiting = RecordingSnapshot(
            make_snapshot(anchors=[anchor(0, 0), anchor(1, 3)]))
        with self.assertRaises(TicketNotReady):
            waiting.manifest('A')
        sealed = RecordingSnapshot(
            make_snapshot(blocks=[block('X', 0, 2), block('Y', 2, 4)],
                          seal=8))
        with self.assertRaises(TicketNotReady):
            sealed.manifest('A')
        done = RecordingSnapshot(
            make_snapshot(tickets=[ticket('B', 0, Fraction(1, 2), True),
                                   ticket('A', Fraction(1, 2),
                                          Fraction(3, 2))]))
        with self.assertRaises(TicketNotReady):
            done.manifest('B')
        with self.assertRaises(UnknownTicket):
            sealed.manifest('NO-SUCH')
        with self.assertRaises(UnknownTicket):
            sealed.manifest(7)


class RetentionReclaimTests(unittest.TestCase):
    """跨封口块、多单共享、左端待校准、相接不占用等保留规则。"""

    def test_block_crossing_seal_is_never_reclaimed(self):
        snap = make_snapshot(
            anchors=[anchor(0, 0), anchor(2, 10)],
            blocks=[block('X', 0, 2), block('Y', 2, 10)],
            seal=8, tickets=[ticket('T', 0, 2)])
        snapshot = RecordingSnapshot(snap)
        self.assertEqual(status_map(snapshot.plan()), {'T': READY})
        # 交接结束后：X 完全在封口内可回收，Y 跨过封口不可整块回收。
        snap_closed = make_snapshot(
            anchors=[anchor(0, 0), anchor(2, 10)],
            blocks=[block('X', 0, 2), block('Y', 2, 10)],
            seal=8, tickets=[ticket('T', 0, 2, True)])
        self.assertEqual(
            RecordingSnapshot(snap_closed).plan()['reclaimable_blocks'],
            ['X'])

    def test_shared_block_waits_for_last_dependency(self):
        tickets = [ticket('P', 0, 1), ticket('Q', Fraction(1, 2), 2)]
        snap = make_snapshot(
            anchors=[anchor(0, 0), anchor(2, 8)],
            blocks=[block('K', 0, 8)], seal=8, tickets=tickets)
        snapshot = RecordingSnapshot(snap)
        self.assertEqual(snapshot.plan()['reclaimable_blocks'], [])
        # 只结束 P：K 仍被 Q 占用。
        snap['tickets'][0]['closed'] = True
        self.assertEqual(RecordingSnapshot(snap).plan()['reclaimable_blocks'],
                         [])
        # 两个依赖都消失后 K 才回收。
        snap['tickets'][1]['closed'] = True
        self.assertEqual(
            RecordingSnapshot(snap).plan()['reclaimable_blocks'], ['K'])

    def test_await_anchor_with_unknown_left_keeps_tail_from_last_anchor(self):
        # 只有 (0,0)：切片两端都待校准，尾区间从采样点 1 开始。
        snap = make_snapshot(
            anchors=[anchor(0, 0)],
            blocks=[block('touch', 0, 1), block('near', 1, 3),
                    block('far', 5, 9)],
            seal=9, tickets=[ticket('T', Fraction(1, 2), Fraction(3, 2))])
        plan = RecordingSnapshot(snap).plan()
        self.assertEqual(status_map(plan), {'T': AWAIT_ANCHOR})
        # 与尾区间 [1, ...) 刚好相接的 touch 不占用、可回收；near/far 保留。
        self.assertEqual(plan['reclaimable_blocks'], ['touch'])

    def test_await_anchor_with_known_left_keeps_from_ceil_of_left(self):
        # 锚点到谱面 1：左端 1/2 可换算 F=3/2 -> ceil 2；右端待锚点。
        snap = make_snapshot(
            anchors=[anchor(0, 0), anchor(1, 3)],
            blocks=[block('touch', 0, 2), block('keep', 2, 4),
                    block('later', 8, 9)],
            seal=9, tickets=[ticket('T', Fraction(1, 2), Fraction(3, 2))])
        plan = RecordingSnapshot(snap).plan()
        self.assertEqual(status_map(plan), {'T': AWAIT_ANCHOR})
        self.assertEqual(plan['reclaimable_blocks'], ['touch'])

    def test_ready_block_is_kept_until_handover_closes(self):
        snap = make_snapshot(
            anchors=[anchor(0, 0), anchor(1, 1)],
            blocks=[block('K', 0, 1)], seal=1,
            tickets=[ticket('T', 0, 1)])
        self.assertEqual(RecordingSnapshot(snap).plan()['reclaimable_blocks'],
                         [])
        snap['tickets'][0]['closed'] = True
        self.assertEqual(
            RecordingSnapshot(snap).plan()['reclaimable_blocks'], ['K'])

    def test_empty_interval_is_undeliverable_and_holds_no_blocks(self):
        # F(1)=1/4、F(2)=1/2，向上取整都为 1：整数区间为空。
        snap = make_snapshot(
            anchors=[anchor(0, 0), anchor(4, 1)],
            blocks=[block('K', 0, 1)], seal=1,
            tickets=[ticket('T', 1, 2)])
        snapshot = RecordingSnapshot(snap)
        row = snapshot.plan()['tickets'][0]
        self.assertEqual(row['status'], UNDELIVERABLE)
        self.assertEqual(row['reason'], EMPTY_INTERVAL)
        self.assertEqual(row['sample_start'], row['sample_end'])
        # 不可交付单不占块：K 无依赖且在封口内，可回收。
        self.assertEqual(snapshot.plan()['reclaimable_blocks'], ['K'])

    def test_cross_seal_block_covers_gap_below_seal(self):
        # 跨封口块 [6,12) 把封口 8 以下的部分录全：不判永久缺口。
        snap = make_snapshot(
            anchors=[anchor(0, 0), anchor(2, 10)],
            blocks=[block('head', 0, 4), block('tail', 6, 12)],
            seal=8, tickets=[ticket('T', 0, 2)])
        plan = RecordingSnapshot(snap).plan()
        # 4、5 两个采样点缺失且位于封口以下。
        self.assertEqual(status_map(plan), {'T': UNDELIVERABLE})
        # head 无依赖且在封口内可回收；tail 跨封口不可回收。
        self.assertEqual(plan['reclaimable_blocks'], ['head'])

    def test_undeliverable_and_closed_tickets_never_hold_blocks(self):
        # F(p)=5p。empty 区间两端都映射到 (9,10) 内：F(37/20)=9.25、
        # F(39/20)=9.75，向上取整都为 9 -> 整数区间为空，不可交付且不占
        # 块；done 已结束。
        snap = make_snapshot(
            anchors=[anchor(0, 0), anchor(2, 10)],
            blocks=[block('K', 0, 10)], seal=10,
            tickets=[ticket('empty', Fraction(37, 20), Fraction(39, 20)),
                     ticket('done', 0, 1, True)])
        plan = RecordingSnapshot(snap).plan()
        plan = RecordingSnapshot(snap).plan()
        rows = {row['id']: row['status'] for row in plan['tickets']}
        self.assertEqual(rows['empty'], UNDELIVERABLE)
        self.assertEqual(rows['done'], CLOSED)
        self.assertEqual(plan['reclaimable_blocks'], ['K'])


class ManifestDetailTests(unittest.TestCase):
    """清单偏移、标签裁剪/省略、休止切片与稳定顺序。"""

    def setUp(self):
        self.anchors = [anchor(0, 0), anchor(2, 8)]

    def test_offsets_clip_blocks_to_slice_with_no_outside_data(self):
        snap = make_snapshot(
            anchors=self.anchors,
            blocks=[block('a', 0, 3), block('b', 3, 5), block('c', 5, 10)],
            seal=10, tickets=[ticket('T', Fraction(1, 2), Fraction(3, 2))])
        # F(1/2)=2, F(3/2)=6 -> 整数区间 [2,6)。
        manifest = RecordingSnapshot(snap).manifest('T')
        self.assertEqual((manifest['sample_start'],
                          manifest['sample_end']), (2, 6))
        self.assertEqual(manifest['blocks'], [
            {'id': 'a', 'offset_start': 2, 'offset_end': 3},
            {'id': 'b', 'offset_start': 0, 'offset_end': 2},
            {'id': 'c', 'offset_start': 0, 'offset_end': 1}])

    def test_label_omitted_when_clipped_to_no_samples(self):
        # 选非整数映射坐标：F(p)=4p 下 F(4/3)=16/3；裁剪长度 1/10 个谱面
        # 单位只跨 0.4 个采样点，两端 ceil(16/3)=ceil(163/30)=6 -> 取不到
        # 采样点，标签省略；切片自身 [4/3, 7/4) 的整数区间 [6,7) 非空。
        s = score([note('n', Fraction(4, 3), Fraction(1, 10))])
        snap = make_snapshot(
            score_obj=s, anchors=self.anchors,
            blocks=[block('a', 0, 8)], seal=8,
            tickets=[ticket('T', Fraction(4, 3), Fraction(7, 4))])
        manifest = RecordingSnapshot(snap).manifest('T')
        self.assertEqual(manifest['labels'], [])
        # 取样区间本身非空（[6,7)），清单照常交付，不影响录音交付。
        self.assertEqual(manifest['sample_start'], 6)
        self.assertEqual(manifest['sample_end'], 7)

    def test_onset_rules_for_clipped_and_contained_chains(self):
        s = score([
            note('before', 0, 2, 60, voice='v1'),
            note('inside', Fraction(1, 2), 1, 62, voice='v2'),
            note('after', 1, 1, 64, voice='v3')])
        snap = make_snapshot(
            score_obj=s, anchors=self.anchors,
            blocks=[block('a', 0, 8)], seal=8,
            tickets=[ticket('T', Fraction(1, 2), Fraction(3, 2))])
        labels = RecordingSnapshot(snap).manifest('T')['labels']
        by_voice = {label['voice']: label for label in labels}
        # 从切片第一个采样点裁入的延音不是新起音；裁剪区间 [1/2,3/2)
        # 映射为相对 [0,4)（F(p)=4p：F(3/2)-F(1/2)=6-2=4）。
        self.assertFalse(by_voice['v1']['contains_onset'])
        self.assertEqual(
            (by_voice['v1']['relative_start'], by_voice['v1']['relative_end']),
            (0, 4))
        # 链首落在切片内才是真正起音；越过切片右界的链同样算。
        self.assertTrue(by_voice['v2']['contains_onset'])
        self.assertTrue(by_voice['v3']['contains_onset'])
        # 排序：按换算后起点采样点（v1、v2 同点再按声部），最后 v3。
        self.assertEqual([label['voice'] for label in labels],
                         ['v1', 'v2', 'v3'])

    def test_chain_starting_exactly_at_slice_start_has_onset(self):
        s = score([note('n', Fraction(1, 2), 1)])
        snap = make_snapshot(
            score_obj=s, anchors=self.anchors,
            blocks=[block('a', 0, 8)], seal=8,
            tickets=[ticket('T', Fraction(1, 2), Fraction(3, 2))])
        label = RecordingSnapshot(snap).manifest('T')['labels'][0]
        self.assertTrue(label['contains_onset'])
        self.assertEqual(label['relative_start'], 0)

    def test_rest_only_slice_delivers_audio_with_empty_labels(self):
        s = score([rest('r', 0, 1)])
        snap = make_snapshot(
            score_obj=s, anchors=self.anchors,
            blocks=[block('a', 0, 4)], seal=4,
            tickets=[ticket('T', 0, 1)])
        manifest = RecordingSnapshot(snap).manifest('T')
        self.assertEqual(manifest['labels'], [])
        self.assertEqual(manifest['blocks'],
                         [{'id': 'a', 'offset_start': 0, 'offset_end': 4}])

    def test_transposed_pitches_and_chain_event_order(self):
        s = score([
            note('n1', 0, 1, 60, tie_to='n2'),
            note('n2', 1, 1, 60)], transpose=2)
        snap = make_snapshot(
            score_obj=s, anchors=self.anchors,
            blocks=[block('a', 0, 8)], seal=8,
            tickets=[ticket('T', 0, 2)])
        label = RecordingSnapshot(snap).manifest('T')['labels'][0]
        self.assertEqual(label['pitches'], [62])
        self.assertEqual(label['events'], ['n1', 'n2'])

    def test_results_stable_under_reordering(self):
        kwargs = dict(
            anchors=self.anchors,
            blocks=[block('a', 0, 3), block('b', 3, 5), block('c', 5, 8)],
            seal=8,
            tickets=[ticket('T', Fraction(1, 2), Fraction(3, 2)),
                     ticket('U', 0, Fraction(1, 2))])
        base = make_snapshot(**kwargs)
        shuffled = copy.deepcopy(base)
        shuffled['blocks'] = list(reversed(shuffled['blocks']))
        shuffled['tickets'] = list(reversed(shuffled['tickets']))
        shuffled['score']['events'] = list(
            reversed(shuffled['score']['events']))
        one = RecordingSnapshot(base)
        other = RecordingSnapshot(shuffled)
        self.assertEqual(one.plan(), other.plan())
        self.assertEqual(one.manifest('T'), other.manifest('T'))

    def test_output_is_json_serializable_with_native_types(self):
        snapshot = RecordingSnapshot(make_snapshot())
        encoded = json.dumps(snapshot.plan(), sort_keys=True)
        self.assertIsInstance(encoded, str)
        encoded = json.dumps(snapshot.manifest('A'), sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded['ticket_id'], 'A')

    def test_functional_wrappers_match_class_entrypoints(self):
        snap = make_snapshot()
        self.assertEqual(plan_snapshot(copy.deepcopy(snap)),
                         RecordingSnapshot(snap).plan())
        self.assertEqual(read_manifest(copy.deepcopy(snap), 'B'),
                         RecordingSnapshot(snap).manifest('B'))


class ScaleTests(unittest.TestCase):
    """规模：20000 块、10000 个大量重叠切片、2000 锚点。"""

    def _build(self):
        # 源谱模型上限：200 小节、2000 事件；锚点/块/切片打满本模块上限。
        measures = [{'id': f'm{i}', 'numerator': 4, 'denominator': 4}
                    for i in range(200)]
        events = []
        for i in range(200):
            events.append(note(
                f'n{i}', 0, 4, voice='v1', measure=f'm{i}',
                tie_to=f'n{i + 1}' if i < 199 else None))
        big_score = score(events, measures=measures)
        # 全谱长 800；2000 个锚点均匀落在其上，首项 (0,0)。
        anchors = [anchor(0, 0)] + [
            anchor(Fraction(2 * i, 5), 10 * i) for i in range(1, 2000)]
        blocks = [block(f'b{j}', j, j + 1) for j in range(20000)]
        # 10000 个大量相互重叠的切片，全部位于全谱（长 800）之内。
        tickets = [ticket(f'T{i:05d}', i % 790, (i % 790) + 2)
                   for i in range(10000)]
        return make_snapshot(score_obj=big_score, anchors=anchors,
                             blocks=blocks, seal=20000, tickets=tickets)

    def test_global_plan_then_one_full_manifest(self):
        snapshot = RecordingSnapshot(self._build())
        started = time.perf_counter()
        plan = snapshot.plan()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 10.0)
        self.assertEqual(len(plan['tickets']), 10000)
        self.assertTrue(all(row['status'] == READY
                            for row in plan['tickets']))
        # 规划结果里只有状态与区间，没有任何按单清单/标签。
        self.assertTrue(all(set(row) <= {'id', 'status', 'sample_start',
                                         'sample_end'}
                            for row in plan['tickets']))
        manifest = snapshot.manifest('T00000')
        # F(p)=25p，T00000 区间 [0,2)：整数区间 [0,50)，五十块各整段。
        self.assertEqual((manifest['sample_start'],
                          manifest['sample_end']), (0, 50))
        self.assertEqual([row['id'] for row in manifest['blocks']],
                         [f'b{j}' for j in range(50)])
        self.assertTrue(all(row['offset_start'] == 0
                            and row['offset_end'] == 1
                            for row in manifest['blocks']))
        label = manifest['labels'][0]
        self.assertEqual(label['events'][:2], ['n0', 'n1'])
        self.assertTrue(label['contains_onset'])
        self.assertEqual((label['relative_start'], label['relative_end']),
                         (0, 50))
        # 远在所有切片之外的块无依赖且在封口内，必须如实判定可回收。
        self.assertIn('b19999', plan['reclaimable_blocks'])
        # 重复规划确定性且同样快。
        self.assertEqual(snapshot.plan(), plan)

    def test_limits_enforced(self):
        data = self._build()
        over_anchors = copy.deepcopy(data)
        over_anchors['anchors'].append(anchor(799, 1_000_000))
        with self.assertRaises(ValueError):
            RecordingSnapshot(over_anchors)
        over_blocks = copy.deepcopy(data)
        over_blocks['blocks'].append(block('extra', 20000, 20001))
        with self.assertRaises(ValueError):
            RecordingSnapshot(over_blocks)
        over_tickets = copy.deepcopy(data)
        over_tickets['tickets'].append(ticket('extra', 0, 1))
        with self.assertRaises(ValueError):
            RecordingSnapshot(over_tickets)


class ValidationTests(unittest.TestCase):
    """非法输入矩阵：任何失败都抛 InvalidSnapshot（也是 ValueError）。"""

    def base(self):
        return make_snapshot()

    def assert_invalid(self, mutate):
        data = self.base()
        mutate(data)
        with self.assertRaises(InvalidSnapshot):
            RecordingSnapshot(data)

    def test_shape_and_identity(self):
        self.assert_invalid(lambda d: d.pop('seal'))
        self.assert_invalid(lambda d: d.update(extra=1))
        self.assert_invalid(lambda d: d.update(recording_id='bad id!'))
        with self.assertRaises(InvalidSnapshot):
            RecordingSnapshot([])

    def test_anchor_rules(self):
        self.assert_invalid(lambda d: d.update(anchors=[]))
        self.assert_invalid(lambda d: d.update(
            anchors=[anchor(1, 0), anchor(2, 8)]))
        self.assert_invalid(lambda d: d.update(
            anchors=[anchor(0, 0), anchor(0, 3)]))
        self.assert_invalid(lambda d: d.update(
            anchors=[anchor(0, 0), anchor(1, 0)]))
        self.assert_invalid(lambda d: d.update(
            anchors=[anchor(0, 0), anchor(5, 3)]))  # 超出全谱末尾
        self.assert_invalid(lambda d: d.update(
            anchors=[{'position': [0, 1], 'sample': True}]))
        self.assert_invalid(lambda d: d.update(
            anchors=[{'position': [0, 1], 'sample': 2 ** 53}]))
        self.assert_invalid(lambda d: d.update(
            anchors=[{'position': [10 ** 9 + 1, 1], 'sample': 0}]))
        self.assert_invalid(lambda d: d.update(
            anchors=[{'position': [1, 0], 'sample': 0}]))
        self.assert_invalid(lambda d: d.update(
            anchors=[{'position': [1, 1_000_001], 'sample': 0}]))
        self.assert_invalid(lambda d: d.update(
            anchors=[{'position': '(0,1)', 'sample': 0}]))

    def test_block_rules(self):
        self.assert_invalid(lambda d: d.update(
            blocks=[block('X', 2, 2)]))                   # 零长度
        self.assert_invalid(lambda d: d.update(
            blocks=[block('X', 0, 3), block('Y', 2, 4)]))  # 相交
        self.assert_invalid(lambda d: d.update(
            blocks=[block('X', 0, 2), block('X', 2, 4)]))  # 重复 ID
        self.assert_invalid(lambda d: d.update(
            blocks=[block('X', True, 2)]))
        self.assert_invalid(lambda d: d.update(
            blocks=[block('X', -1, 2)]))
        self.assert_invalid(lambda d: d.update(
            blocks=[block('X', 0, 2 ** 53)]))

    def test_seal_rules(self):
        self.assert_invalid(lambda d: d.update(seal=True))
        self.assert_invalid(lambda d: d.update(seal=-1))
        self.assert_invalid(lambda d: d.update(seal=2 ** 53))
        self.assert_invalid(lambda d: d.update(seal=1.5))

    def test_ticket_rules(self):
        self.assert_invalid(lambda d: d.update(
            tickets=[ticket('T', 1, 1)]))                 # 零长度
        self.assert_invalid(lambda d: d.update(
            tickets=[ticket('T', 0, 5)]))                 # 越出全谱
        self.assert_invalid(lambda d: d.update(
            tickets=[ticket('T', 0, 1), ticket('T', 1, 2)]))  # 重复单号
        bad = ticket('T', 0, 1)
        bad['closed'] = 1
        self.assert_invalid(lambda d: d.update(tickets=[bad]))
        self.assert_invalid(lambda d: d.update(
            tickets=[{'id': 'T', 'start': [0, 1], 'end': [1, 1],
                      'closed': False, 'extra': 1}]))

    def test_strict_score_rejection_and_allowed_quiet_scores(self):
        self.assert_invalid(lambda d: d.update(score=score(
            [note('a', 0, 1), note('b', Fraction(9, 10), 1)])))  # 重叠
        self.assert_invalid(lambda d: d.update(score=score(
            [note('a', 3, 2)])))                               # 越界
        self.assert_invalid(lambda d: d.update(score=score(
            [note('a', 0, 1, tie_to='c'), note('c', 2, 1)])))  # 无效延音
        for quiet in (score([]), score([rest('r', 0, 1)])):
            snap = make_snapshot(score_obj=quiet,
                                 blocks=[block('a', 0, 4)],
                                 tickets=[ticket('T', 0, 1)])
            manifest = RecordingSnapshot(snap).manifest('T')
            self.assertEqual(manifest['labels'], [])

    def test_error_codes(self):
        classes = (InvalidSnapshot, UnknownTicket, TicketNotReady)
        codes = {cls.code for cls in classes}
        self.assertEqual(len(codes), len(classes))
        self.assertTrue(issubclass(InvalidSnapshot, ValueError))
        snapshot = RecordingSnapshot(self.base())
        self.assertTrue(issubclass(type(snapshot.plan()), dict))


class OldBehaviorRegressionTests(unittest.TestCase):
    """新模块不改变既有入口行为。"""

    def test_process_and_status_constants_unchanged(self):
        from pathlib import Path
        import json as _json
        data = _json.loads(Path('fixtures/input.json').read_text())
        result = process(data)
        self.assertEqual(result['diagnostics'], [])
        self.assertEqual(result['spans'], [{
            'events': ['n1', 'n2'], 'voice': 'v1', 'pitches': [62],
            'start': '0', 'end': '6'}])
        self.assertEqual(
            (CLOSED, AWAIT_ANCHOR, AWAIT_BLOCK, READY, UNDELIVERABLE),
            ('closed', 'await_anchor', 'await_block', 'ready',
             'undeliverable'))
        self.assertEqual((EMPTY_INTERVAL, SEALED_GAP),
                         ('empty_interval', 'sealed_gap'))


if __name__ == '__main__':
    unittest.main()
