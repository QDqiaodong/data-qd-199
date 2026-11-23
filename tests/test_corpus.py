import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from score_spans import corpus
from score_spans.corpus import (
    CorpusError, InvalidChange, ReleaseConflict, RequestConflict,
    UnknownRelease, UnknownScore, publish_corpus, read_corpus)
from score_spans.service import process


def upsert(score_id, family_id, preferred_split, score):
    return {'op': 'upsert', 'score_id': score_id, 'family_id': family_id,
            'preferred_split': preferred_split, 'score': score}


def withdraw(score_id):
    return {'op': 'withdraw', 'score_id': score_id}


def ids(rows):
    return {row['score_id'] for row in rows}


def score_a():
    """两个 4/4 小节：三连音起点的和弦延音链 + 另一声部（非整数时值）。"""
    return {
        'measures': [{'id': 'm1', 'numerator': 4, 'denominator': 4},
                     {'id': 'm2', 'numerator': 4, 'denominator': 4}],
        'events': [
            {'id': 'a1', 'voice': 'v1', 'measure': 'm1',
             'start': [1, 3], 'duration': [4, 3], 'pitches': [60, 64],
             'tie_to': 'a2'},
            {'id': 'a2', 'voice': 'v1', 'measure': 'm1',
             'start': [5, 3], 'duration': [1, 3], 'pitches': [60, 64]},
            {'id': 'a3', 'voice': 'v2', 'measure': 'm2',
             'start': [0, 1], 'duration': [2, 1], 'pitches': [55]}],
        'transpose': 0}


def score_a_corrected():
    """A 的更正版：末事件音高改变，内容指纹随之改变。"""
    score = score_a()
    score['events'][2]['pitches'] = [56]
    return score


def score_c():
    """与 A 仅整体移调、改名与等效延音写法（链改写为单事件）的差异。"""
    return {
        'measures': [{'id': 'x1', 'numerator': 4, 'denominator': 4},
                     {'id': 'x2', 'numerator': 4, 'denominator': 4}],
        'events': [
            {'id': 'c1', 'voice': 'w1', 'measure': 'x1',
             'start': [1, 3], 'duration': [5, 3], 'pitches': [58, 62]},
            {'id': 'c2', 'voice': 'w2', 'measure': 'x2',
             'start': [0, 1], 'duration': [2, 1], 'pitches': [53]}],
        'transpose': 2}


def score_b():
    return {'measures': [{'id': 'b1', 'numerator': 4, 'denominator': 4}],
            'events': [{'id': 'be', 'voice': 'vb', 'measure': 'b1',
                        'start': [0, 1], 'duration': [1, 1],
                        'pitches': [60]}],
            'transpose': 0}


def score_d():
    return {'measures': [{'id': 'd1', 'numerator': 4, 'denominator': 4}],
            'events': [{'id': 'de', 'voice': 'vd', 'measure': 'd1',
                        'start': [0, 1], 'duration': [4, 1],
                        'pitches': [70]}],
            'transpose': 0}


def score_p():
    """与 D 同指纹：整体移调 + 改名。"""
    return {'measures': [{'id': 'p1', 'numerator': 4, 'denominator': 4}],
            'events': [{'id': 'pe', 'voice': 'vp', 'measure': 'p1',
                        'start': [0, 1], 'duration': [4, 1],
                        'pitches': [60]}],
            'transpose': 10}


def score_e():
    return {'measures': [{'id': 'e1', 'numerator': 2, 'denominator': 4}],
            'events': [{'id': 'ee', 'voice': 've', 'measure': 'e1',
                        'start': [0, 1], 'duration': [2, 1],
                        'pitches': [50]}],
            'transpose': 0}


def score_z():
    """与 A 同时长、同音高集合，但节奏不同（跨度终点不同）。"""
    score = score_a()
    score['events'] = [
        {'id': 'z1', 'voice': 'v1', 'measure': 'm1',
         'start': [1, 3], 'duration': [4, 3], 'pitches': [60, 64]},
        {'id': 'z2', 'voice': 'v2', 'measure': 'm2',
         'start': [0, 1], 'duration': [2, 1], 'pitches': [55]}]
    return score


