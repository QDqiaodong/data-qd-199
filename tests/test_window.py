import copy
import json
import time
import unittest
from fractions import Fraction
from pathlib import Path

from score_spans.service import process, process_window


def full_measure_note(nid, voice, measure, length, pitch=60, tie_to=None):
    event = {'id': nid, 'voice': voice, 'measure': measure,
             'start': [0, 1], 'duration': [length.numerator, length.denominator],
             'pitches': [pitch]}
    if tie_to is not None:
        event['tie_to'] = tie_to
    return event


class WindowBillionTests(unittest.TestCase):
    """验收 1：十亿次反复的压缩查询。"""

    def setUp(self):
        self.N = 1_000_000_000
        self.score = {
            'measures': [
                {'id': 'A', 'numerator': 3, 'denominator': 4},
                {'id': 'B', 'numerator': 2, 'denominator': 4},
                {'id': 'C', 'numerator': 1, 'denominator': 4},
            ],
            'events': [
                full_measure_note('a', 'v', 'A', Fraction(3), tie_to='b'),
                full_measure_note('b', 'v', 'B', Fraction(2), tie_to='a'),
                full_measure_note('c', 'v', 'C', Fraction(1)),
            ],
            'transpose': 0,
        }
        self.sections = [
            {'from_measure': 'A', 'through_measure': 'B', 'repeat': self.N},
            {'from_measure': 'C', 'through_measure': 'C', 'repeat': 1},
        ]
        self.last_b = 2 * self.N - 1

    def test_last_b_with_c_tail(self):
        t0 = time.perf_counter()
        r = process_window(self.score, self.sections, self.last_b,
                           self.last_b + 1)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(r['measures']), 1)
        self.assertEqual(r['measures'][0], {
            'ordinal': self.last_b, 'measure': 'B',
            'start': str(5 * self.N - 2), 'length': '2'})
        self.assertEqual(len(r['spans']), 1)
        span = r['spans'][0]
        self.assertEqual(span['start'], str(5 * self.N - 2))
        self.assertEqual(span['end'], str(5 * self.N))
        self.assertEqual(span['full_start'], '0')
        self.assertEqual(span['full_end'], str(5 * self.N))
        self.assertEqual(span['first_event'], [0, 'a'])
        self.assertEqual(span['last_event'], [self.last_b, 'b'])
        self.assertEqual(span['member_count'], 2 * self.N)
        self.assertEqual(span['visible_events'], [[self.last_b, 'b']])
        self.assertEqual(span['pitches'], [60])
        self.assertEqual(r['diagnostics'], [{
            'kind': 'invalid_tie', 'ordinal': self.last_b,
            'from': 'b', 'to': 'a'}])
        # JSON 可序列化
        json.dumps(r)

    def test_tail_switched_to_a(self):
        sections = [
            {'from_measure': 'A', 'through_measure': 'B', 'repeat': self.N},
            {'from_measure': 'A', 'through_measure': 'A', 'repeat': 1},
        ]
        t0 = time.perf_counter()
        r = process_window(self.score, sections, self.last_b,
                           self.last_b + 1)
        self.assertLess(time.perf_counter() - t0, 5.0)
        span = r['spans'][0]
        self.assertEqual(span['start'], str(5 * self.N - 2))
        self.assertEqual(span['end'], str(5 * self.N))
        self.assertEqual(span['full_end'], str(5 * self.N + 3))
        self.assertEqual(span['last_event'], [2 * self.N, 'a'])
        self.assertEqual(span['member_count'], 2 * self.N + 1)
        self.assertEqual(span['visible_events'], [[self.last_b, 'b']])
        self.assertEqual(r['diagnostics'], [])

    def test_middle_b_head_and_tail_far_away(self):
        # 第 4 次 B：ordinal 7（轮次 r=3：A6,B7）
        r = process_window(self.score, self.sections, 7, 8)
        span = r['spans'][0]
        self.assertEqual(span['first_event'], [0, 'a'])
        self.assertEqual(span['last_event'], [self.last_b, 'b'])
        self.assertEqual(span['member_count'], 2 * self.N)
        self.assertEqual(span['full_start'], '0')
        self.assertEqual(span['full_end'], str(5 * self.N))
        self.assertEqual(span['start'], '18')
        self.assertEqual(span['end'], '20')
        self.assertEqual(span['visible_events'], [[7, 'b']])
        self.assertEqual(r['diagnostics'], [])

    def test_first_a_window(self):
        r = process_window(self.score, self.sections, 0, 2)
        self.assertEqual([m['ordinal'] for m in r['measures']], [0, 1])
        span = r['spans'][0]
        self.assertEqual(span['first_event'], [0, 'a'])
        self.assertEqual(span['last_event'], [self.last_b, 'b'])
        self.assertEqual(span['member_count'], 2 * self.N)
        self.assertEqual(span['visible_events'], [[0, 'a'], [1, 'b']])
        self.assertEqual(span['start'], '0')
        self.assertEqual(span['end'], '5')


