"""带踏板键盘补录：计划生成、预览应用与边界验收。

测试从调用方角度覆盖：采集消息 -> 发音区间 -> 声部放置 -> 跨小节切分
-> 计划保存/重载 -> 写回当前谱。已有谱面处理 ``process`` 用作回归参照：
原发音不变，每个新起音恰好落成一条独立链。
"""

import copy
import itertools
import json
import unittest
from fractions import Fraction

from score_spans.overdub import (
    ApplyConflict, InvalidOverdub, NoPlacement, apply_overdub, plan_overdub)
from score_spans.service import process


def two_measures(events=(), transpose=0, meter=(2, 4)):
    return {'measures': [{'id': 'm1', 'numerator': meter[0],
                          'denominator': meter[1]},
                         {'id': 'm2', 'numerator': meter[0],
                          'denominator': meter[1]}],
            'events': list(events), 'transpose': transpose}


def down(tick, onset, pitch, candidates):
    return {'kind': 'key_down', 'tick': tick, 'onset_id': onset,
            'pitch': pitch, 'candidates': list(candidates)}


def up(tick, onset):
    return {'kind': 'key_up', 'tick': tick, 'onset_id': onset}


def pedal(tick, on):
    return {'kind': 'pedal', 'tick': tick, 'on': on}


def acceptance_messages(pedal_at=0, pedal_first=True):
    """验收 1 的采集消息：踏板起初踩下或改在 tick 48 踩下。"""
    if pedal_at == 0:
        return [pedal(0, True),
                down(0, 'S', 64, ['A', 'B']),
                up(48, 'S'),
                down(80, 'L', 64, ['A']),
                pedal(112, False),
                up(144, 'L')]
    # tick 48 的踏板消息与 S 抬键同 tick：数组顺序决定谁先生效。
    if pedal_first:
        return [down(0, 'S', 64, ['A', 'B']),
                pedal(48, True),
                up(48, 'S'),
                down(80, 'L', 64, ['A']),
                pedal(112, False),
                up(144, 'L')]
    return [down(0, 'S', 64, ['A', 'B']),
            up(48, 'S'),
            pedal(48, True),
            down(80, 'L', 64, ['A']),
            pedal(112, False),
            up(144, 'L')]


class PlanAcceptanceTests(unittest.TestCase):
    def test_pedal_from_zero_splits_and_transposes(self):
        score = two_measures(transpose=5)
        plan = plan_overdub(score, 'b1', 'm1', 48,
                            acceptance_messages(pedal_at=0))
        preview = {row['onset_id']: row for row in plan['onsets']}
        self.assertEqual((preview['S']['voice'], preview['S']['start'],
                          preview['S']['end']), ('B', '0', '7/3'))
        self.assertEqual((preview['L']['voice'], preview['L']['start'],
                          preview['L']['end']), ('A', '5/3', '3'))
        # S 跨小节：m1 的 [0,2) 与 m2 的 [0,1/3) 以延音相连。
        self.assertEqual(len(preview['S']['events']), 2)
        self.assertEqual(len(preview['L']['events']), 2)
        self.assertEqual(score['events'], [])

        applied = apply_overdub(score, json.loads(json.dumps(plan)))
        result = process(applied)
        # 留白允许；新增片段自身不产生重叠或越界。
        self.assertFalse([d for d in result['diagnostics']
                          if d['kind'] in ('overlap', 'overrun')])
        by_voice = {(s['voice'], s['start'], s['end']): s
                    for s in result['spans']}
        self.assertIn(('B', '0', '7/3'), by_voice)
        self.assertIn(('A', '5/3', '3'), by_voice)
        # 返回听到的音高（源谱 transpose=5、写入 pitch 59）。
        self.assertEqual(by_voice[('B', '0', '7/3')]['pitches'], [64])
        self.assertEqual(by_voice[('A', '5/3', '3')]['pitches'], [64])
        # 两次起音互不连延音：链成员各自为 2 且 ID 不同。
        self.assertEqual(len(by_voice[('B', '0', '7/3')]['events']), 2)
        self.assertEqual(len(by_voice[('A', '5/3', '3')]['events']), 2)
        self.assertNotEqual(by_voice[('B', '0', '7/3')]['events'],
                            by_voice[('A', '5/3', '3')]['events'])

    def test_pedal_at_down_tick_before_key_up(self):
        score = two_measures(transpose=5)
        plan = plan_overdub(score, 'b1', 'm1', 48,
                            acceptance_messages(pedal_at=48, pedal_first=True))
        preview = {row['onset_id']: row for row in plan['onsets']}
        self.assertEqual((preview['S']['voice'], preview['S']['end']),
                         ('B', '7/3'))
        self.assertEqual((preview['L']['voice'], preview['L']['end']),
                         ('A', '3'))

    def test_pedal_after_key_up_shortens_s(self):
        score = two_measures(transpose=5)
        plan = plan_overdub(score, 'b1', 'm1', 48,
                            acceptance_messages(pedal_at=48,
                                                pedal_first=False))
        preview = {row['onset_id']: row for row in plan['onsets']}
        # S 在踏板踩下前已抬键，只持续到 1；A 空闲，字典序最小放置为 A。
        self.assertEqual((preview['S']['voice'], preview['S']['start'],
                          preview['S']['end']), ('A', '0', '1'))
        # L 仍不能在 112 提前结束，踏板迟迟才抬起。
        self.assertEqual((preview['L']['voice'], preview['L']['start'],
                          preview['L']['end']), ('A', '5/3', '3'))

    def test_rest_occupies_voice_b(self):
        rest = {'id': 'r1', 'voice': 'B', 'measure': 'm1',
                'start': [1, 1], 'duration': [1, 1], 'pitches': []}
        occupied = two_measures([rest], transpose=5)
        snapshot = copy.deepcopy(occupied)
        with self.assertRaises(NoPlacement):
            plan_overdub(occupied, 'b1', 'm1', 48,
                         acceptance_messages(pedal_at=0))
        self.assertEqual(occupied, snapshot)  # 失败不改传入谱
        plan = plan_overdub(occupied, 'b1', 'm1', 48,
                            acceptance_messages(pedal_at=48,
                                                pedal_first=False))
        preview = {row['onset_id']: row for row in plan['onsets']}
        self.assertEqual(preview['S']['voice'], 'A')


