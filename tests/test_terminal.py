import json
import unittest
from fractions import Fraction

from score_spans.model import parse
from score_spans.terminal import (
    CLOSE, OPEN, RECOVERY_FRAME, SCORE_FRAME, InvalidCheckpoint,
    InvalidTransfer, ReceiptRejected, TerminalDelivery, build_frames,
    open_transfer)


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


def reload(delivery):
    """只靠最新 JSON 检查点重建状态机。"""
    return TerminalDelivery.from_json(delivery.to_json())


def accept(delivery, packet):
    delivery.receive_receipt(packet['transfer_id'],
                             packet['terminal_code'],
                             packet['packet_index'])


def drain(delivery):
    """取包并逐包确认直到结束，返回所有取到的包。"""
    seen = []
    packet = delivery.next_packet()
    while packet is not None:
        seen.append(packet)
        accept(delivery, packet)
        packet = delivery.next_packet()
    return seen


class FakeTerminal:
    """本地接收端替身：按 (传送, 代号, 包序号) 去重，收齐整帧才执行。

    记录已暂存片与已执行帧，模拟真实终端的去重与整帧原子执行语义。
    """

    def __init__(self):
        self._seen = set()                  # (transfer_id, code, index)
        self._buffers = {}                  # (transfer, code) -> 帧缓存
        self.held = set()                   # 当前亮着的物理键 (ch, pitch)
        self.executed_frames = []

    def handle(self, packet, *, drop_last_receipt=False):
        key = (packet['transfer_id'], packet['terminal_code'],
               packet['packet_index'])
        if key in self._seen:
            # 终端去重：重复片不暂存、不重新执行整帧，但重发回执。
            return {'transfer_id': packet['transfer_id'],
                    'terminal_code': packet['terminal_code'],
                    'packet_index': packet['packet_index']}
        self._seen.add(key)
        frame_key = (packet['transfer_id'], packet['terminal_code'],
                     packet['frame_kind'], packet['time'])
        buffer = self._buffers.setdefault(
            frame_key, {'total': packet['fragment_total'], 'fragments': {}})
        buffer['fragments'][packet['fragment_index']] = packet
        if (drop_last_receipt
                and packet['fragment_index'] == packet['fragment_total']):
            # 末片已在终端生效，但回执丢失。
            self._maybe_execute(frame_key)
            return None
        if len(buffer['fragments']) == buffer['total']:
            self._maybe_execute(frame_key)
        return {'transfer_id': packet['transfer_id'],
                'terminal_code': packet['terminal_code'],
                'packet_index': packet['packet_index']}

    def _maybe_execute(self, frame_key):
        buffer = self._buffers[frame_key]
        if len(buffer['fragments']) != buffer['total'] \
                or frame_key in self.executed_frames:
            return
        self.executed_frames.append(frame_key)
        ordered = [buffer['fragments'][i]
                   for i in sorted(buffer['fragments'])]
        for packet in ordered:
            for op in packet['operations']:
                key = (op['channel'], op['pitch'])
                if op['switch'] == CLOSE:
                    self.held.discard(key)
                else:
                    self.held.add(key)

    def reset(self):
        self._seen.clear()
        self._buffers.clear()
        self.held.clear()


def acceptance_score():
    """A60[0,2)、B60[1,3)、C60[3,4)、D64[3,4)、E67[3,4)，五个不同声部。"""
    return score([
        note('a', 0, 2, 60, voice='A'),
        note('b', 1, 2, 60, voice='B'),
        note('c', 3, 1, 60, voice='C'),
        note('d', 3, 1, 64, voice='D'),
        note('e', 3, 1, 67, voice='E')],
        measures=[{'id': 'm1', 'numerator': 4, 'denominator': 4}])


ACCEPTANCE_CHANNELS = {v: 1 for v in 'ABCDE'}