def score_w():
    """与 C 同跨度，但多一个空白小节（谱尾留白，时长不同）。"""
    score = score_c()
    score['measures'] = score['measures'] + [
        {'id': 'x3', 'numerator': 4, 'denominator': 4}]
    return score


def score_r():
    """两个声部各一条相同跨度：重复跨度数目为 2。"""
    return {'measures': [{'id': 'r1', 'numerator': 4, 'denominator': 4}],
            'events': [
                {'id': 'ra', 'voice': 'v1', 'measure': 'r1',
                 'start': [0, 1], 'duration': [1, 1], 'pitches': [60]},
                {'id': 'rb', 'voice': 'v2', 'measure': 'r1',
                 'start': [0, 1], 'duration': [1, 1], 'pitches': [60]}],
            'transpose': 0}


def score_i():
    """与 A 同节奏同时长，但相对音程不同（[60,65] 而非 [60,64]）。"""
    score = score_a()
    score['events'] = [
        {'id': 'i1', 'voice': 'v1', 'measure': 'm1',
         'start': [1, 3], 'duration': [5, 3], 'pitches': [60, 65]},
        {'id': 'i2', 'voice': 'v2', 'measure': 'm2',
         'start': [0, 1], 'duration': [2, 1], 'pitches': [55]}]
    return score


def score_q():
    return {'measures': [{'id': 'q1', 'numerator': 4, 'denominator': 4}],
            'events': [{'id': 'qe', 'voice': 'vq', 'measure': 'q1',
                        'start': [0, 1], 'duration': [3, 2],
                        'pitches': [64]}],
            'transpose': 0}


def score_q2():
    return {'measures': [{'id': 'q2', 'numerator': 4, 'denominator': 4}],
            'events': [{'id': 'qe2', 'voice': 'vq', 'measure': 'q2',
                        'start': [0, 1], 'duration': [2, 1],
                        'pitches': [65]}],
            'transpose': 0}


def score_x():
    return {'measures': [{'id': 'x1', 'numerator': 4, 'denominator': 4}],
            'events': [{'id': 'xe', 'voice': 'vx', 'measure': 'x1',
                        'start': [0, 1], 'duration': [4, 1],
                        'pitches': [60]}],
            'transpose': 0}


def score_s1():
    return {'measures': [{'id': 's1', 'numerator': 4, 'denominator': 4}],
            'events': [{'id': 'se1', 'voice': 'vs', 'measure': 's1',
                        'start': [0, 1], 'duration': [2, 1],
                        'pitches': [61]}],
            'transpose': 0}


def score_s2():
    return {'measures': [{'id': 's2', 'numerator': 4, 'denominator': 4}],
            'events': [{'id': 'se2', 'voice': 'vs', 'measure': 's2',
                        'start': [0, 1], 'duration': [1, 1],
                        'pitches': [62]}],
            'transpose': 0}


def score_g():
    return {'measures': [{'id': 'g1', 'numerator': 4, 'denominator': 4}],
            'events': [{'id': 'ge', 'voice': 'vg', 'measure': 'g1',
                        'start': [0, 1], 'duration': [1, 1],
                        'pitches': [63]}],
            'transpose': 0}


def score_overlap():
    return {'measures': [{'id': 'o1', 'numerator': 4, 'denominator': 4}],
            'events': [
                {'id': 'oa', 'voice': 'vo', 'measure': 'o1',
                 'start': [0, 1], 'duration': [2, 1], 'pitches': [60]},
                {'id': 'ob', 'voice': 'vo', 'measure': 'o1',
                 'start': [1, 1], 'duration': [2, 1], 'pitches': [61]}],
            'transpose': 0}


def seed_v1(store):
    """空库发布 A、B、D：A 属 F 建议 train，B 属 G 建议 eval，D 属 H。"""
    return publish_corpus(store, 'req-1', 0, [
        upsert('A', 'F', 'train', score_a()),
        upsert('B', 'G', 'eval', score_b()),
        upsert('D', 'H', 'train', score_d())])


def bridge_changes():
    """登记 C：来源称其属 G，但内容与 A 仅移调/改名/等效延音之差。"""
    return [upsert('C', 'G', 'eval', score_c())]


