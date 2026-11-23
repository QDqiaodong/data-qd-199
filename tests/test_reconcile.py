import json
import unittest
from fractions import Fraction

from score_spans.reconcile import (
    InvalidCheckpoint, InvalidLabel, InvalidScore, InvalidSource,
    ReconcileError, Reconciler, StaleRevision, TicketRejected,
    open_batch)


def fp(value):
    f = Fraction(value)
    return [f.numerator, f.denominator]


def note(eid, start, duration, pitch=60, voice='v1', measure='m1',
         tie_to=None):
    event = {'id': eid, 'voice': voice, 'measure': measure,
             'start': fp(start), 'duration': fp(duration),
             'pitches': [pitch]}
    if tie_to is not None:
        event['tie_to'] = tie_to
    return event


def score(events, measures=None, transpose=0):
    return {'measures': measures
            or [{'id': 'm1', 'numerator': 4, 'denominator': 4}],
            'events': events, 'transpose': transpose}


def reload(reconciler):
    """只靠最新 JSON 检查点重建状态（模拟标注方多年后回传）。"""
    return Reconciler.from_json(reconciler.to_json())


class SplitCarryTests(unittest.TestCase):
    """验收 1：拆分接续、缺来源作废、竞争关联不可挑选。"""

    def base_score(self):
        # A 在 [0,1)、B 在 [1,2)，同小节两条独立发音链。
        return score([note('a', 0, 1), note('b', 1, 1)])

    def split_score(self, a1='a1', a2='a2', bid='b'):
        # A 拆成前 1/3 与后 2/3 两个精确相接的合法延音事件。
        return score([
            note(a1, 0, Fraction(1, 3), tie_to=a2),
            note(a2, Fraction(1, 3), Fraction(2, 3)),
            note(bid, 1, 1)])

    def test_split_rename_reload_then_old_ticket_consumed_once(self):
        rec = open_batch('B-1', self.base_score())
        self.assertEqual(rec.revision, 0)
        delivery = rec.delivery()
        self.assertEqual([r['ticket'] for r in delivery['pending']], [0, 1])
        self.assertEqual(delivery['confirmed'], [])
        # B 的工作单先确认。
        rec.submit('B-1', 1, 'accent')

        # 修订 1：完整声明 A 到两个拆分成员的来源以及 B 的承接。
        rec.apply_revision(1, self.split_score(),
                           [['a', 'a1'], ['a', 'a2'], ['b', 'b']])
        d1 = rec.delivery()
        self.assertEqual(d1['revision'], 1)
        self.assertEqual(len(d1['pending']), 1)
        pending_a = d1['pending'][0]
        self.assertEqual(pending_a['ticket'], 0)  # 原 A 票保留回传资格
        self.assertEqual(pending_a['events'], ['a1', 'a2'])  # 用新成员
        self.assertEqual(pending_a['start'], '0')
        self.assertEqual(pending_a['end'], '1')
        confirmed_b = d1['confirmed'][0]
        self.assertEqual(confirmed_b['label'], 'accent')
        self.assertEqual(confirmed_b['events'], ['b'])

        # 修订 2：只改 A 的成员 ID 并完整接续；B 仍是纯改名承接。
        rec.apply_revision(
            2, self.split_score(a1='c1', a2='c2'),
            [['a1', 'c1'], ['a2', 'c2'], ['b', 'b']])
        rec = reload(rec)  # 只靠最新序列化再载入的状态
        d2 = rec.delivery()
        pending_a = d2['pending'][0]
        self.assertEqual(pending_a['ticket'], 0)  # 工作单跨多次修订仍有效
        self.assertEqual(pending_a['events'], ['c1', 'c2'])  # 最新成员
        self.assertEqual(d2['confirmed'][0]['events'], ['b'])
        self.assertEqual(d2['confirmed'][0]['label'], 'accent')

        # 原 A 票只能消费一次，标注行用最新两个成员。
        rec.submit('B-1', 0, 'tenuto')
        d3 = rec.delivery()
        self.assertEqual(d3['pending'], [])
        labels = {tuple(r['events']): r['label'] for r in d3['confirmed']}
        self.assertEqual(labels[('c1', 'c2')], 'tenuto')
        self.assertEqual(labels[('b',)], 'accent')
        with self.assertRaises(TicketRejected):  # 同票再次回传拒绝
            rec.submit('B-1', 0, 'plain')

    def test_missing_second_source_voids_old_ticket(self):
        rec = open_batch('B-1', self.base_score())
        rec.submit('B-1', 1, 'accent')
        new_score = self.split_score()
        # 保持新谱不变，只删掉 A 到后半段的来源对。
        rec.apply_revision(1, new_score,
                           [['a', 'a1'], ['b', 'b']])
        d1 = rec.delivery()
        self.assertEqual(len(d1['pending']), 1)
        new_ticket = d1['pending'][0]['ticket']
        self.assertEqual(new_ticket, 2)  # 旧票作废，批内票号不复用
        self.assertEqual(d1['pending'][0]['events'], ['a1', 'a2'])
        self.assertEqual(d1['confirmed'][0]['label'], 'accent')  # B 不受影响
        with self.assertRaises(TicketRejected):  # 原 A 票必须作废
            rec.submit('B-1', 0, 'tenuto')
        rec.submit('B-1', new_ticket, 'plain')  # 新票可完成这条链
        self.assertEqual(
            {tuple(r['events']): r['label']
             for r in rec.delivery()['confirmed']}[('a1', 'a2')], 'plain')

    def test_competing_source_keeps_neither_relation(self):
        rec = open_batch('B-1', self.base_score())
        rec.submit('B-1', 1, 'accent')
        # 来源形状本身合法（无重复对、两端均可解析为非休止事件），但
        # b->a2 使新 A 链同时连回 A、B 两条旧链：不能只保留最像的一条。
        rec.apply_revision(
            1, self.split_score(),
            [['a', 'a1'], ['a', 'a2'], ['b', 'b'], ['b', 'a2']])
        d1 = rec.delivery()
        self.assertEqual(len(d1['pending']), 2)  # A、B 都重发新票
        self.assertEqual(d1['confirmed'], [])    # B 的旧标签不能沿用
        self.assertEqual(sorted(r['ticket'] for r in d1['pending']), [2, 3])
        with self.assertRaises(TicketRejected):  # A 的旧票不能沿用
            rec.submit('B-1', 0, 'tenuto')
        with self.assertRaises(TicketRejected):  # B 的旧票同样作废
            rec.submit('B-1', 1, 'plain')