class FrameSemanticsTests(unittest.TestCase):
    """验收 1：物理键占用合并、先关后开、同刻排序与延音链不重触发。"""

    def test_acceptance_frame_operations(self):
        delivery = open_transfer('T-1', 0, acceptance_score(),
                                 ACCEPTANCE_CHANNELS, 64)
        packets = drain(delivery)
        timeline = {p['time']: [op['switch'] + str(op['pitch'])
                                for op in p['operations']]
                    for p in packets}
        # 1、2 时没有帧。
        self.assertEqual(sorted(timeline), ['0', '3', '4'])
        self.assertEqual(timeline['0'], ['on60'])
        self.assertEqual(timeline['3'],
                         ['off60', 'on60', 'on64', 'on67'])
        self.assertEqual(timeline['4'],
                         ['off60', 'off64', 'off67'])
        # 每帧只有一片。
        self.assertTrue(all(p['fragment_total'] == 1 for p in packets))
        self.assertEqual(delivery.confirmed_position(), '4')
        self.assertIsNone(delivery.next_packet())  # 无更多包明确结束

    def test_no_frame_at_times_without_operations(self):
        # 两个同键占用首尾相接：中间时刻必须先关后开，因此该时刻有帧；
        # 而占用中间的时间点（1、2）无任何操作。
        frames = build_frames(parse(acceptance_score())[1],
                              ACCEPTANCE_CHANNELS)
        self.assertEqual([str(f['time']) for f in frames], ['0', '3', '4'])

    def test_tie_chain_seam_does_not_retrigger_with_exact_fractions(self):
        s = score([
            note('h', 0, Fraction(1, 3), tie_to='m'),
            note('m', Fraction(1, 3), Fraction(1, 3), tie_to='t'),
            note('t', Fraction(2, 3), Fraction(1, 3))])
        delivery = open_transfer('T-2', 0, s, {'v1': 1}, 64)
        packets = drain(delivery)
        # 整条延音链只在 0 开一次、1 关一次，1/3 与 2/3 接缝不产生帧。
        self.assertEqual(len(packets), 2)
        self.assertEqual([(op['switch'], op['pitch'])
                          for op in packets[0]['operations']], [(OPEN, 60)])
        self.assertEqual(packets[0]['time'], '0')
        self.assertEqual([(op['switch'], op['pitch'])
                          for op in packets[1]['operations']],
                         [(CLOSE, 60)])
        self.assertEqual(packets[1]['time'], '1')

    def test_same_pitch_different_channels_are_independent(self):
        s = score([
            note('a', 0, 2, 60, voice='A'),
            note('b', 1, 2, 60, voice='B')])
        delivery = open_transfer('T-3', 0, s, {'A': 1, 'B': 2}, 64)
        packets = drain(delivery)
        timeline = {p['time']: sorted((op['channel'], op['switch'],
                                       op['pitch'])
                                      for op in p['operations'])
                    for p in packets}
        self.assertEqual(timeline['0'], [(1, OPEN, 60)])
        self.assertEqual(timeline['1'], [(2, OPEN, 60)])
        self.assertEqual(timeline['2'], [(1, CLOSE, 60)])
        self.assertEqual(timeline['3'], [(2, CLOSE, 60)])

    def test_groups_sorted_by_channel_then_pitch(self):
        s = score([
            note('a', 0, 1, 67, voice='A'),
            note('b', 0, 1, 60, voice='B'),
            note('c', 0, 1, 64, voice='C')])
        delivery = open_transfer('T-4', 0, s,
                                 {'A': 2, 'B': 1, 'C': 1}, 64)
        packet = delivery.next_packet()
        self.assertEqual([(op['channel'], op['pitch'])
                          for op in packet['operations']],
                         [(1, 60), (1, 64), (2, 67)])

    def test_close_before_open_even_when_key_identical(self):
        # 旧占用此刻全部结束而新占用同时开始：即使键号没变也先关后开。
        s = score([note('a', 0, 1, 60, voice='A'),
                   note('b', 1, 1, 60, voice='B')])
        delivery = open_transfer('T-5', 0, s, {'A': 1, 'B': 1}, 64)
        first = delivery.next_packet()
        accept(delivery, first)
        second = delivery.next_packet()
        self.assertEqual([(op['switch'], op['pitch'])
                          for op in second['operations']],
                         [(CLOSE, 60), (OPEN, 60)])

    def test_held_key_stays_open_while_one_occupancy_spans(self):
        # A[0,2) 与 B[1,2) 同键；t=1 B 开始时键已开 -> 无操作；
        # t=2 两者同时结束 -> 只关一次。
        s = score([note('a', 0, 2, 60, voice='A'),
                   note('b', 1, 1, 60, voice='B')])
        delivery = open_transfer('T-6', 0, s, {'A': 1, 'B': 1}, 64)
        packets = drain(delivery)
        self.assertEqual([p['time'] for p in packets], ['0', '2'])
        self.assertEqual([op['switch'] for op in packets[1]['operations']],
                         [CLOSE])

    def test_time_strings_are_canonical_fractions(self):
        s = score([note('a', Fraction(1, 2), Fraction(1, 2), 60)])
        delivery = open_transfer('T-7', 0, s, {'v1': 1}, 64)
        packets = drain(delivery)
        self.assertEqual([p['time'] for p in packets], ['1/2', '1'])