class WindowChainTests(unittest.TestCase):
    """验收 2：小编排的链截断、边界与分数延音。"""

    def _score(self):
        return {
            'measures': [
                {'id': 'm1', 'numerator': 4, 'denominator': 4},
                {'id': 'm2', 'numerator': 4, 'denominator': 4},
                {'id': 'm3', 'numerator': 4, 'denominator': 4},
            ],
            'events': [
                # m1: 整小节音，跨小节延到 m2 的 x
                {'id': 'n1', 'voice': 'v', 'measure': 'm1',
                 'start': [0, 1], 'duration': [4, 1], 'pitches': [60],
                 'tie_to': 'x'},
                # m2: 同高但起点不匹配（休止占位），链在此截断
                {'id': 'r', 'voice': 'v', 'measure': 'm2',
                 'start': [0, 1], 'duration': [1, 1], 'pitches': []},
                {'id': 'x', 'voice': 'v', 'measure': 'm2',
                 'start': [1, 1], 'duration': [1, 1], 'pitches': [60]},
                # m3: 恢复，同音高重新起音
                {'id': 'n3', 'voice': 'v', 'measure': 'm3',
                 'start': [0, 1], 'duration': [2, 1], 'pitches': [60]},
            ],
            'transpose': 0,
        }

    def test_mismatching_measure_truncates_chain(self):
        score = self._score()
        sections = [{'from_measure': 'm1', 'through_measure': 'm3',
                     'repeat': 1}]
        r = process_window(score, sections, 0, 3)
        spans = r['spans']
        # n1 跨小节目标 x 起点非零 -> 断链；n1 独立成链
        firsts = {tuple(s['first_event']): s for s in spans}
        self.assertIn((0, 'n1'), firsts)
        self.assertEqual(firsts[(0, 'n1')]['last_event'], [0, 'n1'])
        self.assertEqual(firsts[(0, 'n1')]['member_count'], 1)
        # x 是新起音
        self.assertIn((1, 'x'), firsts)
        self.assertEqual(firsts[(1, 'x')]['member_count'], 1)
        # n3 也是新起音
        self.assertIn((2, 'n3'), firsts)
        # n1 的延音未兑现诊断
        self.assertIn({'kind': 'invalid_tie', 'ordinal': 0,
                       'from': 'n1', 'to': 'x'}, r['diagnostics'])

    def test_section_and_repeat_boundaries(self):
        # 段 1 为 [m1] 两轮：第 0 轮 m1 后仍是 m1（同段下一轮），其跨小节
        # 声明指向 m2 的 b，下一次小节里没有 b -> 失败；
        # 第 1 轮 m1（ordinal 1）后是段 2 的 m2，b 起点为 0 -> 跨段成立。
        score = {
            'measures': [
                {'id': 'm1', 'numerator': 2, 'denominator': 4},
                {'id': 'm2', 'numerator': 2, 'denominator': 4},
            ],
            'events': [
                full_measure_note('a', 'v', 'm1', Fraction(2), tie_to='b'),
                full_measure_note('b', 'v', 'm2', Fraction(2)),
            ],
            'transpose': 0,
        }
        sections = [
            {'from_measure': 'm1', 'through_measure': 'm1', 'repeat': 2},
            {'from_measure': 'm2', 'through_measure': 'm2', 'repeat': 1},
        ]
        # 第 0 次 m1 的 a：下一次仍是 m1（同段内下一轮），其中没有 b -> 失败
        r0 = process_window(score, sections, 0, 1)
        self.assertEqual(r0['spans'][0]['last_event'], [0, 'a'])
        self.assertIn({'kind': 'invalid_tie', 'ordinal': 0,
                       'from': 'a', 'to': 'b'}, r0['diagnostics'])
        # 第 1 次 m1（ordinal 1）：下一次是段 2 的 m2，b 存在且起点 0 -> 成立
        r1 = process_window(score, sections, 1, 3)
        span = r1['spans'][0]
        self.assertEqual(span['first_event'], [1, 'a'])
        self.assertEqual(span['last_event'], [2, 'b'])
        self.assertEqual(span['member_count'], 2)
        self.assertEqual(span['full_start'], '2')
        self.assertEqual(span['full_end'], '6')

    def test_exact_fractional_intra_measure_tie(self):
        score = {
            'measures': [{'id': 'm', 'numerator': 4, 'denominator': 4}],
            'events': [
                {'id': 'p', 'voice': 'v', 'measure': 'm',
                 'start': [1, 3], 'duration': [2, 3], 'pitches': [60],
                 'tie_to': 'q'},
                {'id': 'q', 'voice': 'v', 'measure': 'm',
                 'start': [1, 1], 'duration': [1, 3], 'pitches': [60]},
            ],
            'transpose': 0,
        }
        r = process_window(
            score,
            [{'from_measure': 'm', 'through_measure': 'm', 'repeat': 1}],
            0, 1)
        span = r['spans'][0]
        self.assertEqual(span['member_count'], 2)
        self.assertEqual(span['first_event'], [0, 'p'])
        self.assertEqual(span['last_event'], [0, 'q'])
        self.assertEqual(span['full_start'], '1/3')
        self.assertEqual(span['full_end'], '4/3')
        # 链本身无 invalid_tie；未铺满小节产生的是覆盖 gap 诊断
        self.assertNotIn('invalid_tie',
                         [d['kind'] for d in r['diagnostics']])

    def test_intra_measure_gap_breaks_tie(self):
        score = {
            'measures': [{'id': 'm', 'numerator': 4, 'denominator': 4}],
            'events': [
                {'id': 'p', 'voice': 'v', 'measure': 'm',
                 'start': [0, 1], 'duration': [1, 1], 'pitches': [60],
                 'tie_to': 'q'},
                {'id': 'q', 'voice': 'v', 'measure': 'm',
                 'start': [2, 1], 'duration': [1, 1], 'pitches': [60]},
            ],
            'transpose': 0,
        }
        r = process_window(
            score,
            [{'from_measure': 'm', 'through_measure': 'm', 'repeat': 1}],
            0, 1)
        self.assertEqual(len(r['spans']), 2)
        self.assertIn({'kind': 'invalid_tie', 'ordinal': 0,
                       'from': 'p', 'to': 'q'}, r['diagnostics'])

    def test_cross_measure_target_nonzero_start_fails(self):
        score = {
            'measures': [
                {'id': 'm1', 'numerator': 2, 'denominator': 4},
                {'id': 'm2', 'numerator': 2, 'denominator': 4},
            ],
            'events': [
                full_measure_note('a', 'v', 'm1', Fraction(2), tie_to='b'),
                {'id': 'b', 'voice': 'v', 'measure': 'm2',
                 'start': [1, 2], 'duration': [1, 2], 'pitches': [60]},
            ],
            'transpose': 0,
        }
        r = process_window(
            score,
            [{'from_measure': 'm1', 'through_measure': 'm2', 'repeat': 1}],
            0, 2)
        self.assertEqual(r['spans'][0]['last_event'], [0, 'a'])
        self.assertIn({'kind': 'invalid_tie', 'ordinal': 0,
                       'from': 'a', 'to': 'b'}, r['diagnostics'])

    def test_tie_succeeds_in_one_occurrence_and_fails_in_another(self):
        # 段 [m1,m2] 两轮：m1 的 a 指向 m2 的 b（跨小节成立）；
        # 第 2 轮 m2 后无小节 -> b 自身的跨小节声明失败
        score = {
            'measures': [
                {'id': 'm1', 'numerator': 1, 'denominator': 4},
                {'id': 'm2', 'numerator': 1, 'denominator': 4},
            ],
            'events': [
                full_measure_note('a', 'v', 'm1', Fraction(1), tie_to='b'),
                full_measure_note('b', 'v', 'm2', Fraction(1), tie_to='a'),
            ],
            'transpose': 0,
        }
        sections = [{'from_measure': 'm1', 'through_measure': 'm2',
                     'repeat': 2}]
        r = process_window(score, sections, 0, 4)
        # 链：a0-b1-a2-b3，b3 的 tie 在末尾失败
        span = r['spans'][0]
        self.assertEqual(span['first_event'], [0, 'a'])
        self.assertEqual(span['last_event'], [3, 'b'])
        self.assertEqual(span['member_count'], 4)
        self.assertEqual([d for d in r['diagnostics']
                          if d['kind'] == 'invalid_tie'],
                         [{'kind': 'invalid_tie', 'ordinal': 3,
                           'from': 'b', 'to': 'a'}])

    def test_overlapping_chords_not_merged(self):
        # 同时值同音高的两个独立事件不会被隐式合并
        score = {
            'measures': [{'id': 'm', 'numerator': 4, 'denominator': 4}],
            'events': [
                {'id': 'x', 'voice': 'v', 'measure': 'm',
                 'start': [0, 1], 'duration': [1, 1], 'pitches': [60]},
                {'id': 'y', 'voice': 'v', 'measure': 'm',
                 'start': [0, 1], 'duration': [1, 1], 'pitches': [60]},
            ],
            'transpose': 0,
        }
        r = process_window(
            score,
            [{'from_measure': 'm', 'through_measure': 'm', 'repeat': 1}],
            0, 1)
        self.assertEqual(len(r['spans']), 2)

    def test_window_clip_keeps_full_chain_identity(self):
        # 窗口只覆盖长链中部：裁剪区间与完整范围不同，成员不重新起音
        score = {
            'measures': [{'id': 'm', 'numerator': 1, 'denominator': 4}],
            'events': [
                full_measure_note('a', 'v', 'm', Fraction(1), tie_to='a'),
            ],
            'transpose': 0,
        }
        sections = [{'from_measure': 'm', 'through_measure': 'm',
                     'repeat': 10}]
        r = process_window(score, sections, 4, 6)
        span = r['spans'][0]
        self.assertEqual(span['first_event'], [0, 'a'])
        self.assertEqual(span['last_event'], [9, 'a'])
        self.assertEqual(span['member_count'], 10)
        self.assertEqual(span['full_start'], '0')
        self.assertEqual(span['full_end'], '10')
        self.assertEqual(span['start'], '4')
        self.assertEqual(span['end'], '6')
        self.assertEqual(span['visible_events'], [[4, 'a'], [5, 'a']])
        self.assertEqual(r['diagnostics'], [])