class ContentChangeTests(unittest.TestCase):
    """验收 2：改内容退出交付、恢复不复活旧票、检查点不累积历史。"""

    def test_confirmed_chain_change_pitch_or_position_issues_new_ticket(self):
        rec = open_batch('B-2', score([note('x', 0, 1), note('y', 2, 1)]))
        rec.submit('B-2', 0, 'plain')  # x 已确认，y 待标注
        # 已确认链改音高，声明完整延续关系：旧标签仍须退出。
        rec.apply_revision(1, score([note('x2', 0, 1, pitch=61),
                                     note('y', 2, 1)]),
                           [['x', 'x2'], ['y', 'y']])
        d1 = rec.delivery()
        # x 旧标签退出并产生新票；y 的待标注票 1 原样保留。
        self.assertEqual(sorted(r['ticket'] for r in d1['pending']), [1, 2])
        self.assertEqual(d1['confirmed'], [])
        rec.submit('B-2', 2, 'accent')
        rec.apply_revision(2, score([note('x3', 0, 1, pitch=61),
                                     note('y2', Fraction(3, 2), 1)]),
                           [['x2', 'x3'], ['y', 'y2']])
        d2 = rec.delivery()
        self.assertEqual(len(d2['pending']), 1)
        self.assertEqual(d2['pending'][0]['events'], ['y2'])
        self.assertEqual(d2['pending'][0]['start'], '3/2')
        # 被拒修订不得改变任何状态。
        with self.assertRaises(StaleRevision):
            rec.apply_revision(9, score([note('z', 0, 1)]), [])
        self.assertEqual(rec.revision, 2)
        # 已确认的 x3 标签仍在交付中。
        self.assertEqual(rec.delivery()['confirmed'][0]['events'], ['x3'])

    def test_change_then_restore_never_revives_old_tickets(self):
        rec = open_batch('B-3', score([
            note('y', 0, 1), note('z', 3, 1, voice='v2')]))
        rec.submit('B-3', 1, 'tenuto')  # 旁边真正连续的 z 先确认
        old_ticket = 0
        # 第一步：y 改位置，完整来源。
        rec.apply_revision(1, score([
            note('y1', 1, 1), note('z', 3, 1, voice='v2')]),
            [['y', 'y1'], ['z', 'z']])
        self.assertEqual(rec.delivery()['confirmed'][0]['events'], ['z'])
        ticket_after_move = rec.delivery()['pending'][0]['ticket']
        self.assertNotEqual(ticket_after_move, old_ticket)
        # 第二步：恢复为完全相同的旧谱事件（含旧 ID），完整来源。
        rec.apply_revision(2, score([
            note('y', 0, 1), note('z', 3, 1, voice='v2')]),
            [['y1', 'y'], ['z', 'z']])
        rec = reload(rec)
        d2 = rec.delivery()
        self.assertEqual(d2['pending'][0]['events'], ['y'])
        latest_ticket = d2['pending'][0]['ticket']
        self.assertNotIn(latest_ticket, (old_ticket, ticket_after_move))
        with self.assertRaises(TicketRejected):  # 曾经发出的旧票不可用
            rec.submit('B-3', old_ticket, 'plain')
        with self.assertRaises(TicketRejected):  # 中间轮次的票也不可用
            rec.submit('B-3', ticket_after_move, 'plain')
        rec.submit('B-3', latest_ticket, 'accent')  # 最新票可用
        labels = {tuple(r['events']): r['label']
                  for r in rec.delivery()['confirmed']}
        self.assertEqual(labels[('y',)], 'accent')
        self.assertEqual(labels[('z',)], 'tenuto')  # 连续工作不受影响

    def test_repeated_undo_redo_checkpoint_keeps_fixed_shape(self):
        # 每轮：原谱 y 改名移到位置 1（改位置 -> 新票），再恢复含旧 ID
        # 的原谱（恢复 -> 又是新票）；多轮撤销与重发后重新载入检查点。
        original = score([note('y', 0, 1)])
        rec = open_batch('B-4', original)
        first_ticket = rec.delivery()['pending'][0]['ticket']
        for round_no in range(1, 5):
            moved_id = f'm{round_no}'
            moved = score([note(moved_id, 1, 1)])
            before_id = 'y' if round_no == 1 else f'z{round_no - 1}'
            rec.apply_revision(round_no * 2 - 1, moved,
                               [[before_id, moved_id]])
            rec = reload(rec)
            restored_id = f'z{round_no}'
            rec.apply_revision(round_no * 2,
                               score([note(restored_id, 0, 1)]),
                               [[moved_id, restored_id]])
            rec = reload(rec)
            # 每轮恢复后都是新票，最初的票永不复活。
            with self.assertRaises(TicketRejected):
                rec.submit('B-4', first_ticket, 'plain')
            state = json.loads(rec.to_json())
            # 检查点形状固定：无历次谱、关系、已销票明细或修订记录。
            self.assertEqual(set(state), {
                'format', 'batch_id', 'revision', 'next_ticket',
                'score', 'entries'})
            self.assertEqual(state['revision'], round_no * 2)
            self.assertEqual(len(state['entries']), 1)  # 只随当前链数变化
            self.assertNotIn('history', state)
            self.assertNotIn('spent_tickets', state)
        latest = rec.delivery()['pending'][0]['ticket']
        rec.submit('B-4', latest, 'plain')
        self.assertEqual(rec.delivery()['confirmed'][0]['label'], 'plain')