class PacketizationReceiptResetTests(unittest.TestCase):
    """验收 2：K=2 分包、末片回执丢失、复位恢复屏障与无复位对照。"""

    def setUp(self):
        self.delivery = open_transfer(
            'T-8', 0, acceptance_score(), ACCEPTANCE_CHANNELS, 2)

    def test_packets_split_with_earlier_fragments_full(self):
        # 0 时帧只有 1 个操作，K=2 下仍是单片。
        first = self.delivery.next_packet()
        self.assertEqual((first['packet_index'], first['fragment_index'],
                          first['fragment_total']), (1, 1, 1))
        self.assertEqual(len(first['operations']), 1)
        accept(self.delivery, first)
        # 3 时帧有 4 个操作：K=2 下均分为两片，前面的包装满。
        second = self.delivery.next_packet()
        self.assertEqual((second['packet_index'], second['fragment_index'],
                          second['fragment_total']), (2, 1, 2))
        self.assertEqual(len(second['operations']), 2)
        # 非末片确认前不能取同帧下一片：反复取仍是同一个完整包。
        self.assertEqual(self.delivery.next_packet(), second)
        accept(self.delivery, second)
        third = self.delivery.next_packet()
        self.assertEqual((third['packet_index'], third['fragment_index'],
                          third['fragment_total']), (3, 2, 2))
        self.assertEqual(len(third['operations']), 2)
        self.assertEqual(third['time'], '3')

    def test_repeated_next_packet_returns_identical_packet_after_reload(self):
        first = self.delivery.next_packet()
        self.assertEqual(self.delivery.next_packet(), first)
        reloaded = reload(self.delivery)
        self.assertEqual(reloaded.next_packet(), first)

    def test_reset_before_last_receipt_then_recovery_barrier(self):
        terminal = FakeTerminal()
        # 确认 0 时帧（单片）。
        p0 = self.delivery.next_packet()
        accept(self.delivery, terminal.handle(p0))
        self.assertEqual(self.delivery.confirmed_position(), '0')
        # 3 时帧第一片确认暂存，第二片取出但末片回执到达前复位。
        f1 = self.delivery.next_packet()
        self.assertEqual(f1['time'], '3')
        accept(self.delivery, terminal.handle(f1))
        f2 = self.delivery.next_packet()
        self.assertEqual(f2['fragment_index'], 2)
        terminal.reset()
        self.delivery.record_reset(1)

        for label, restored in (('reloaded', reload(self.delivery)),
                                ('live', self.delivery)):
            with self.subTest(label=label):
                # 新代号只能先恢复 60（0 时帧后应亮的唯一物理键）。
                recovery = restored.next_packet()
                self.assertEqual(recovery['frame_kind'], RECOVERY_FRAME)
                self.assertEqual(recovery['terminal_code'], 1)
                self.assertEqual(recovery['packet_index'], 1)
                self.assertEqual(recovery['time'], '0')
                self.assertEqual([(op['channel'], op['pitch'], op['switch'])
                                  for op in recovery['operations']],
                                 [(1, 60, OPEN)])
                # 旧代号末片回执不能越过恢复屏障。
                with self.assertRaises(ReceiptRejected):
                    restored.receive_receipt('T-8', 0, f2['packet_index'])
                accept(restored, recovery)
                # 恢复不推进谱面位置。
                self.assertEqual(restored.confirmed_position(), '0')
                # 恢复完成后从 3 时帧首片完整重送。
                again1 = restored.next_packet()
                self.assertEqual(again1['frame_kind'], SCORE_FRAME)
                self.assertEqual(again1['time'], '3')
                self.assertEqual(again1['fragment_index'], 1)
                self.assertEqual(again1['packet_index'], 2)
                accept(restored, again1)
                # 第一片回执不推进奏谱位置。
                self.assertEqual(restored.confirmed_position(), '0')
                again2 = restored.next_packet()
                self.assertEqual(again2['fragment_index'], 2)
                accept(restored, again2)
                self.assertEqual(restored.confirmed_position(), '3')

    def test_reset_when_last_fragment_executed_but_receipt_lost(self):
        terminal = FakeTerminal()
        p0 = self.delivery.next_packet()
        accept(self.delivery, terminal.handle(p0))
        f1 = self.delivery.next_packet()
        accept(self.delivery, terminal.handle(f1))
        f2 = self.delivery.next_packet()
        # 末片在终端已生效，但回执丢失；随后终端复位清空。
        self.assertIsNone(terminal.handle(f2, drop_last_receipt=True))
        terminal.reset()
        self.delivery.record_reset(1)
        restored = reload(self.delivery)
        recovery = restored.next_packet()
        self.assertEqual(recovery['frame_kind'], RECOVERY_FRAME)
        # 只能恢复 60，不能恢复 64/67：3 时帧从未整帧确认。
        self.assertEqual([(op['channel'], op['pitch'])
                          for op in recovery['operations']], [(1, 60)])
        with self.assertRaises(ReceiptRejected):
            restored.receive_receipt('T-8', 0, f2['packet_index'])
        accept(restored, recovery)
        # 完整重送 3 时的两片，即便此前曾发到末片。
        again1 = restored.next_packet()
        self.assertEqual((again1['time'], again1['fragment_index']),
                         ('3', 1))
        accept(restored, again1)
        again2 = restored.next_packet()
        self.assertEqual(again2['fragment_index'], 2)
        accept(restored, again2)
        self.assertEqual(restored.confirmed_position(), '3')

    def test_no_reset_control_last_receipt_lost_keeps_same_packet(self):
        terminal = FakeTerminal()
        p0 = self.delivery.next_packet()
        accept(self.delivery, terminal.handle(p0))
        f1 = self.delivery.next_packet()
        accept(self.delivery, terminal.handle(f1))
        f2 = self.delivery.next_packet()
        lost = terminal.handle(f2, drop_last_receipt=True)
        self.assertIsNone(lost)
        # 无复位：原代号反复取到原末片（包序号不变），终端去重且不重执行。
        reloaded = reload(self.delivery)
        same = reloaded.next_packet()
        self.assertEqual(same, f2)
        executed_before = len(terminal.executed_frames)
        duplicate_receipt = terminal.handle(same)
        self.assertEqual(duplicate_receipt['packet_index'], f2['packet_index'])
        self.assertEqual(len(terminal.executed_frames), executed_before)
        accept(reloaded, duplicate_receipt)
        self.assertEqual(reloaded.confirmed_position(), '3')

    def test_non_final_receipt_does_not_advance_position(self):
        p0 = self.delivery.next_packet()
        accept(self.delivery, p0)
        f1 = self.delivery.next_packet()
        accept(self.delivery, f1)  # 非末片只暂存，不推进奏谱位置
        self.assertEqual(self.delivery.confirmed_position(), '0')
        # 暂存后仍只能取同帧第二片。
        f2 = self.delivery.next_packet()
        self.assertEqual((f2['time'], f2['fragment_index']), ('3', 2))

    def test_packet_metadata_shape(self):
        packet = self.delivery.next_packet()
        self.assertEqual(set(packet), {
            'transfer_id', 'terminal_code', 'packet_index', 'frame_kind',
            'time', 'fragment_index', 'fragment_total', 'operations'})
        self.assertEqual(packet['transfer_id'], 'T-8')
        self.assertEqual(packet['terminal_code'], 0)
        self.assertTrue(all(set(op) == {'channel', 'pitch', 'switch'}
                            for op in packet['operations']))