def _crash_publish(store, request_id, expected_release, changes, point):
    """子进程：在故障注入点模拟进程退出（不返回、不清理）。"""
    corpus._FAULT_HOOKS[point] = lambda: os._exit(1)
    corpus.publish_corpus(store, request_id, expected_release, changes)


def _publish_worker(store, request_id, expected_release, changes, queue):
    try:
        release = publish_corpus(store, request_id, expected_release, changes)
        queue.put(('ok', request_id, release['release_id']))
    except Exception as e:
        queue.put(('error', request_id, type(e).__name__))


class CorpusCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / 'store'

    def assert_partition(self, release, current_ids):
        """同一版本每个当前有效 ID 恰好出现在其中一个列表。"""
        seen = {}
        for name in ('train', 'eval', 'quarantine'):
            for row in release[name]:
                self.assertNotIn(row['score_id'], seen,
                                 f"{row['score_id']} in two lists")
                seen[row['score_id']] = name
        self.assertEqual(set(seen), set(current_ids))
        for name in ('train', 'eval', 'quarantine'):
            self.assertEqual([r['score_id'] for r in release[name]],
                             sorted(r['score_id'] for r in release[name]))


class CorpusReleaseTests(CorpusCase):
    """验收 1：桥接隔离、历史锚点、历史版本原样可读、指纹边界。"""

    def test_bridge_quarantine_and_history(self):
        r0 = read_corpus(self.store)
        self.assertEqual(r0, {'release_id': 0, 'train': [], 'eval': [],
                              'quarantine': []})

        r1 = seed_v1(self.store)
        self.assertEqual(r1['release_id'], 1)
        self.assertEqual(ids(r1['train']), {'A', 'D'})
        self.assertEqual(ids(r1['eval']), {'B'})
        self.assertEqual(r1['quarantine'], [])
        self.assert_partition(r1, {'A', 'B', 'D'})
        json.dumps(r1)
        # 发布行：score_id、当前 family_id、全谱时长、发音跨度；
        # 跨度只含起点/终点/移调后音高，端点为规范精确分数字符串。
        row_a = next(r for r in r1['train'] if r['score_id'] == 'A')
        self.assertEqual(set(row_a), {'score_id', 'family_id', 'duration',
                                      'spans'})
        self.assertEqual(row_a['family_id'], 'F')
        self.assertEqual(row_a['duration'], '8')
        self.assertEqual(row_a['spans'], [
            {'start': '1/3', 'end': '2', 'pitches': [60, 64]},
            {'start': '4', 'end': '6', 'pitches': [55]}])
        for row in r1['train'] + r1['eval']:
            for span in row['spans']:
                self.assertEqual(set(span), {'start', 'end', 'pitches'})

        # 第二版：C 以内容指纹桥接 A、以曲族桥接 B -> 全部隔离。
        r2 = publish_corpus(self.store, 'req-2', 1, bridge_changes())
        self.assertEqual(r2['release_id'], 2)
        self.assertEqual(ids(r2['train']), {'D'})  # 不相关样本不过度隔离
        self.assertEqual(r2['eval'], [])
        self.assertEqual(ids(r2['quarantine']), {'A', 'B', 'C'})
        self.assert_partition(r2, {'A', 'B', 'C', 'D'})
        for row in r2['quarantine']:
            self.assertEqual(row['train_anchors'], ['A'])
            self.assertEqual(row['eval_anchors'], ['B'])
        row_c = next(r for r in r2['quarantine'] if r['score_id'] == 'C')
        self.assertEqual(row_c['family_id'], 'G')
        self.assertEqual(row_c['spans'], row_a['spans'])  # 等价写法同跨度

        # 第三版：撤回 C，更正 A 的曲族与内容；旧证据仍在 -> A、B 仍隔离。
        r3 = publish_corpus(self.store, 'req-3', 2, [
            withdraw('C'), upsert('A', 'F2', 'train', score_a_corrected())])
        self.assertEqual(r3['release_id'], 3)
        self.assertEqual(ids(r3['quarantine']), {'A', 'B'})
        self.assertNotIn('C', ids(r3['train']) | ids(r3['eval'])
                         | ids(r3['quarantine']))
        self.assertEqual(ids(r3['train']), {'D'})
        row_a3 = next(r for r in r3['quarantine'] if r['score_id'] == 'A')
        self.assertEqual(row_a3['family_id'], 'F2')
        self.assertEqual(row_a3['spans'][-1]['pitches'], [56])
        self.assert_partition(r3, {'A', 'B', 'D'})

        # 历史版本原样可读：A 在 train、B 在 eval 的第一版。
        self.assertEqual(read_corpus(self.store, 1), r1)
        self.assertEqual(read_corpus(self.store, 2), r2)
        self.assertEqual(read_corpus(self.store), r3)

        # 第四版：仅一侧锚点时，新谱从锚点而非建议分集。
        # E 与 D 同曲族 H（建议 eval），P 与 D 同内容指纹（建议 eval），
        # 组内只有 D 的 train 锚点 -> E、P 都进 train。
        r4 = publish_corpus(self.store, 'req-4', 3, [
            upsert('E', 'H', 'eval', score_e()),
            upsert('P', 'P1', 'eval', score_p())])
        self.assertEqual(ids(r4['train']), {'D', 'E', 'P'})
        self.assertEqual(r4['eval'], [])
        self.assertEqual(ids(r4['quarantine']), {'A', 'B'})
        self.assert_partition(r4, {'A', 'B', 'D', 'E', 'P'})

        # 第五版：节奏 / 谱尾留白 / 重复跨度 / 相对音程不同的样本
        # 不得被误判为同指纹（否则会被 A、B 双侧锚点组隔离或改道）。
        r5 = publish_corpus(self.store, 'req-5', 4, [
            upsert('Z', 'FZ', 'train', score_z()),
            upsert('W', 'FW', 'eval', score_w()),
            upsert('R', 'FR', 'train', score_r()),
            upsert('I', 'FI', 'train', score_i())])
        self.assertEqual(ids(r5['train']), {'D', 'E', 'P', 'Z', 'R', 'I'})
        self.assertEqual(ids(r5['eval']), {'W'})
        self.assertEqual(ids(r5['quarantine']), {'A', 'B'})
        self.assert_partition(r5, {'A', 'B', 'D', 'E', 'P', 'Z', 'W', 'R',
                                   'I'})
        # 重复跨度保留重复次数
        row_r = next(r for r in r5['train'] if r['score_id'] == 'R')
        self.assertEqual(row_r['spans'], [
            {'start': '0', 'end': '1', 'pitches': [60]},
            {'start': '0', 'end': '1', 'pitches': [60]}])
        self.assertEqual(read_corpus(self.store), r5)

    def test_idempotent_replay_and_canonical_comparison(self):
        r1 = publish_corpus(self.store, 'req-1', 0,
                            [upsert('A', 'F', 'train', score_a())])
        # 原样重试：先于版本检查返回原版本，不推进当前版本
        again = publish_corpus(self.store, 'req-1', 0,
                               [upsert('A', 'F', 'train', score_a())])
        self.assertEqual(again, r1)
        self.assertEqual(read_corpus(self.store)['release_id'], 1)
        # 对象键顺序不计入请求比较
        reordered = [{'score': score_a(), 'preferred_split': 'train',
                      'family_id': 'F', 'score_id': 'A', 'op': 'upsert'}]
        self.assertEqual(publish_corpus(self.store, 'req-1', 0, reordered),
                         r1)
        # 同 ID 不同内容（含 expected_release 不同）须明确冲突
        with self.assertRaises(RequestConflict):
            publish_corpus(self.store, 'req-1', 0,
                           [upsert('A', 'F', 'eval', score_a())])
        with self.assertRaises(RequestConflict):
            publish_corpus(self.store, 'req-1', 1,
                           [upsert('A', 'F', 'train', score_a())])
        # 数组顺序计入请求比较
        r2 = publish_corpus(self.store, 'req-2', 1, [
            upsert('Z1', 'FZ', 'train', score_q()),
            upsert('Z2', 'FZ', 'train', score_q2())])
        self.assertEqual(r2['release_id'], 2)
        with self.assertRaises(RequestConflict):
            publish_corpus(self.store, 'req-2', 1, [
                upsert('Z2', 'FZ', 'train', score_q2()),
                upsert('Z1', 'FZ', 'train', score_q())])
    def test_reregistration_continues_identity(self):
        # X 首发到 train 后撤回：版本里不再出现，但证据与锚点保留
        r1 = publish_corpus(self.store, 'req-1', 0,
                            [upsert('X', 'F1', 'train', score_x())])
        self.assertEqual(ids(r1['train']), {'X'})
        r2 = publish_corpus(self.store, 'req-2', 1, [withdraw('X')])
        self.assertEqual(r2, {'release_id': 2, 'train': [], 'eval': [],
                              'quarantine': []})
        # 相同 ID 重新登记延续原身份：新曲族新内容仍受 train 锚点约束
        r3 = publish_corpus(self.store, 'req-3', 2,
                            [upsert('X', 'F2', 'eval', score_q2())])
        self.assertEqual(ids(r3['train']), {'X'})
        self.assertEqual(r3['eval'], [])
        # 撤回后旧曲族别名依然有效：Y 登记 X 的旧曲族 F1 即被连入同组
        r4 = publish_corpus(self.store, 'req-4', 3,
                            [upsert('Y', 'F1', 'eval', score_g())])
        self.assertEqual(ids(r4['train']), {'X', 'Y'})
        self.assertEqual(r4['eval'], [])

    def test_quarantine_lists_multiple_sorted_anchors(self):
        renamed_s1 = score_s1()
        renamed_s1['events'][0]['id'] = 'other'
        renamed_x = score_x()
        renamed_x['events'][0]['id'] = 'other'
        publish_corpus(self.store, 'req-1', 0, [
            upsert('X1', 'F1', 'train', score_x()),
            upsert('X2', 'F2', 'train', score_s1()),
            upsert('Y', 'F3', 'eval', score_b())])
        # B1 以曲族 F1 与 X2 的内容同时牵连；B2 以曲族 F3 与 X1 的内容
        # 同时牵连 -> 全组连通，两侧锚点齐备，整组隔离
        r2 = publish_corpus(self.store, 'req-2', 1, [
            upsert('B1', 'F1', 'eval', renamed_s1),
            upsert('B2', 'F3', 'train', renamed_x)])
        self.assertEqual(ids(r2['quarantine']), {'X1', 'X2', 'Y', 'B1',
                                                 'B2'})
        self.assertEqual(r2['train'], [])
        self.assertEqual(r2['eval'], [])
        for row in r2['quarantine']:
            self.assertEqual(row['train_anchors'], ['X1', 'X2'])
            self.assertEqual(row['eval_anchors'], ['Y'])

    def test_identity_spaces_and_min_id_rule(self):
        # 曲族 ID 与谱 ID 是不同身份空间：谱 K 与曲族 K 互不冲突
        r1 = publish_corpus(self.store, 'req-1', 0, [
            upsert('K', 'K', 'train', score_x()),
            upsert('J', 'K', 'eval', score_g())])
        # 无锚点组按字典序最小当前 ID（J）的建议分集：J 建议 eval
        self.assertEqual(r1['train'], [])
        self.assertEqual(ids(r1['eval']), {'J', 'K'})