class ValidationAndGuardTests(unittest.TestCase):
    """验收 3：非法混合修订、过期号、无效回传、空谱/休止、多对一与防误伤。"""

    def setUp(self):
        self.rec = open_batch('B-5', score([
            note('a', 0, 1), note('b', 1, 1), note('c', 2, 1)]))
        self.rec.submit('B-5', 1, 'accent')  # b 已确认
        self.snapshot = self.rec.to_json()

    def assert_unchanged(self):
        self.assertEqual(self.rec.to_json(), self.snapshot)

    def test_mixed_revision_with_illegal_pair_rejected_atomically(self):
        good_new = score([note('a2', 0, 1), note('b', 1, 1),
                          note('c2', 2, 1)])
        cases = [
            [['a', 'a2'], ['b', 'b'], ['c', 'ghost']],       # 右引用不存在
            [['a', 'a2'], ['b', 'b'], ['ghost', 'c2']],       # 左引用不存在
            [['a', 'a2'], ['b', 'b'], ['a', 'a2']],           # 重复对
            'not-a-list', [['a']], [['a', 'a2', 'x']],        # 形状错误
            [['a', 'a2'], [7, 'b']],                          # 非字符串
        ]
        for bad in cases:
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidSource):
                    self.rec.apply_revision(1, good_new, bad)
                self.assert_unchanged()  # 其他链不丢标、不误收
        # 休止事件不能作为来源两端。
        rest_score = score([
            note('a2', 0, 1), note('b', 1, 1),
            {'id': 'r', 'voice': 'v1', 'measure': 'm1',
             'start': fp(2), 'duration': fp(1), 'pitches': []}])
        with self.assertRaises(InvalidSource):
            self.rec.apply_revision(1, rest_score,
                                    [['a', 'a2'], ['b', 'b'], ['c', 'r']])
        self.assert_unchanged()

    def test_stale_revision_number_distinguishable(self):
        same_score = score([note('a', 0, 1), note('b', 1, 1),
                            note('c', 2, 1)])
        for bad_no in (0, 2, -1, '1', 1.0, True):
            with self.subTest(bad_no=bad_no):
                with self.assertRaises(StaleRevision):
                    self.rec.apply_revision(
                        bad_no, same_score,
                        [['a', 'a'], ['b', 'b'], ['c', 'c']])
        self.assert_unchanged()

    def test_invalid_submissions_distinguishable(self):
        with self.assertRaises(InvalidLabel):
            self.rec.submit('B-5', 0, 'LOUD')
        with self.assertRaises(InvalidLabel):
            self.rec.submit('B-5', 0, None)
        with self.assertRaises(TicketRejected):  # 已消费票重复回传
            self.rec.submit('B-5', 1, 'plain')
        with self.assertRaises(TicketRejected):  # 不存在的票
            self.rec.submit('B-5', 99, 'plain')
        with self.assertRaises(TicketRejected):  # 其他批的票
            self.rec.submit('OTHER', 0, 'plain')
        with self.assertRaises(TicketRejected):
            self.rec.submit('B-5', '0', 'plain')
        self.assert_unchanged()
        # 标签先于票校验，两类错误可区分。
        with self.assertRaises(InvalidLabel):
            self.rec.submit('OTHER', 0, 'nope')

    def test_empty_and_rest_only_scores_open_and_revise(self):
        empty = open_batch('B-6', score([]))
        self.assertEqual(empty.delivery(),
                         {'revision': 0, 'pending': [], 'confirmed': []})
        rest_only = score([{'id': 'r', 'voice': 'v1', 'measure': 'm1',
                            'start': fp(0), 'duration': fp(1),
                            'pitches': []}])
        rest_batch = open_batch('B-7', rest_only)
        self.assertEqual(rest_batch.delivery()['pending'], [])
        # 留白/休止不妨碍后续修订加入发音事件。
        rest_batch.apply_revision(
            1, score([note('n', 0, 1),
                      {'id': 'r', 'voice': 'v1', 'measure': 'm1',
                       'start': fp(1), 'duration': fp(1), 'pitches': []}]),
            [])
        d1 = rest_batch.delivery()
        self.assertEqual(len(d1['pending']), 1)
        self.assertEqual(d1['pending'][0]['events'], ['n'])
        # 空谱修订也合法：所有当前链失去资格，交付清空。
        self.rec.apply_revision(1, score([]), [])
        self.assertEqual(self.rec.delivery(),
                         {'revision': 1, 'pending': [], 'confirmed': []})

    def test_many_to_one_tie_rewrite_drops_old_labels(self):
        rec = open_batch('B-8', score([note('c1', 0, 1),
                                       note('c2', 1, 1)]))
        rec.submit('B-8', 0, 'plain')
        rec.submit('B-8', 1, 'accent')
        merged = score([note('d1', 0, 1, tie_to='d2'),
                        note('d2', 1, 1)])
        # 多对一合法延音改写：新链连回两条旧链，不能沿用任一旧标签。
        rec.apply_revision(1, merged,
                           [['c1', 'd1'], ['c2', 'd2']])
        d1 = rec.delivery()
        self.assertEqual(d1['confirmed'], [])
        self.assertEqual(len(d1['pending']), 1)
        self.assertEqual(d1['pending'][0]['events'], ['d1', 'd2'])
        self.assertEqual(d1['pending'][0]['ticket'], 2)
        rec.submit('B-8', 2, 'tenuto')
        self.assertEqual(rec.delivery()['confirmed'][0]['label'], 'tenuto')

    def test_failure_in_one_chain_keeps_others_intact(self):
        # 同声部重叠：拒绝整次修订，b 的标签与 a、c 的票原样保留。
        bad_overlap = score([note('a', 0, 1), note('b', 1, 1),
                             note('c', Fraction(9, 10), 1)])
        with self.assertRaises(InvalidScore):
            self.rec.apply_revision(1, bad_overlap,
                                    [['a', 'a'], ['b', 'b'], ['c', 'c']])
        self.assert_unchanged()
        # 未兑现延音：a 声明延到 c，但 c 的起点 2 不等于 a 的终点 1。
        unfulfilled = score([note('a', 0, 1, tie_to='c'),
                             note('b', 1, 1), note('c', 2, 1)])
        with self.assertRaises(InvalidScore):
            self.rec.apply_revision(
                1, unfulfilled, [['a', 'a'], ['b', 'b'], ['c', 'c']])
        self.assert_unchanged()
        # 事件越出所属小节（4/4 小节长度为 4）。
        overrun = score([note('a', 0, 1), note('b', 1, 1),
                         note('c', 3, 2)])
        with self.assertRaises(InvalidScore):
            self.rec.apply_revision(1, overrun, [])
        self.assert_unchanged()

    def test_error_codes_are_distinguishable(self):
        classes = (InvalidScore, InvalidSource, StaleRevision,
                   TicketRejected, InvalidLabel, InvalidCheckpoint)
        codes = {cls.code for cls in classes}
        self.assertEqual(len(codes), len(classes))
        for cls in classes:
            self.assertTrue(issubclass(cls, ReconcileError))
        self.assertTrue(issubclass(InvalidScore, ValueError))
        self.assertTrue(issubclass(InvalidSource, ValueError))
        self.assertTrue(issubclass(InvalidLabel, ValueError))
        self.assertFalse(issubclass(StaleRevision, ValueError))

    def test_checkpoint_roundtrip_and_corruption(self):
        rec = reload(self.rec)
        self.assertEqual(rec.to_json(), self.snapshot)
        for broken in ('not json', '{}',
                       self.snapshot.replace('"revision": 0',
                                             '"revision": -1'),
                       self.snapshot.replace('score-spans.reconcile/1',
                                             'other/2')):
            with self.assertRaises(InvalidCheckpoint):
                Reconciler.from_json(broken)
        # 篡改 entries 与当前谱不一致同样拒绝。
        state = json.loads(self.snapshot)
        state['entries'][0]['events'] = ['tampered']
        with self.assertRaises(InvalidCheckpoint):
            Reconciler.from_json(json.dumps(state))
        state2 = json.loads(self.snapshot)
        state2['next_ticket'] = 0  # 开放票号超出计数范围
        with self.assertRaises(InvalidCheckpoint):
            Reconciler.from_json(json.dumps(state2))