class RecoveryFragmentationTests(unittest.TestCase):
    """验收 3：恢复本身分片、恢复途中再复位、恢复完成前不混入奏谱包。"""

    def _busy_score(self, voices):
        # 每个声部一个独立音高，全部 [0,2)，t=1 无操作；t=2 整批关闭。
        events = [note(f'n{i}', 0, 2, 40 + i, voice=v)
                  for i, v in enumerate(voices)]
        return score(events), {v: 1 for v in voices}

    def test_recovery_is_fragmented_and_restarts_on_repeated_reset(self):
        voices = [f'V{i}' for i in range(5)]
        s, channels = self._busy_score(voices)
        delivery = open_transfer('T-9', 0, s, channels, 2)
        # 只确认 0 时开 5 键的整帧（K=2 -> 3 片），2 时关闭帧尚未交付。
        for _ in range(3):
            packet = delivery.next_packet()
            self.assertEqual(packet['time'], '0')
            accept(delivery, packet)
        self.assertEqual(delivery.confirmed_position(), '0')
        # 复位：5 个确认活跃键超过 K，恢复帧必须分片，且不推进位置。
        delivery.record_reset(1)
        r1 = delivery.next_packet()
        self.assertEqual(r1['frame_kind'], RECOVERY_FRAME)
        self.assertEqual((r1['fragment_index'], r1['fragment_total']),
                         (1, 3))
        self.assertEqual(len(r1['operations']), 2)  # 前面的包装满
        accept(delivery, r1)
        r2 = delivery.next_packet()
        accept(delivery, r2)
        # 恢复途中再次复位：从首片重来，包序号在新代号重新从 1 开始。
        delivery.record_reset(2)
        reloaded = reload(delivery)
        again1 = reloaded.next_packet()
        self.assertEqual(again1['terminal_code'], 2)
        self.assertEqual(again1['packet_index'], 1)
        self.assertEqual(again1['fragment_index'], 1)
        self.assertEqual(again1['frame_kind'], RECOVERY_FRAME)
        # 恢复完成前不会混入奏谱包：逐片确认期间全部是 recovery。
        accept(reloaded, again1)
        for expected_index in (2, 3):
            packet = reloaded.next_packet()
            self.assertEqual(packet['frame_kind'], RECOVERY_FRAME)
            self.assertEqual(packet['fragment_index'], expected_index)
            accept(reloaded, packet)
        # 恢复不推进位置；恢复完成后续送未交付的 2 时关闭帧首片。
        self.assertEqual(reloaded.confirmed_position(), '0')
        nxt = reloaded.next_packet()
        self.assertEqual(nxt['frame_kind'], SCORE_FRAME)
        self.assertEqual(nxt['time'], '2')
        self.assertEqual(nxt['fragment_index'], 1)

    def test_recovery_after_partial_score_delivery_reopens_only_held_keys(self):
        # 0 时帧开 5 键；2 时帧尚未确认时复位，恢复只重开这 5 键。
        voices = [f'V{i}' for i in range(5)]
        s, channels = self._busy_score(voices)
        delivery = open_transfer('T-10', 0, s, channels, 3)
        open_packets = []
        for _ in range(2):  # 5 键 / K=3 -> 两片
            packet = delivery.next_packet()
            accept(delivery, packet)
            open_packets.append(packet)
        self.assertEqual(delivery.confirmed_position(), '0')
        close1 = delivery.next_packet()  # 2 时帧首片在途，未确认
        delivery.record_reset(1)
        reloaded = reload(delivery)
        recovery = reloaded.next_packet()
        self.assertEqual(recovery['frame_kind'], RECOVERY_FRAME)
        self.assertEqual(recovery['time'], '0')
        self.assertEqual(len(recovery['operations']), 3)
        self.assertTrue(all(op['switch'] == OPEN
                            for op in recovery['operations']))
        # 旧代号在途回执无效。
        with self.assertRaises(ReceiptRejected):
            reloaded.receive_receipt('T-10', 0, close1['packet_index'])

    def test_no_key_recovery_skips_recovery_frame(self):
        # 确认位置之后所有键都已关闭：复位后无键可恢复，直接续送下一帧。
        s = score([note('a', 0, 1, 60, voice='A'),
                   note('b', 2, 1, 62, voice='B')])
        delivery = open_transfer('T-11', 0, s, {'A': 1, 'B': 1}, 64)
        p0 = delivery.next_packet()   # 0 时开 60
        accept(delivery, p0)
        p1 = delivery.next_packet()   # 1 时关 60
        accept(delivery, p1)
        self.assertEqual(delivery.confirmed_position(), '1')
        delivery.record_reset(1)
        nxt = delivery.next_packet()  # 无恢复帧，直接是 2 时奏谱帧
        self.assertEqual(nxt['frame_kind'], SCORE_FRAME)
        self.assertEqual(nxt['time'], '2')

    def test_reset_before_any_confirmation_skips_empty_recovery(self):
        # 尚无任何整帧确认时复位：初始位置无键，跳过恢复帧并从首帧开始。
        s = score([note('a', Fraction(1, 2), 1, 60, voice='A')])
        delivery = open_transfer('T-14', 0, s, {'A': 1}, 64)
        delivery.record_reset(1)
        reloaded = reload(delivery)
        packet = reloaded.next_packet()
        self.assertEqual(packet['frame_kind'], SCORE_FRAME)
        self.assertEqual(packet['time'], '1/2')
        self.assertEqual(packet['packet_index'], 1)
        # 无键恢复被跳过时相位已规范化为 score，游标指向首个奏谱帧。
        state = json.loads(reloaded.to_json())
        self.assertEqual(state['phase'], SCORE_FRAME)
        self.assertEqual(state['cursor']['frame_index'], 0)
        self.assertEqual(state['cursor']['next_fragment'], 1)
        # 空谱在确认前复位：仍然直接结束。
        empty = open_transfer('T-15', 0, score([]), {}, 64)
        empty.record_reset(1)
        self.assertIsNone(reload(empty).next_packet())

    def test_repeated_and_older_reset_notifications_do_not_roll_back(self):
        delivery = open_transfer('T-12', 0, acceptance_score(),
                                 ACCEPTANCE_CHANNELS, 2)
        p0 = delivery.next_packet()
        accept(delivery, p0)
        delivery.record_reset(1)
        snapshot = delivery.to_json()
        delivery.record_reset(1)  # 重复
        delivery.record_reset(0)  # 更旧
        self.assertEqual(delivery.to_json(), snapshot)
        packet = delivery.next_packet()
        accept(delivery, packet)
        delivery.record_reset(1)  # 恢复途中的同号复位不回退进度
        self.assertEqual(delivery.next_packet()['packet_index'], 2)