class CorpusCrashTests(CorpusCase):
    """验收 2：提交点前后进程中断，重开存储可区分两种情形。"""

    def _crash(self, store, request_id, expected_release, changes, point):
        ctx = multiprocessing.get_context('fork')
        proc = ctx.Process(target=_crash_publish,
                           args=(str(store), request_id, expected_release,
                                 changes, point))
        proc.start()
        proc.join()
        self.assertEqual(proc.exitcode, 1)

    def test_crash_before_commit_leaves_no_trace(self):
        seed_v1(self.store)
        self._crash(self.store, 'req-bridge', 1, bridge_changes(),
                    'before_commit')
        # 重开仍是旧版，读不到新版本
        self.assertEqual(read_corpus(self.store)['release_id'], 1)
        with self.assertRaises(UnknownRelease):
            read_corpus(self.store, 2)
        # 提交不带桥接的新批次：没有遗留证据改变 A 或 B 的分集
        r2 = publish_corpus(self.store, 'req-q', 1,
                            [upsert('Q', 'FQ', 'eval', score_q())])
        self.assertEqual(r2['release_id'], 2)
        self.assertEqual(ids(r2['train']), {'A', 'D'})
        self.assertEqual(ids(r2['eval']), {'B', 'Q'})
        self.assertEqual(r2['quarantine'], [])
        # 未成功提交的请求不占用 ID：同 ID 不同内容可正常提交
        r3 = publish_corpus(self.store, 'req-bridge', 2,
                            [upsert('Q2', 'FQ2', 'train', score_q2())])
        self.assertEqual(r3['release_id'], 3)
        self.assertEqual(ids(r3['train']), {'A', 'D', 'Q2'})

    def test_crash_after_commit_is_visible_and_replayable(self):
        seed_v1(self.store)
        self._crash(self.store, 'req-bridge', 1, bridge_changes(),
                    'after_commit')
        # 重开能看到完整隔离版（提交已生效，只是调用方没拿到返回）
        current = read_corpus(self.store)
        self.assertEqual(current['release_id'], 2)
        self.assertEqual(ids(current['quarantine']), {'A', 'B', 'C'})
        self.assertEqual(ids(current['train']), {'D'})
        # 先再成功发布一次撤回 C
        r3 = publish_corpus(self.store, 'req-wd', 2, [withdraw('C')])
        self.assertEqual(r3['release_id'], 3)
        self.assertEqual(ids(r3['quarantine']), {'A', 'B'})
        # 用原请求 ID 和原参数重放桥接请求：返回仍包含 C 的原隔离版
        replay = publish_corpus(self.store, 'req-bridge', 1,
                                bridge_changes())
        self.assertEqual(replay['release_id'], 2)
        self.assertEqual(ids(replay['quarantine']), {'A', 'B', 'C'})
        self.assertEqual(replay, read_corpus(self.store, 2))
        # 当前版仍不含 C，且未被重放推进
        self.assertEqual(read_corpus(self.store)['release_id'], 3)
        self.assertEqual(ids(read_corpus(self.store)['quarantine']),
                         {'A', 'B'})
        # 同 ID 改参数须冲突
        with self.assertRaises(RequestConflict):
            publish_corpus(self.store, 'req-bridge', 1,
                           [upsert('C', 'G', 'train', score_c())])
        with self.assertRaises(RequestConflict):
            publish_corpus(self.store, 'req-bridge', 3, bridge_changes())