class DeliveryShapeTests(unittest.TestCase):
    """交付行内容、排序与输入顺序无关性。"""

    def test_rows_carry_review_content_and_sorted_members(self):
        s = score([
            note('h', 0, Fraction(1, 3), tie_to='t'),
            note('t', Fraction(1, 3), Fraction(2, 3), pitch=60),
            note('late', 2, 1, voice='v2'),
            note('early', 0, Fraction(1, 2), voice='v2')])
        rec = open_batch('B-9', s)
        d = rec.delivery()
        self.assertEqual([(r['start'], r['voice']) for r in d['pending']],
                         [('0', 'v1'), ('0', 'v2'), ('2', 'v2')])
        chain_row = d['pending'][0]
        self.assertEqual(chain_row['events'], ['h', 't'])  # 按时间排列
        self.assertEqual(chain_row['pitches'], [60])
        self.assertEqual(chain_row['end'], '1')
        self.assertEqual(chain_row['batch_id'], 'B-9')
        self.assertIn('ticket', chain_row)
        rec.submit('B-9', chain_row['ticket'], 'accent')
        confirmed = rec.delivery()['confirmed'][0]
        self.assertEqual(set(confirmed),
                         {'voice', 'pitches', 'start', 'end', 'events',
                          'label'})
        self.assertEqual(confirmed['label'], 'accent')

    def test_transpose_reflected_in_review_pitches(self):
        s = score([note('a', 0, 1, pitch=60)], transpose=2)
        rec = open_batch('B-10', s)
        self.assertEqual(rec.delivery()['pending'][0]['pitches'], [62])
        # 移调后越界的谱拒绝开批。
        with self.assertRaises(InvalidScore):
            open_batch('B-11', score([note('a', 0, 1, pitch=127)],
                                     transpose=1))

    def test_input_array_orders_do_not_matter(self):
        events = [note('a', 0, 1, tie_to='a2'),
                  note('a2', 1, 1), note('b', 2, 1)]
        shuffled = score(list(reversed(events)))
        r1 = open_batch('B-12', score(events)).delivery()
        r2 = open_batch('B-12', shuffled).delivery()
        self.assertEqual(json.dumps(r1, sort_keys=True),
                         json.dumps(r2, sort_keys=True))
        # 来源对顺序同样不影响沿用结果。
        rec = open_batch('B-13', score(events))
        new_score = score([note('c', 0, 1, tie_to='c2'),
                           note('c2', 1, 1), note('b2', 2, 1)])
        sources = [['b', 'b2'], ['a2', 'c2'], ['a', 'c']]
        rec.apply_revision(1, new_score, sources)
        d = rec.delivery()
        # 两条链都完整沿用：未消费票保留资格，成员换成新谱事件。
        self.assertEqual(sorted(r['ticket'] for r in d['pending']), [0, 1])
        self.assertEqual({tuple(r['events']) for r in d['pending']},
                         {('c', 'c2'), ('b2',)})
        self.assertEqual(d['confirmed'], [])

    def test_rests_and_gaps_create_no_work_items(self):
        s = score([
            {'id': 'r', 'voice': 'v1', 'measure': 'm1',
             'start': fp(0), 'duration': fp(1), 'pitches': []},
            note('n', 2, 1)])  # 前面留白
        rec = open_batch('B-14', s)
        self.assertEqual(len(rec.delivery()['pending']), 1)
        self.assertEqual(rec.delivery()['pending'][0]['events'], ['n'])

    def test_pure_rename_keeps_open_ticket_across_reload(self):
        rec = open_batch('B-15', score([note('a', 0, 1)]))
        rec.apply_revision(1, score([note('a2', 0, 1)]), [['a', 'a2']])
        rec = reload(rec)
        self.assertEqual(rec.delivery()['pending'][0]['ticket'], 0)
        rec.submit('B-15', 0, 'plain')
        self.assertEqual(rec.delivery()['confirmed'][0]['events'], ['a2'])


if __name__ == '__main__':
    unittest.main()