class ReceiptGuardTests(unittest.TestCase):
    """回执拒收矩阵与无副作用保证。"""

    def setUp(self):
        self.delivery = open_transfer(
            'T-13', 0, acceptance_score(), ACCEPTANCE_CHANNELS, 2)

    def assert_unchanged(self):
        self.assertEqual(self.delivery.to_json(), self.snapshot)

    def test_receipt_guards(self):
        packet = self.delivery.next_packet()
        self.snapshot = self.delivery.to_json()
        # 错误传送标识。
        with self.assertRaises(ReceiptRejected):
            self.delivery.receive_receipt('OTHER', 0, packet['packet_index'])
        self.assert_unchanged()
        # 尚未获知的未来代号。
        with self.assertRaises(ReceiptRejected):
            self.delivery.receive_receipt('T-13', 9, packet['packet_index'])
        self.assert_unchanged()
        # 未发出的包（序号超前）。
        with self.assertRaises(ReceiptRejected):
            self.delivery.receive_receipt('T-13', 0,
                                          packet['packet_index'] + 1)
        self.assert_unchanged()
        # 没有在途包时的任何回执都拒绝。
        accept(self.delivery, packet)
        with self.assertRaises(ReceiptRejected):
            self.delivery.receive_receipt('T-13', 0, 1)
        # 非法形状。
        with self.assertRaises(ReceiptRejected):
            self.delivery.receive_receipt({'transfer_id': 'T-13',
                                           'terminal_code': 0}, None, None)
        with self.assertRaises(ReceiptRejected):
            self.delivery.receive_receipt('T-13', 0, '1')
        with self.assertRaises(ReceiptRejected):
            self.delivery.receive_receipt('T-13', True, 2)

    def test_duplicate_receipt_for_confirmed_packet_is_idempotent(self):
        first = self.delivery.next_packet()
        accept(self.delivery, first)
        second = self.delivery.next_packet()
        snapshot = self.delivery.to_json()
        # 当前代号中已确认包的重复回执不改变状态。
        self.delivery.receive_receipt('T-13', 0, first['packet_index'])
        self.assertEqual(self.delivery.to_json(), snapshot)
        # 在途包自己的重复取包也不改变状态。
        self.assertEqual(self.delivery.next_packet(), second)

    def test_dict_form_receipt_accepted(self):
        packet = self.delivery.next_packet()
        self.delivery.receive_receipt(
            {'transfer_id': packet['transfer_id'],
             'terminal_code': packet['terminal_code'],
             'packet_index': packet['packet_index']})
        self.assertEqual(self.delivery.confirmed_position(), '0')

    def test_old_code_receipts_ignored_after_reset(self):
        p0 = self.delivery.next_packet()
        accept(self.delivery, p0)
        f1 = self.delivery.next_packet()
        accept(self.delivery, f1)
        f2 = self.delivery.next_packet()
        self.delivery.record_reset(1)
        snapshot = self.delivery.to_json()
        for index in (1, 2, f2['packet_index'], 99):
            with self.subTest(index=index):
                with self.assertRaises(ReceiptRejected):
                    self.delivery.receive_receipt('T-13', 0, index)
        self.assertEqual(self.delivery.to_json(), snapshot)