class CorpusConcurrencyTests(CorpusCase):
    """验收 3a：基于同一版本的并发写入，至多一个成功。"""

    def test_concurrent_publishers_single_winner(self):
        publish_corpus(self.store, 'req-x', 0,
                       [upsert('X', 'FX', 'train', score_x())])
        ctx = multiprocessing.get_context('fork')
        queue = ctx.Queue()
        batch_s1 = [upsert('S1', 'FS1', 'train', score_s1())]
        batch_s2 = [upsert('S2', 'FS2', 'eval', score_s2())]
        procs = [ctx.Process(target=_publish_worker,
                             args=(str(store), rid, 1, batch, queue))
                 for store, rid, batch in (
                     (self.store, 'rid-s1', batch_s1),
                     (self.store, 'rid-s2', batch_s2))]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join()
            self.assertEqual(proc.exitcode, 0)
        results = sorted(queue.get(timeout=10) for _ in procs)
        self.assertEqual([r[0] for r in results], ['error', 'ok'])
        self.assertEqual(results[0][2], 'ReleaseConflict')
        self.assertEqual(results[1][2], 2)
        loser, winner = results[0][1], results[1][1]
        winner_score = 'S1' if winner == 'rid-s1' else 'S2'
        # 新版本只含胜方样本；失败方不留证据、锚点或请求绑定
        r2 = read_corpus(self.store, 2)
        self.assertEqual(ids(r2['train']) | ids(r2['eval']),
                         {'X', winner_score})
        state = json.loads((self.store / 'state.json').read_text())
        self.assertEqual({e['score_id'] for e in state['evidence']},
                         {'X', winner_score})
        self.assertEqual(set(state['requests']), {'req-x', winner})
        self.assertEqual(set(state['anchors']), {'X', winner_score})
        # 失败方基于新版本重新提交同一请求 ID：未占用，可成功
        loser_batch = batch_s1 if loser == 'rid-s1' else batch_s2
        r3 = publish_corpus(self.store, loser, 2, loser_batch)
        self.assertEqual(r3['release_id'], 3)
        self.assertEqual(ids(r3['train']), {'X', 'S1'})
        self.assertEqual(ids(r3['eval']), {'S2'})
        self.assertEqual(r3['quarantine'], [])