class PlanPlacementTests(unittest.TestCase):
    def test_lexicographic_not_first_free_per_note(self):
        # X 覆盖 [0,3) 且只能 A/B；Y=[0,1) 只能 A；Z=[1,2) 只能 A。
        # 逐音取"第一个空声部"会把 X 放进 A，随后 Y 无处可去；完整放置
        # 必须回溯，字典序最小解为 (X=B, Y=A, Z=A)。
        score = two_measures(transpose=0)
        messages = [down(0, 'X', 60, ['A', 'B']),
                    down(0, 'Y', 62, ['A']),
                    up(48, 'Y'),
                    down(48, 'Z', 64, ['A']),
                    up(96, 'Z'),
                    up(144, 'X')]
        plan = plan_overdub(score, 'bx', 'm1', 48, messages)
        preview = {row['onset_id']: row['voice'] for row in plan['onsets']}
        self.assertEqual(preview, {'X': 'B', 'Y': 'A', 'Z': 'A'})
        self.assertEqual(self._minimal(messages), ['B', 'A', 'A'])
        applied = apply_overdub(score, plan)
        # Y、Z 虽同声部、同音高且紧邻，仍是两条独立链，不连延音。
        self.assertEqual({(s['voice'], s['start'], s['end'])
                          for s in process(applied)['spans']},
                         {('B', '0', '3'), ('A', '0', '1'),
                          ('A', '1', '2')})

    def test_lexicographic_with_rest_and_restricted_candidates(self):
        # B 在 m2 [2,3) 有显式休止（绝对 [2,3)）。P=[0,2)、Q=[1,3) 都允许
        # A/B：Q 放 A 会与 P 的 A 冲突，放 B 会撞休止，故 P 只能 B、Q 只能
        # A，字典序最小完整放置为 (P=B, Q=A)，不能按音逐个取第一个空声部。
        rest = {'id': 'r', 'voice': 'B', 'measure': 'm2',
                'start': [0, 1], 'duration': [1, 1], 'pitches': []}
        score = two_measures([rest], transpose=0)
        messages = [down(0, 'P', 60, ['A', 'B']),
                    down(48, 'Q', 62, ['A', 'B']),
                    up(96, 'P'),
                    up(144, 'Q')]
        plan = plan_overdub(score, 'bx', 'm1', 48, messages)
        preview = {row['onset_id']: row['voice'] for row in plan['onsets']}
        self.assertEqual(preview, {'P': 'B', 'Q': 'A'})
        self.assertEqual(self._minimal(
            messages, occupied={'B': [(Fraction(2), Fraction(3))]}),
            ['B', 'A'])
        applied = apply_overdub(score, plan)
        spans = {(s['voice'], s['start'], s['end'])
                 for s in process(applied)['spans']}
        self.assertIn(('B', '0', '2'), spans)
        self.assertIn(('A', '1', '3'), spans)
        self.assertEqual({s[0] for s in spans}, {'A', 'B'})

    def _intervals(self, messages):
        records = {}
        for message in messages:
            if message['kind'] == 'key_down':
                records[message['onset_id']] = {
                    'id': message['onset_id'], 'tick': message['tick'],
                    'candidates': sorted(set(message['candidates'])),
                    'start': None, 'end': None}
            elif message['kind'] == 'key_up':
                records[message['onset_id']]['end'] = message['tick']
        for record in records.values():
            record['start'] = record['tick']
        return list(records.values())

    def _minimal(self, messages, occupied=None, tpq=48):
        """小规模独立枚举：重算全部完整放置，返回字典序最小声部序列。

        与实现无关：直接对候选笛卡尔积做穷举，按 ``(tick, 起音 ID)`` 顺序
        检查区间是否与同声部占用相交，取字典序最小的可行组合。
        """
        order = sorted(self._intervals(messages),
                       key=lambda row: (row['tick'], row['id']))
        voices = sorted({v for row in order for v in row['candidates']}
                        | set((occupied or {}).keys()))
        existing = {voice: list(slots)
                    for voice, slots in (occupied or {}).items()}
        best = None
        for combo in itertools.product(*[row['candidates'] for row in order]):
            slots = {voice: list(existing.get(voice, [])) for voice in voices}
            ok = True
            for row, voice in zip(order, combo):
                interval = (Fraction(row['start'], tpq),
                            Fraction(row['end'], tpq))
                if any(a < interval[1] and interval[0] < b
                       for a, b in slots[voice]):
                    ok = False
                    break
                slots[voice].append(interval)
            if ok and (best is None or list(combo) < best):
                best = list(combo)
        return best

    def test_random_placement_matches_brute_force(self):
        # 与实现无关的差分：随机小批量起音，逐个与穷举结果比对。
        import random
        from score_spans.overdub import NoPlacement, _place
        generator = random.Random(20241124)
        voices = ['A', 'B', 'C']
        for trial in range(60):
            occupied = {voice: [] for voice in voices}
            for voice in voices:
                for _ in range(generator.randint(0, 2)):
                    start = Fraction(generator.randint(0, 11), 2)
                    length = Fraction(generator.randint(1, 4), 2)
                    occupied[voice].append((start, start + length))
            messages = []
            for index in range(generator.randint(1, 5)):
                start = generator.randint(0, 23)
                end = start + generator.randint(1, 8)
                candidates = generator.sample(voices, generator.randint(1, 3))
                messages.append((start,
                                 down(start, f'o{index}', 60, candidates)))
                messages.append((end, up(end, f'o{index}')))
            ordered = [message for _tick, message
                       in sorted(messages, key=lambda row: row[0])]
            expected = self._minimal(ordered, occupied=occupied, tpq=2)
            records = {}
            for message in ordered:
                if message['kind'] == 'key_down':
                    records[message['onset_id']] = {
                        'onset_id': message['onset_id'],
                        'candidates': message['candidates'],
                        'start': Fraction(message['tick'], 2), 'end': None}
                else:
                    records[message['onset_id']]['end'] = \
                        Fraction(message['tick'], 2)
            onsets = sorted(records.values(),
                            key=lambda row: (row['start'], row['onset_id']))
            try:
                actual = _place(onsets, occupied)
            except NoPlacement:
                actual = None
            self.assertEqual(actual, expected, (trial, ordered, occupied))

    def test_unknown_candidate_voice_is_usable(self):
        # 候选 ID 不必已在谱上出现：空声部完全可放。
        score = two_measures(transpose=0)
        plan = plan_overdub(score, 'bx', 'm1', 48,
                            [down(0, 'S', 64, ['NewVoice']), up(24, 'S')])
        self.assertEqual(plan['onsets'][0]['voice'], 'NewVoice')

    def test_existing_event_blocks_candidate(self):
        score = two_measures([{'id': 'a1', 'voice': 'A', 'measure': 'm1',
                               'start': [0, 1], 'duration': [1, 1],
                               'pitches': [60]}], transpose=0)
        with self.assertRaises(NoPlacement):
            plan_overdub(score, 'bx', 'm1', 48,
                         [down(0, 'S', 64, ['A']), up(24, 'S')])
        # 空闲区间仍可写入同一声部。
        plan = plan_overdub(score, 'bx', 'm1', 48,
                            [down(96, 'S', 64, ['A']), up(144, 'S')])
        self.assertEqual(plan['onsets'][0]['voice'], 'A')

    def test_adjacent_endpoints_do_not_conflict(self):
        score = two_measures([{'id': 'a1', 'voice': 'A', 'measure': 'm1',
                               'start': [0, 1], 'duration': [1, 1],
                               'pitches': [60]}], transpose=0)
        plan = plan_overdub(score, 'bx', 'm1', 48,
                            [down(48, 'S', 64, ['A']), up(96, 'S')])
        self.assertEqual(plan['onsets'][0]['start'], '1')
        self.assertEqual(plan['onsets'][0]['end'], '2')

    def test_same_pitch_onsets_not_merged(self):
        score = two_measures(transpose=0)
        messages = [down(0, 'S1', 64, ['A']), up(48, 'S1'),
                    down(48, 'S2', 64, ['A']), up(96, 'S2')]
        plan = plan_overdub(score, 'bx', 'm1', 48, messages)
        self.assertEqual(len(plan['onsets']), 2)
        applied = apply_overdub(score, plan)
        result = process(applied)
        self.assertEqual(len(result['spans']), 2)
        self.assertEqual({span['start'] for span in result['spans']},
                         {'0', '1'})

    def test_pedal_hold_keeps_both_onsets(self):
        score = two_measures(transpose=0)
        messages = [pedal(0, True),
                    down(0, 'S1', 64, ['A']), up(24, 'S1'),
                    down(24, 'S2', 64, ['B']), up(48, 'S2'),
                    pedal(96, False)]
        plan = plan_overdub(score, 'bx', 'm1', 48, messages)
        applied = apply_overdub(score, plan)
        spans = sorted(process(applied)['spans'], key=lambda s: s['start'])
        # 两次同音起音都在踏板下保持到同一抬起点，仍各成一条独立链。
        self.assertEqual([(s['start'], s['end']) for s in spans],
                         [('0', '2'), ('1/2', '2')])
        self.assertEqual([s['pitches'] for s in spans], [[64], [64]])