class WindowDiagnosticTests(unittest.TestCase):
    """验收 3：缺拍、重叠、越界、休止与重复出现的 ordinal。"""

    def test_diagnostics_carry_ordinal_per_occurrence(self):
        score = {
            'measures': [{'id': 'm', 'numerator': 4, 'denominator': 4}],
            'events': [
                {'id': 'e', 'voice': 'v', 'measure': 'm',
                 'start': [1, 1], 'duration': [1, 1], 'pitches': [60]},
            ],
            'transpose': 0,
        }
        sections = [{'from_measure': 'm', 'through_measure': 'm',
                     'repeat': 3}]
        r = process_window(score, sections, 0, 3)
        gaps = [d for d in r['diagnostics'] if d['kind'] == 'gap']
        # 每次出现有两段缺拍（0..1 与 2..4），三次出现共 6 条，各带 ordinal
        self.assertEqual(sorted(d['ordinal'] for d in gaps),
                         [0, 0, 1, 1, 2, 2])
        ordinals = {}
        for d in gaps:
            self.assertEqual(d['measure'], 'm')
            self.assertEqual(d['voice'], 'v')
            ordinals.setdefault(d['ordinal'], []).append(
                (d['start'], d['end']))
        self.assertEqual(ordinals[0], [('0', '1'), ('2', '4')])

    def test_overrun_event_audited_but_silent(self):
        score = {
            'measures': [{'id': 'm', 'numerator': 2, 'denominator': 4}],
            'events': [
                {'id': 'e', 'voice': 'v', 'measure': 'm',
                 'start': [1, 1], 'duration': [2, 1], 'pitches': [60],
                 'tie_to': 'e'},
                {'id': 'ok', 'voice': 'w', 'measure': 'm',
                 'start': [0, 1], 'duration': [1, 1], 'pitches': [70]},
            ],
            'transpose': 0,
        }
        sections = [{'from_measure': 'm', 'through_measure': 'm',
                     'repeat': 1}]
        r = process_window(score, sections, 0, 1)
        kinds = {d['kind'] for d in r['diagnostics']}
        self.assertIn('overrun', kinds)
        self.assertIn('invalid_tie', kinds)
        # 越界事件不产生跨度；另一声部的合法跨度仍交付
        self.assertEqual([s['voice'] for s in r['spans']], ['w'])

    def test_rest_is_not_a_node_and_tie_fails(self):
        score = {
            'measures': [
                {'id': 'm1', 'numerator': 1, 'denominator': 4},
                {'id': 'm2', 'numerator': 1, 'denominator': 4},
            ],
            'events': [
                {'id': 'r', 'voice': 'v', 'measure': 'm1',
                 'start': [0, 1], 'duration': [1, 1], 'pitches': [],
                 'tie_to': 'n'},
                {'id': 'n', 'voice': 'v', 'measure': 'm2',
                 'start': [0, 1], 'duration': [1, 1], 'pitches': [60]},
            ],
            'transpose': 0,
        }
        r = process_window(
            score,
            [{'from_measure': 'm1', 'through_measure': 'm2', 'repeat': 1}],
            0, 2)
        self.assertEqual(r['spans'][0]['first_event'], [1, 'n'])
        self.assertIn({'kind': 'invalid_tie', 'ordinal': 0,
                       'from': 'r', 'to': 'n'}, r['diagnostics'])

    def test_voice_without_events_still_audited(self):
        score = {
            'measures': [
                {'id': 'm1', 'numerator': 1, 'denominator': 4},
                {'id': 'm2', 'numerator': 1, 'denominator': 4},
            ],
            'events': [
                {'id': 'n', 'voice': 'v', 'measure': 'm1',
                 'start': [0, 1], 'duration': [1, 1], 'pitches': [60]},
            ],
            'transpose': 0,
        }
        r = process_window(
            score,
            [{'from_measure': 'm1', 'through_measure': 'm2', 'repeat': 1}],
            1, 2)
        self.assertIn({'kind': 'gap', 'voice': 'v', 'measure': 'm2',
                       'ordinal': 1, 'start': '0', 'end': '1'},
                      r['diagnostics'])

    def test_tie_check_ignores_window_right_edge(self):
        # 窗口最后一个小节的跨小节声明，其目标在窗口外仍须判定成立
        score = {
            'measures': [
                {'id': 'm1', 'numerator': 1, 'denominator': 4},
                {'id': 'm2', 'numerator': 1, 'denominator': 4},
            ],
            'events': [
                full_measure_note('a', 'v', 'm1', Fraction(1), tie_to='b'),
                full_measure_note('b', 'v', 'm2', Fraction(1)),
            ],
            'transpose': 0,
        }
        sections = [{'from_measure': 'm1', 'through_measure': 'm2',
                     'repeat': 1}]
        r = process_window(score, sections, 0, 1)
        self.assertEqual(r['diagnostics'], [])
        span = r['spans'][0]
        self.assertEqual(span['last_event'], [1, 'b'])
        self.assertEqual(span['full_end'], '2')
        self.assertEqual(span['end'], '1')

    def test_event_order_shuffle_does_not_change_output(self):
        score = {
            'measures': [
                {'id': 'm1', 'numerator': 2, 'denominator': 4},
                {'id': 'm2', 'numerator': 2, 'denominator': 4},
            ],
            'events': [
                full_measure_note('a', 'v', 'm1', Fraction(2), tie_to='b'),
                full_measure_note('b', 'v', 'm2', Fraction(2)),
                {'id': 'g', 'voice': 'w', 'measure': 'm1',
                 'start': [0, 1], 'duration': [1, 2], 'pitches': [50]},
            ],
            'transpose': 0,
        }
        sections = [{'from_measure': 'm1', 'through_measure': 'm2',
                     'repeat': 2}]
        r1 = process_window(score, sections, 0, 4)
        shuffled = copy.deepcopy(score)
        shuffled['events'] = list(reversed(shuffled['events']))
        r2 = process_window(shuffled, sections, 0, 4)
        self.assertEqual(json.dumps(r1, sort_keys=True),
                         json.dumps(r2, sort_keys=True))