class ValidationTests(unittest.TestCase):
    """验收 3：空谱、非法输入、输入事件重排与检查点损坏。"""

    def test_empty_and_rest_only_scores(self):
        # 空事件表没有任何声部，映射为空；首次取包即结束。
        empty = open_transfer('T-20', 0, score([]), {}, 4)
        self.assertIsNone(empty.next_packet())
        self.assertIsNone(empty.confirmed_position())
        rest_only = score([rest('r', 0, 1)])
        # 纯休止声部不强制映射；给仅有休止的声部映射也允许。
        delivery = open_transfer('T-21', 0, rest_only, {'v1': 1}, 4)
        self.assertIsNone(delivery.next_packet())
        delivery2 = open_transfer('T-22', 0, rest_only, {}, 4)
        self.assertIsNone(delivery2.next_packet())
        # 空谱也可经检查点往返。
        self.assertEqual(reload(empty).to_json(), empty.to_json())

    def test_invalid_open_arguments(self):
        s = score([note('a', 0, 1)])
        cases = [
            ('bad id!', 0, s, {'v1': 1}, 4),             # 传送标识
            ('T', -1, s, {'v1': 1}, 4),                  # 负代号
            ('T', 1.0, s, {'v1': 1}, 4),                 # 非整数代号
            ('T', True, s, {'v1': 1}, 4),                # 布尔代号
            ('T', 0, s, {'v1': 1}, 0),                   # K 下界
            ('T', 0, s, {'v1': 1}, 65),                  # K 上界
            ('T', 0, s, {'v1': 1}, '2'),                 # K 非整数
            ('T', 0, s, {}, 4),                          # 空映射
            ('T', 0, s, {'v1': 0}, 4),                   # 通道越界
            ('T', 0, s, {'v1': 17}, 4),                  # 通道越界
            ('T', 0, s, {'v1': 1.0}, 4),                 # 通道非整数
            ('T', 0, s, {'ghost': 1}, 4),                # 谱外声部
        ]
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(InvalidTransfer):
                    open_transfer(*args)
        with self.assertRaises(InvalidTransfer):  # 非休止声部缺映射
            open_transfer('T', 0, s, {}, 4)
        with self.assertRaises(InvalidTransfer):  # 非 dict 映射
            open_transfer('T', 0, s, [('v1', 1)], 4)

    def test_overlap_overrun_invalid_tie_rejected(self):
        overlap = score([note('a', 0, 1), note('b', Fraction(9, 10), 1)])
        with self.assertRaises(InvalidTransfer):
            open_transfer('T', 0, overlap, {'v1': 1}, 4)
        overrun = score([note('a', 3, 2)])
        with self.assertRaises(InvalidTransfer):
            open_transfer('T', 0, overrun, {'v1': 1}, 4)
        bad_tie = score([note('a', 0, 1, tie_to='c'), note('c', 2, 1)])
        with self.assertRaises(InvalidTransfer):
            open_transfer('T', 0, bad_tie, {'v1': 1}, 4)

    def test_rest_only_voice_mapping_optional_sounding_required(self):
        s = score([note('a', 0, 1, voice='A'), rest('r', 1, 1, voice='R')])
        delivery = open_transfer('T-23', 0, s, {'A': 1}, 4)
        self.assertEqual(len(drain(delivery)), 2)
        with self.assertRaises(InvalidTransfer):  # 休止之外的发音声部缺失
            open_transfer('T-24', 0, s, {'R': 1}, 4)

    def test_input_event_reorder_does_not_change_frames(self):
        events = [note('a', 0, 2, 60, voice='A'),
                  note('b', 1, 2, 60, voice='B'),
                  note('c', 3, 1, 60, voice='C'),
                  note('d', 3, 1, 64, voice='D'),
                  note('e', 3, 1, 67, voice='E')]
        d1 = open_transfer('T-25', 0, score(events),
                           ACCEPTANCE_CHANNELS, 2)
        d2 = open_transfer('T-25', 0, score(list(reversed(events))),
                           ACCEPTANCE_CHANNELS, 2)
        # 输入事件重排不改变任何取到的包与确认位置。
        self.assertEqual(drain(d1), drain(d2))
        self.assertEqual(d1.confirmed_position(), d2.confirmed_position())

    def test_shared_channel_voices_and_transpose(self):
        # 两个声部共用通道 1；移调在解析期已生效。
        s = score([note('a', 0, 1, 60, voice='A'),
                   note('b', 0, 1, 62, voice='B')], transpose=2)
        delivery = open_transfer('T-26', 0, s, {'A': 1, 'B': 1}, 4)
        packet = delivery.next_packet()
        self.assertEqual([op['pitch'] for op in packet['operations']],
                         [62, 64])

    def test_checkpoint_roundtrip_and_corruption(self):
        delivery = open_transfer('T-27', 0, acceptance_score(),
                                 ACCEPTANCE_CHANNELS, 2)
        delivery.next_packet()  # 制造在途包
        blob = delivery.to_json()
        self.assertEqual(TerminalDelivery.from_json(blob).to_json(), blob)
        state = json.loads(blob)
        # 标准 JSON、固定字段集合，不累计历史。
        self.assertEqual(set(state), {
            'format', 'transfer_id', 'terminal_code', 'packet_limit',
            'score', 'channels', 'confirmed_until', 'phase', 'cursor',
            'packet_counter', 'outstanding'})
        broken_variants = [
            'not json', '{}',
            blob.replace('score-spans.terminal/1', 'other/2'),
            blob.replace('"packet_limit": 2', '"packet_limit": 0'),
            blob.replace('"terminal_code": 0', '"terminal_code": -1'),
        ]
        for broken in broken_variants:
            with self.subTest(broken=broken[:20]):
                with self.assertRaises(InvalidCheckpoint):
                    TerminalDelivery.from_json(broken)
        tampered = json.loads(blob)
        tampered['confirmed_until'] = 99
        with self.assertRaises(InvalidCheckpoint):
            TerminalDelivery.from_json(json.dumps(tampered))
        tampered2 = json.loads(blob)
        tampered2['channels']['A'] = 9
        with self.assertRaises(InvalidCheckpoint):
            TerminalDelivery.from_json(json.dumps(tampered2))
        tampered3 = json.loads(blob)
        tampered3['outstanding']['packet_index'] = 999
        with self.assertRaises(InvalidCheckpoint):
            TerminalDelivery.from_json(json.dumps(tampered3))
        tampered4 = json.loads(blob)
        tampered4['phase'] = 'mystery'
        with self.assertRaises(InvalidCheckpoint):
            TerminalDelivery.from_json(json.dumps(tampered4))

    def test_checkpoint_keeps_no_history_after_long_run(self):
        delivery = open_transfer('T-28', 0, acceptance_score(),
                                 ACCEPTANCE_CHANNELS, 2)
        terminal = FakeTerminal()
        packet = delivery.next_packet()
        while packet is not None:
            receipt = terminal.handle(packet)
            accept(delivery, receipt)
            packet = delivery.next_packet()
        delivery.record_reset(1)
        delivery.record_reset(2)
        state = json.loads(delivery.to_json())
        # 不保存已送包序列、过去代号或历次检查点。
        self.assertNotIn('history', state)
        self.assertNotIn('sent_packets', state)
        self.assertEqual(state['terminal_code'], 2)
        self.assertEqual(state['packet_counter'], 1)

    def test_error_codes_are_distinguishable(self):
        classes = (InvalidTransfer, ReceiptRejected, InvalidCheckpoint)
        codes = {cls.code for cls in classes}
        self.assertEqual(len(codes), len(classes))
        for cls in classes:
            self.assertTrue(issubclass(cls, Exception))
        self.assertTrue(issubclass(InvalidTransfer, ValueError))