class PedalAndMessageTests(unittest.TestCase):
    def test_release_while_pedal_down_extends_to_pedal_lift(self):
        score = two_measures(transpose=0)
        plan = plan_overdub(score, 'bx', 'm1', 48,
                            [pedal(0, True), down(0, 'S', 60, ['A']),
                             up(12, 'S'), pedal(96, False)])
        self.assertEqual(plan['onsets'][0]['end'], '2')

    def test_lifting_pedal_does_not_end_held_key(self):
        score = two_measures(transpose=0)
        plan = plan_overdub(score, 'bx', 'm1', 48,
                            [down(0, 'S', 60, ['A']), pedal(48, True),
                             pedal(96, False), up(144, 'S')])
        self.assertEqual(plan['onsets'][0]['end'], '3')

    def test_pedal_held_without_lift_is_rejected(self):
        score = two_measures(transpose=0)
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48,
                         [pedal(0, True), down(0, 'S', 60, ['A']),
                          up(48, 'S')])

    def test_never_released_key_is_rejected(self):
        score = two_measures(transpose=0)
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48, [down(0, 'S', 60, ['A'])])

    def test_double_release_and_unknown_release(self):
        score = two_measures(transpose=0)
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48,
                         [down(0, 'S', 60, ['A']), up(24, 'S'),
                          up(48, 'S')])
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48, [up(24, 'ghost')])

    def test_release_not_after_press(self):
        score = two_measures(transpose=0)
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48,
                         [down(48, 'S', 60, ['A']), up(48, 'S')])

    def test_same_tick_release_after_press_is_ordered(self):
        # 同 tick 时数组顺序为准：先按键、后抬键不满足“更晚 tick”，拒绝。
        score = two_measures(transpose=0)
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48,
                         [down(48, 'S', 60, ['A']), up(48, 'S')])

    def test_end_exactly_at_measure_line(self):
        score = two_measures(transpose=0)
        plan = plan_overdub(score, 'bx', 'm1', 48,
                            [down(0, 'S', 60, ['A']), up(96, 'S')])
        self.assertEqual(len(plan['onsets'][0]['events']), 1)
        self.assertEqual(plan['onsets'][0]['end'], '2')
        events = plan['new_events']
        self.assertEqual(events[0]['measure'], 'm1')
        self.assertEqual(events[0]['duration'], [2, 1])
        self.assertNotIn('tie_to', events[0])

    def test_crossing_measure_line_declares_tie(self):
        score = two_measures(transpose=0)
        plan = plan_overdub(score, 'bx', 'm1', 48,
                            [down(0, 'S', 60, ['A']), up(108, 'S')])
        ids = plan['onsets'][0]['events']
        self.assertEqual(len(ids), 2)
        applied = apply_overdub(score, plan)
        result = process(applied)
        self.assertEqual(len(result['spans']), 1)
        self.assertEqual((result['spans'][0]['start'],
                          result['spans'][0]['end']), ('0', '9/4'))

    def test_non_integer_durations(self):
        score = two_measures(transpose=0)
        plan = plan_overdub(score, 'bx', 'm1', 48,
                            [down(16, 'S', 60, ['A']), up(32, 'S')])
        self.assertEqual((plan['onsets'][0]['start'], plan['onsets'][0]['end']),
                         ('1/3', '2/3'))
        applied = apply_overdub(score, plan)
        span = process(applied)['spans'][0]
        self.assertEqual((span['start'], span['end']), ('1/3', '2/3'))

    def test_tick_past_tail_and_tick_order(self):
        score = two_measures(transpose=0)
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48,
                         [down(0, 'S', 60, ['A']), up(193, 'S')])
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48,
                         [down(48, 'S', 60, ['A']), up(96, 'S'),
                          down(0, 'L', 62, ['A'])])

    def test_tick_at_tail_is_allowed_when_interval_fits(self):
        score = two_measures(transpose=0)
        plan = plan_overdub(score, 'bx', 'm1', 48,
                            [down(144, 'S', 60, ['A']), up(192, 'S')])
        self.assertEqual(plan['onsets'][0]['end'], '4')
        # 起点恰在谱尾无法给出正长度区间，拒绝。
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48,
                         [down(192, 'S', 60, ['A']), up(192, 'S')])