class WindowValidationTests(unittest.TestCase):
    """非法参数与旧接口回归。"""

    def setUp(self):
        self.score = {
            'measures': [
                {'id': 'm1', 'numerator': 2, 'denominator': 4},
                {'id': 'm2', 'numerator': 2, 'denominator': 4},
            ],
            'events': [
                full_measure_note('a', 'v', 'm1', Fraction(2), tie_to='b'),
                full_measure_note('b', 'v', 'm2', Fraction(2)),
            ],
            'transpose': 0,
        }

    def test_bad_sections_shape(self):
        good = [{'from_measure': 'm1', 'through_measure': 'm2',
                 'repeat': 1}]
        with self.assertRaises(ValueError):
            process_window(self.score, [], 0, 1)
        with self.assertRaises(ValueError):
            process_window(self.score, good * 101, 0, 1)
        with self.assertRaises(ValueError):
            process_window(
                self.score,
                [{'from_measure': 'm2', 'through_measure': 'm1',
                  'repeat': 1}], 0, 1)
        with self.assertRaises(ValueError):
            process_window(
                self.score,
                [{'from_measure': 'nope', 'through_measure': 'm1',
                  'repeat': 1}], 0, 1)
        with self.assertRaises(ValueError):
            process_window(
                self.score,
                [{'from_measure': 'm1', 'through_measure': 'm2',
                  'repeat': 0}], 0, 1)
        with self.assertRaises(ValueError):
            process_window(
                self.score,
                [{'from_measure': 'm1', 'through_measure': 'm2',
                  'repeat': 10**9 + 1}], 0, 1)

    def test_bad_window(self):
        sections = [{'from_measure': 'm1', 'through_measure': 'm2',
                     'repeat': 1}]
        for first, stop in ((-1, 1), (0, 0), (0, 3), (2, 3), (0, 9),
                            (1, 1)):
            with self.assertRaises(ValueError):
                process_window(self.score, sections, first, stop)
        # 窗口最多 8 个小节
        big = [{'from_measure': 'm1', 'through_measure': 'm2',
                'repeat': 5}]
        with self.assertRaises(ValueError):
            process_window(self.score, big, 0, 9)
        # 布尔值不算整数
        with self.assertRaises(ValueError):
            process_window(self.score, sections, True, 1)
        with self.assertRaises(ValueError):
            process_window(self.score, sections, 0, True)

    def test_invalid_score_still_rejected(self):
        bad = copy.deepcopy(self.score)
        bad['transpose'] = 200
        with self.assertRaises(ValueError):
            process_window(bad,
                           [{'from_measure': 'm1',
                             'through_measure': 'm2', 'repeat': 1}], 0, 1)
        bad2 = copy.deepcopy(self.score)
        bad2['events'][0]['tie_to'] = 'ghost'
        with self.assertRaises(ValueError):
            process_window(bad2,
                           [{'from_measure': 'm1',
                             'through_measure': 'm2', 'repeat': 1}], 0, 1)

    def test_section_field_shape(self):
        good = [{'from_measure': 'm1', 'through_measure': 'm2',
                 'repeat': 1}]
        with self.assertRaises(ValueError):
            process_window(self.score, 'not-a-list', 0, 1)
        with self.assertRaises(ValueError):
            process_window(self.score, [{}], 0, 1)
        with self.assertRaises(ValueError):
            process_window(
                self.score,
                [{'from_measure': 'm1', 'through_measure': 'm2',
                  'repeat': 1, 'extra': 1}], 0, 1)
        with self.assertRaises(ValueError):
            process_window(
                self.score,
                [{'from_measure': 7, 'through_measure': 'm2',
                  'repeat': 1}], 0, 1)
        with self.assertRaises(ValueError):
            process_window(self.score, good, 0, 1.0)

    def test_outside_window_failures_are_not_reported(self):
        # 两段：窗口只看第一段，第二段末尾的断链不得出现在诊断里
        score = {
            'measures': [{'id': 'm', 'numerator': 1, 'denominator': 4}],
            'events': [
                full_measure_note('a', 'v', 'm', Fraction(1), tie_to='a'),
            ],
            'transpose': 0,
        }
        sections = [
            {'from_measure': 'm', 'through_measure': 'm', 'repeat': 2},
            {'from_measure': 'm', 'through_measure': 'm', 'repeat': 1},
        ]
        r = process_window(score, sections, 0, 1)
        # ordinal 0 的 a 后面还有 ordinal 1，延音成立；末尾 ordinal 2 在窗外
        self.assertEqual(r['diagnostics'], [])
        self.assertEqual(r['spans'][0]['last_event'], [2, 'a'])

    def test_span_sort_order(self):
        score = {
            'measures': [{'id': 'm', 'numerator': 4, 'denominator': 4}],
            'events': [
                {'id': 'late', 'voice': 'v', 'measure': 'm',
                 'start': [2, 1], 'duration': [1, 1], 'pitches': [62]},
                {'id': 'early', 'voice': 'v', 'measure': 'm',
                 'start': [0, 1], 'duration': [1, 1], 'pitches': [60]},
            ],
            'transpose': 0,
        }
        r = process_window(
            score,
            [{'from_measure': 'm', 'through_measure': 'm', 'repeat': 1}],
            0, 1)
        self.assertEqual([s['first_event'][1] for s in r['spans']],
                         ['early', 'late'])

    def test_legacy_process_unchanged(self):
        data = json.loads(Path('fixtures/input.json').read_text())
        r = process(data)
        self.assertEqual(r['spans'], [{
            'events': ['n1', 'n2'], 'voice': 'v1',
            'pitches': [62], 'start': '0', 'end': '6'}])
        self.assertEqual(r['diagnostics'], [])
        self.assertEqual(r['summary'],
                         {'events': 3, 'sounding_spans': 1,
                          'diagnostics': 0})
        self.assertEqual(len(r['measures']), 2)


if __name__ == '__main__':
    unittest.main()