class CorpusValidationTests(CorpusCase):
    """验收 3b：不合规谱混合批次整批拒绝；错误类别可区分；旧入口回归。"""

    def test_mixed_batch_rejected_atomically(self):
        publish_corpus(self.store, 'req-1', 0,
                       [upsert('X', 'FX', 'train', score_x())])
        mixed = [upsert('G', 'FG', 'train', score_g()),
                 upsert('H', 'FH', 'eval', score_overlap())]
        with self.assertRaises(InvalidChange):
            publish_corpus(self.store, 'req-mix', 1, mixed)
        # 不产生新版本，也不留隐形关系
        self.assertEqual(read_corpus(self.store)['release_id'], 1)
        with self.assertRaises(UnknownRelease):
            read_corpus(self.store, 2)
        state = json.loads((self.store / 'state.json').read_text())
        self.assertEqual({e['score_id'] for e in state['evidence']}, {'X'})
        self.assertEqual(set(state['current']), {'X'})
        self.assertEqual(set(state['requests']), {'req-1'})
        # 被拒批次不占用请求 ID：修正后可重新提交
        r2 = publish_corpus(self.store, 'req-mix', 1,
                            [upsert('G', 'FG', 'train', score_g())])
        self.assertEqual(r2['release_id'], 2)
        self.assertEqual(ids(r2['train']), {'X', 'G'})
        # 被拒的曲族 FH 没有留下关系：新谱按自身建议分集
        r3 = publish_corpus(self.store, 'req-3', 2,
                            [upsert('M', 'FH', 'eval', score_q2())])
        self.assertEqual(ids(r3['eval']), {'M'})

    def test_admission_rules(self):
        publish_corpus(self.store, 'req-0', 0,
                       [upsert('X', 'FX', 'train', score_x())])
        bad_scores = {
            'overlap': score_overlap(),
            'overrun': {'measures': [{'id': 'm', 'numerator': 2,
                                      'denominator': 4}],
                        'events': [{'id': 'o', 'voice': 'v', 'measure': 'm',
                                    'start': [1, 1], 'duration': [2, 1],
                                    'pitches': [60]}],
                        'transpose': 0},
            'unfulfilled_tie': {
                'measures': [{'id': 'm', 'numerator': 4, 'denominator': 4}],
                'events': [
                    {'id': 't1', 'voice': 'v', 'measure': 'm',
                     'start': [0, 1], 'duration': [1, 1], 'pitches': [60],
                     'tie_to': 't2'},
                    {'id': 't2', 'voice': 'v', 'measure': 'm',
                     'start': [2, 1], 'duration': [1, 1], 'pitches': [60]}],
                'transpose': 0},
            'rests_only': {'measures': [{'id': 'm', 'numerator': 4,
                                         'denominator': 4}],
                           'events': [{'id': 'r', 'voice': 'v',
                                       'measure': 'm', 'start': [0, 1],
                                       'duration': [4, 1], 'pitches': []}],
                           'transpose': 0},
            'no_events': {'measures': [{'id': 'm', 'numerator': 4,
                                        'denominator': 4}],
                          'events': [], 'transpose': 0},
            'bad_structure': {'measures': [{'id': 'm', 'numerator': 4,
                                            'denominator': 4}],
                              'events': [{'id': 'n', 'voice': 'v',
                                          'measure': 'ghost',
                                          'start': [0, 1],
                                          'duration': [1, 1],
                                          'pitches': [60]}],
                              'transpose': 0},
        }
        for index, (name, bad) in enumerate(bad_scores.items()):
            with self.subTest(admission=name):
                with self.assertRaises(InvalidChange):
                    publish_corpus(self.store, f'req-bad-{index}', 1,
                                   [upsert('BAD', 'FB', 'train', bad)])
        self.assertEqual(read_corpus(self.store)['release_id'], 1)
        # 缺拍本身不拒绝：声部允许留白
        gapped = {'measures': [{'id': 'm', 'numerator': 4,
                                'denominator': 4}],
                  'events': [{'id': 'g', 'voice': 'v', 'measure': 'm',
                              'start': [1, 1], 'duration': [1, 1],
                              'pitches': [60]}],
                  'transpose': 0}
        r2 = publish_corpus(self.store, 'req-gap', 1,
                            [upsert('GAP', 'FG', 'train', gapped)])
        self.assertEqual(ids(r2['train']), {'X', 'GAP'})

    def test_error_categories_are_distinguishable(self):
        publish_corpus(self.store, 'req-1', 0,
                       [upsert('X', 'FX', 'train', score_x())])
        with self.assertRaises(UnknownScore):
            publish_corpus(self.store, 'req-2', 1, [withdraw('ghost')])
        with self.assertRaises(ReleaseConflict):
            publish_corpus(self.store, 'req-3', 5,
                           [upsert('Y', 'FY', 'train', score_q())])
        with self.assertRaises(UnknownRelease):
            read_corpus(self.store, 99)
        with self.assertRaises(InvalidChange):
            publish_corpus(self.store, 'req-4', 1, [])
        # 四种失败互不相同，且都属于 CorpusError
        for cls in (UnknownScore, ReleaseConflict, UnknownRelease,
                    InvalidChange, RequestConflict):
            self.assertTrue(issubclass(cls, CorpusError))
        codes = {cls.code for cls in (UnknownScore, ReleaseConflict,
                                      UnknownRelease, InvalidChange,
                                      RequestConflict)}
        self.assertEqual(len(codes), 5)
        self.assertNotIsInstance(UnknownScore('x'), InvalidChange)
        self.assertIsInstance(InvalidChange('x'), ValueError)
        # 失败请求不占用 ID
        r2 = publish_corpus(self.store, 'req-2', 1,
                            [upsert('Y', 'FY', 'eval', score_q())])
        self.assertEqual(r2['release_id'], 2)

    def test_request_shape_validation(self):
        good = upsert('X', 'FX', 'train', score_x())
        cases = [
            ('not-a-list', 'changes-not-list'),
            ([], 'empty'),
            ([dict(good, score_id=f'ID{i}') for i in range(101)],
             'too-many'),
            ([good, dict(good)], 'duplicate-score-id'),
            ([{'op': 'noop', 'score_id': 'X'}], 'bad-op'),
            ([dict(good, extra=1)], 'extra-field'),
            ([dict(good, preferred_split='side')], 'bad-split'),
            ([dict(good, score_id='bad id!')], 'bad-score-id'),
            ([withdraw('X'), upsert('X', 'FX', 'train', score_x())],
             'duplicate-withdraw-upsert'),
        ]
        for changes, name in cases:
            with self.subTest(shape=name):
                with self.assertRaises(InvalidChange):
                    publish_corpus(self.store, f'req-{name}', 0, changes)
        with self.assertRaises(InvalidChange):
            publish_corpus(self.store, 'req-bool', True, [good])
        with self.assertRaises(InvalidChange):
            publish_corpus(self.store, 'bad rid!', 0, [good])
        with self.assertRaises(InvalidChange):
            read_corpus(self.store, '1')
        self.assertEqual(read_corpus(self.store)['release_id'], 0)

    def test_legacy_process_unaffected_by_admission(self):
        # 训练准入不影响旧入口：重叠谱在 process 下仍出报告而非异常
        result = process(score_overlap())
        self.assertIn('overlap', [d['kind'] for d in result['diagnostics']])
        self.assertEqual(result['summary']['sounding_spans'], 2)


if __name__ == '__main__':
    unittest.main()