class InputValidationTests(unittest.TestCase):
    def test_bad_arguments(self):
        score = two_measures(transpose=0)
        good = [down(0, 'S', 60, ['A']), up(24, 'S')]
        for args in [
                ('x' * 33, 'm1', 48, good),
                ('b1', 'missing', 48, good),
                ('b1', 'm1', 47, good),
                ('b1', 'm1', True, good),
                ('b1', 'm1', '48', good),
                ('b1', 'm1', 480, [])]:
            with self.assertRaises(InvalidOverdub):
                plan_overdub(score, *args)
        for args in [
                ('b1', 'm1', 48, [down(0, 'S', 60, ['A'])]),
                ('b1', 'm1', 48, [down(0, 'S', 60, ['A', 'A']), up(24, 'S')]),
                ('b1', 'm1', 48, [down(0, 'S', 60, []), up(24, 'S')]),
                ('b1', 'm1', 48, [down(0, 'S', 60, ['A', 'B', 'C', 'D',
                                                    'E']), up(24, 'S')]),
                ('b1', 'm1', 48, [down(0, 'S', 128, ['A']), up(24, 'S')]),
                ('b1', 'm1', 48, [down(0, 'S', True, ['A']), up(24, 'S')]),
                ('b1', 'm1', 48, [down(0, 'S', 60, ['A']),
                                  down(0, 'S', 61, ['A']), up(24, 'S')]),
                ('b1', 'm1', 48, [down(0, 'S', 60, ['A']),
                                  {'kind': 'pedal', 'tick': 0, 'on': 1},
                                  up(24, 'S')]),
                ('b1', 'm1', 48, [{'kind': 'key_down', 'tick': 0,
                                   'onset_id': 'S', 'pitch': 60}]),
                ]:
            with self.assertRaises(InvalidOverdub):
                plan_overdub(score, *args)

    def test_batch_bounds(self):
        score = two_measures(transpose=0)
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48,
                         [down(0, f'S{i}', 60, ['A']) for i in range(25)]
                         + [up(24, f'S{i}') for i in range(25)])
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48,
                         [down(0, 'S', 60, ['A', 'B', 'C', 'D']),
                          down(0, 'T', 62, ['E']), up(24, 'S'), up(24, 'T')])
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48,
                         [down(0, f'S{i}', 60, ['A', 'B', 'C', 'D'])
                          for i in range(4)] + [up(24, 'S0')])

    def test_placement_scale_and_ordering(self):
        # 24 条互相错开、候选受限的起音：字典序最小放置必须在毫秒级求得。
        score = two_measures(transpose=0)
        score['measures'] = [{'id': 'm1', 'numerator': 32,
                              'denominator': 4}]
        messages = []
        for index in range(24):
            messages.append(down(index * 4, f'S{index:02d}', 60 + index % 12,
                                 ['A', 'B', 'C', 'D'][:1 + index % 4]))
        for index in range(24):
            messages.append(up(index * 4 + 2, f'S{index:02d}'))
        messages.sort(key=lambda m: (m['tick'], m['onset_id']))
        plan = plan_overdub(score, 'scale', 'm1', 48, messages)
        self.assertEqual(len(plan['onsets']), 24)
        applied = apply_overdub(score, plan)
        spans = process(applied)['spans']
        intervals = sorted((Fraction(s['start']), Fraction(s['end']))
                           for s in spans)
        for left, right in zip(intervals, intervals[1:]):
            self.assertLessEqual(left[1], right[0])

    def test_invalid_old_score_rejected(self):
        overlap = two_measures([
            {'id': 'a', 'voice': 'A', 'measure': 'm1', 'start': [0, 1],
             'duration': [2, 1], 'pitches': [60]},
            {'id': 'b', 'voice': 'A', 'measure': 'm1', 'start': [1, 1],
             'duration': [1, 1], 'pitches': [62]}], transpose=0)
        with self.assertRaises(InvalidOverdub):
            plan_overdub(overlap, 'bx', 'm1', 48,
                         [down(0, 'S', 60, ['Z']), up(24, 'S')])
        untied = two_measures([
            {'id': 'a', 'voice': 'A', 'measure': 'm1', 'start': [0, 1],
             'duration': [1, 1], 'pitches': [60], 'tie_to': 'b'},
            {'id': 'b', 'voice': 'A', 'measure': 'm2', 'start': [1, 1],
             'duration': [1, 1], 'pitches': [60]}], transpose=0)
        with self.assertRaises(InvalidOverdub):
            plan_overdub(untied, 'bx', 'm1', 48,
                         [down(0, 'S', 60, ['Z']), up(24, 'S')])

    def test_unrepresentable_pitch_rejected(self):
        score = two_measures(transpose=100)
        with self.assertRaises(InvalidOverdub):
            plan_overdub(score, 'bx', 'm1', 48,
                         [down(0, 'S', 10, ['A']), up(24, 'S')])

    def test_plan_is_json_and_stable(self):
        score = two_measures(transpose=3)
        plan = plan_overdub(score, 'bx', 'm1', 48,
                            [down(0, 'S', 60, ['A', 'B']), up(96, 'S')])
        blob = json.dumps(plan, sort_keys=True)
        self.assertEqual(json.loads(blob), plan)
        reordered = copy.deepcopy(score)
        reordered['events'] = list(reversed(reordered['events']))
        self.assertEqual(
            plan_overdub(reordered, 'bx', 'm1', 48,
                         [down(0, 'S', 60, ['A', 'B']), up(96, 'S')]), plan)
        first = plan_overdub(score, 'bx', 'm1', 48,
                             [down(0, 'S', 60, ['B', 'A']), up(96, 'S')])
        self.assertEqual(first, plan)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.score = two_measures(transpose=5)
        self.plan = plan_overdub(self.score, 'b1', 'm1', 48,
                                 acceptance_messages(pedal_at=0))
        self.blob = json.dumps(self.plan, sort_keys=True)

    def test_save_reload_apply_with_colleague_edits(self):
        plan = json.loads(self.blob)
        current = two_measures(transpose=5, events=[
            {'id': 'c1', 'voice': 'B', 'measure': 'm2', 'start': [1, 3],
             'duration': [2, 3], 'pitches': [70]},
            {'id': 'c2', 'voice': 'A', 'measure': 'm1', 'start': [0, 1],
             'duration': [1, 1], 'pitches': [50]}])
        applied = apply_overdub(current, plan)
        self.assertEqual(current['events'],
                         [{'id': 'c1', 'voice': 'B', 'measure': 'm2',
                           'start': [1, 3], 'duration': [2, 3],
                           'pitches': [70]},
                          {'id': 'c2', 'voice': 'A', 'measure': 'm1',
                           'start': [0, 1], 'duration': [1, 1],
                           'pitches': [50]}])
        ids = {event['id'] for event in applied['events']}
        for row in plan['new_events']:
            self.assertIn(row['id'], ids)
        preview = {row['onset_id']: row for row in plan['onsets']}
        for row in applied['events']:
            if row['id'] in set(preview['S']['events']):
                self.assertEqual(row['voice'], 'B')
        result = process(applied)
        self.assertIn(('B', '0', '7/3'),
                      {(s['voice'], s['start'], s['end'])
                       for s in result['spans']})

    def test_changes_after_prefix_are_allowed(self):
        plan = json.loads(self.blob)
        current = two_measures(transpose=5)
        current['measures'].append({'id': 'm3', 'numerator': 4,
                                    'denominator': 4})
        current['measures'].append({'id': 'm4', 'numerator': 1,
                                    'denominator': 8})
        current['events'] = [
            {'id': 'late', 'voice': 'Z', 'measure': 'm4', 'start': [0, 1],
             'duration': [1, 2], 'pitches': [40]}]
        applied = apply_overdub(current, plan)
        self.assertIn('late', {event['id'] for event in applied['events']})
        self.assertEqual(len(applied['measures']), 4)

    def test_transpose_change_conflicts(self):
        plan = json.loads(self.blob)
        current = two_measures(transpose=6)
        with self.assertRaises(ApplyConflict):
            apply_overdub(current, plan)
        self.assertEqual(current['events'], [])

    def test_prefix_length_change_conflicts(self):
        plan = json.loads(self.blob)
        current = two_measures(transpose=5)
        current['measures'][0] = {'id': 'm1', 'numerator': 3,
                                  'denominator': 4}
        with self.assertRaises(ApplyConflict):
            apply_overdub(current, plan)

    def test_prefix_id_order_change_conflicts(self):
        plan = json.loads(self.blob)
        current = two_measures(transpose=5)
        current['measures'] = [current['measures'][1],
                               current['measures'][0]]
        with self.assertRaises(ApplyConflict):
            apply_overdub(current, plan)

    def test_edits_inside_prefix_conflict_only_when_planned_interval_or_id(self):
        # 前缀内不碰计划区间与 ID 的修改可以保留。
        plan = json.loads(self.blob)
        current = two_measures(transpose=5, events=[
            {'id': 'keep', 'voice': 'A', 'measure': 'm2', 'start': [1, 1],
             'duration': [1, 1], 'pitches': [52]}])
        applied = apply_overdub(current, plan)
        self.assertIn('keep', {event['id'] for event in applied['events']})
        # 前缀内同声部占住计划区间即整份冲突。
        blocked = two_measures(transpose=5, events=[
            {'id': 'block', 'voice': 'A', 'measure': 'm1', 'start': [2, 1],
             'duration': [1, 3], 'pitches': [52]}])
        with self.assertRaises(ApplyConflict):
            apply_overdub(blocked, plan)
        self.assertEqual(len(blocked['events']), 1)

    def test_clashing_id_in_unrelated_voice_conflicts(self):
        plan = json.loads(self.blob)
        current = two_measures(transpose=5, events=[
            {'id': plan['new_events'][0]['id'], 'voice': 'Z',
             'measure': 'm1', 'start': [0, 1], 'duration': [1, 1],
             'pitches': [40]}])
        with self.assertRaises(ApplyConflict):
            apply_overdub(current, plan)

    def test_occupied_planned_interval_conflicts(self):
        plan = json.loads(self.blob)
        current = two_measures(transpose=5, events=[
            {'id': 'block', 'voice': 'B', 'measure': 'm2', 'start': [0, 1],
             'duration': [1, 3], 'pitches': [50]}])
        with self.assertRaises(ApplyConflict):
            apply_overdub(current, plan)

    def test_reapplying_same_plan_conflicts(self):
        plan = json.loads(self.blob)
        applied = apply_overdub(two_measures(transpose=5), plan)
        with self.assertRaises(ApplyConflict):
            apply_overdub(applied, plan)

    def test_stale_plan_conflicts_when_better_placement_exists(self):
        # S、L 都允许 A/B，存在另一套完整放置。预览后只在 S 已选声部的
        # 踏板尾段内新增占位，键按住的那段不变。
        score = two_measures(transpose=0)
        messages = [pedal(0, True),
                    down(0, 'S', 64, ['A', 'B']), up(24, 'S'),
                    pedal(48, False),
                    down(96, 'L', 64, ['A', 'B']), up(120, 'L')]
        plan = plan_overdub(score, 'b2', 'm1', 48, messages)
        self.assertEqual({row['onset_id']: row['voice']
                          for row in plan['onsets']},
                         {'S': 'A', 'L': 'A'})
        # 只在 S 的踏板尾段 [1/2, 1) 内新增占位；键按住段 [0, 1/2) 不变。
        occupied = two_measures(transpose=0, events=[
            {'id': 'p', 'voice': 'A', 'measure': 'm1', 'start': [1, 2],
             'duration': [1, 2], 'pitches': [55]}])
        replanned = plan_overdub(occupied, 'b2', 'm1', 48, messages)
        self.assertEqual({row['onset_id']: row['voice']
                          for row in replanned['onsets']},
                         {'S': 'B', 'L': 'A'})
        with self.assertRaises(ApplyConflict):
            apply_overdub(occupied, plan)
        self.assertEqual(len(occupied['events']), 1)

    def test_failed_apply_leaves_both_inputs_untouched(self):
        plan = json.loads(self.blob)
        plan_snapshot = copy.deepcopy(plan)
        current = two_measures(transpose=6, events=[
            {'id': 'a', 'voice': 'A', 'measure': 'm1', 'start': [0, 1],
             'duration': [1, 1], 'pitches': [40]}])
        current_snapshot = copy.deepcopy(current)
        with self.assertRaises(ApplyConflict):
            apply_overdub(current, plan)
        self.assertEqual(current, current_snapshot)
        self.assertEqual(plan, plan_snapshot)

    def test_apply_rejects_invalid_plan_shape(self):
        base = json.loads(self.blob)
        mutations = [
            lambda p: p.pop('prefix'),
            lambda p: p.update(format='other/1'),
            lambda p: p['new_events'][0].update(pitches=[200]),
            lambda p: p['new_events'][0].update(duration=[0, 1]),
            lambda p: p['onsets'][0].update(end='0'),
            lambda p: p['onsets'][0].update(voice='Q'),
            lambda p: p['prefix'][-1].update(length='3'),
            lambda p: p['new_events'][0].update(
                tie_to=p['new_events'][-1]['id']),
            lambda p: p['new_events'][0].update(start=[5, 1]),
            lambda p: p['onsets'][0].update(events=[]),
        ]
        for mutate in mutations:
            plan = copy.deepcopy(base)
            mutate(plan)
            with self.assertRaises((InvalidOverdub, ApplyConflict)):
                apply_overdub(two_measures(transpose=5), plan)

    def test_apply_rejects_invalid_current_score(self):
        plan = json.loads(self.blob)
        overlap = two_measures(transpose=5, events=[
            {'id': 'a', 'voice': 'Z', 'measure': 'm1', 'start': [0, 1],
             'duration': [2, 1], 'pitches': [40]},
            {'id': 'b', 'voice': 'Z', 'measure': 'm1', 'start': [1, 1],
             'duration': [1, 1], 'pitches': [42]}])
        with self.assertRaises(ApplyConflict):
            apply_overdub(overlap, plan)