class EndToEndFakeTerminalTests(unittest.TestCase):
    """用本地接收端替身跑完整交付，核对终端实际亮键。"""

    def test_full_delivery_against_fake_terminal(self):
        terminal = FakeTerminal()
        delivery = open_transfer('T-30', 0, acceptance_score(),
                                 ACCEPTANCE_CHANNELS, 2)
        packet = delivery.next_packet()
        while packet is not None:
            receipt = terminal.handle(packet)
            accept(delivery, receipt)
            packet = delivery.next_packet()
        self.assertIsNone(packet)
        self.assertEqual(delivery.confirmed_position(), '4')
        # 4 时帧关闭全部三键。
        self.assertEqual(terminal.held, set())

    def test_reset_recovery_against_fake_terminal(self):
        terminal = FakeTerminal()
        delivery = open_transfer('T-31', 0, acceptance_score(),
                                 ACCEPTANCE_CHANNELS, 2)
        p0 = delivery.next_packet()
        accept(delivery, terminal.handle(p0))
        delivery.record_reset(1)
        terminal.reset()
        packet = delivery.next_packet()
        while packet is not None:
            receipt = terminal.handle(packet)
            accept(delivery, receipt)
            packet = delivery.next_packet()
        # 恢复只亮 60，随后 3 时帧补齐 64/67，4 时全部关闭。
        self.assertEqual(terminal.held, set())
        kinds = [frame[2] for frame in terminal.executed_frames]
        self.assertEqual(kinds.count(RECOVERY_FRAME), 1)


if __name__ == '__main__':
    unittest.main()