class RegressionTests(unittest.TestCase):
    def test_existing_spans_unchanged_and_each_onset_is_one_chain(self):
        original = two_measures(transpose=7, events=[
            {'id': 'x1', 'voice': 'V', 'measure': 'm1', 'start': [0, 1],
             'duration': [2, 1], 'pitches': [60], 'tie_to': 'x2'},
            {'id': 'x2', 'voice': 'V', 'measure': 'm2', 'start': [0, 1],
             'duration': [1, 1], 'pitches': [60]}])
        before = process(original)
        messages = [down(0, 'P', 72, ['B']), up(24, 'P'),
                    down(48, 'Q', 74, ['A']),
                    down(96, 'R', 76, ['B']),
                    up(120, 'Q'), up(144, 'R')]
        snapshot = copy.deepcopy(original)
        plan = plan_overdub(original, 'bg', 'm1', 48, messages)
        applied = apply_overdub(original, plan)
        after = process(applied)
        self.assertEqual(original, snapshot)
        # 原发音跨度逐条保留（时值、音高、声部都不变）。
        self.assertTrue(all(span in after['spans'] for span in before['spans']))
        preview = {row['onset_id']: row for row in plan['onsets']}
        for onset in preview.values():
            chains = [span for span in after['spans']
                      if set(span['events']) == set(onset['events'])]
            self.assertEqual(len(chains), 1, onset['onset_id'])
            chain = chains[0]
            self.assertEqual((chain['start'], chain['end']),
                             (onset['start'], onset['end']))
            self.assertEqual(chain['voice'], onset['voice'])
            self.assertEqual(chain['pitches'], [onset['pitch']])
            self.assertEqual(len(chain['events']), len(onset['events']))

    def test_plan_does_not_mutate_inputs(self):
        score = two_measures(transpose=3)
        snapshot = copy.deepcopy(score)
        messages = acceptance_messages()
        plan = plan_overdub(score, 'b1', 'm1', 48, messages)
        self.assertEqual(score, snapshot)
        plan_snapshot = copy.deepcopy(plan)
        apply_overdub(score, plan)
        self.assertEqual(plan, plan_snapshot)
        self.assertEqual(score, snapshot)
        self.assertEqual(messages, acceptance_messages())


class ErrorCodeTests(unittest.TestCase):
    def test_error_codes_are_stable(self):
        score = two_measures(transpose=0)
        with self.assertRaises(InvalidOverdub) as caught:
            plan_overdub(score, 'b', 'nope', 48,
                         [down(0, 'S', 60, ['A']), up(24, 'S')])
        self.assertEqual(caught.exception.code, 'invalid_input')
        # 不重叠的谱可成功；随后改移调制造应用冲突。
        plan = plan_overdub(score, 'b', 'm1', 48, [
            down(0, 'S', 60, ['A']), up(24, 'S')])
        blocked = two_measures([{'id': 'a', 'voice': 'A', 'measure': 'm1',
                                 'start': [0, 1], 'duration': [2, 1],
                                 'pitches': [60]}], transpose=0)
        with self.assertRaises(NoPlacement) as caught:
            plan_overdub(blocked, 'b', 'm1', 48,
                         [down(0, 'S', 5, ['A']), up(24, 'S')])
        self.assertEqual(caught.exception.code, 'no_placement')
        with self.assertRaises(ApplyConflict) as caught:
            apply_overdub(two_measures(transpose=1), plan)
        self.assertEqual(caught.exception.code, 'apply_conflict')


if __name__ == '__main__':
    unittest.main()
