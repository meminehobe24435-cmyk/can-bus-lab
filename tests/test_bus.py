# -*- coding: utf-8 -*-
"""总线与逐位仲裁测试：仲裁、重发、统计、负载率、错误帧影响。"""

import pytest

from canlab.bus import (
    ARBITRATION_BITS_EXTENDED, ARBITRATION_BITS_STANDARD, BUS_IDLE, CanBus,
    DEFAULT_BITRATE, Node, bits_to_ms, bit_us,
)
from canlab.frame import DOMINANT, RECESSIVE, Frame


def bus_with(*names):
    """建一条总线并注册若干节点。"""
    bus = CanBus()
    for name in names:
        bus.add_node(name)
    return bus


# ---------------------------------------------------------------------------
# 基本构造
# ---------------------------------------------------------------------------


class TestBusConstruction(object):

    def test_default_bitrate(self):
        bus = CanBus()
        assert bus.bitrate == DEFAULT_BITRATE

    def test_bit_time(self):
        bus = CanBus(bitrate=500000)
        assert abs(bus.bit_time_ms - 0.002) < 1e-12

    def test_rejects_bad_bitrate(self):
        with pytest.raises(ValueError):
            CanBus(bitrate=0)
        with pytest.raises(ValueError):
            CanBus(bitrate=-1)

    def test_initial_state_idle(self):
        bus = CanBus()
        assert bus.bit_clock == 0
        assert bus.now_ms == 0.0
        assert bus.stats.frames_sent == 0

    def test_add_node(self):
        bus = CanBus()
        node = bus.add_node("ECU1")
        assert node.name == "ECU1"
        assert len(bus.nodes) == 1

    def test_add_duplicate_node_rejected(self):
        bus = CanBus()
        bus.add_node("ECU1")
        with pytest.raises(ValueError):
            bus.add_node("ECU1")

    def test_node_lookup(self):
        bus = bus_with("A", "B")
        assert bus.node("A").name == "A"

    def test_node_lookup_unknown(self):
        bus = bus_with("A")
        with pytest.raises(KeyError):
            bus.node("Z")

    def test_bits_to_ms_conversion(self):
        assert abs(bits_to_ms(500, 500000) - 1.0) < 1e-12

    def test_bit_us_conversion(self):
        assert abs(bit_us(500000) - 2.0) < 1e-9

    def test_bit_us_rejects_bad_bitrate(self):
        with pytest.raises(ValueError):
            bit_us(0)


# ---------------------------------------------------------------------------
# 单节点发送
# ---------------------------------------------------------------------------


class TestSingleNodeSend(object):

    def test_send_succeeds(self):
        bus = bus_with("A")
        result = bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        assert result.sent

    def test_send_increments_stats(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        assert bus.stats.frames_sent == 1
        assert bus.node("A").tx_success == 1

    def test_send_advances_virtual_time(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        assert bus.now_ms > 0

    def test_send_records_trace(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        assert len(bus.trace) == 1
        assert bus.trace[0].identifier == 0x100

    def test_send_sets_source(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.trace[0].source == "A"

    def test_send_unknown_node(self):
        bus = bus_with("A")
        with pytest.raises(KeyError):
            bus.send("Z", Frame(identifier=0x100))

    def test_other_nodes_receive(self):
        bus = bus_with("A", "B")
        bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        assert bus.node("B").rx_success == 1

    def test_sender_does_not_count_as_receiver(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.node("A").rx_success == 0

    def test_frame_timestamp_recorded(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.trace[0].timestamp > 0


# ---------------------------------------------------------------------------
# 逐位仲裁
# ---------------------------------------------------------------------------


class TestArbitration(object):

    def test_lower_id_wins(self):
        bus = bus_with("A", "B")
        results = bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        assert results[0].arbitration_won
        assert not results[1].arbitration_won

    def test_both_frames_eventually_sent(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        assert bus.stats.frames_sent == 2

    def test_loser_transmits_first(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        assert bus.trace[0].identifier == 0x100
        assert bus.trace[1].identifier == 0x200

    def test_loser_retransmits_once(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        assert bus.node("B").retransmissions == 1

    def test_loser_arbitration_count(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        assert bus.node("B").arbitration_lost == 1
        assert bus.node("A").arbitration_lost == 0

    def test_arbitration_record_fields(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        record = bus.arbitrations[0]
        assert record.winner == "A"
        assert record.winner_id == 0x100
        assert record.losers_names == ["B"]

    def test_arbitration_lost_bit_index(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        # 0x100 = 001 0000 0000，0x200 = 010 0000 0000，第 2 位分歧
        assert bus.arbitrations[0].losers[0][1] == 2

    @pytest.mark.parametrize("low,high,expected_bit", [
        (0x100, 0x200, 2),
        (0x000, 0x001, 11),
        (0x001, 0x002, 10),
        (0x000, 0x400, 1),
        (0x7FE, 0x7FF, 11),
    ])
    def test_arbitration_bit_position(self, low, high, expected_bit):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=low, data=b"\x01")),
            ("B", Frame(identifier=high, data=b"\x02")),
        ])
        assert bus.arbitrations[0].losers[0][1] == expected_bit

    def test_data_frame_beats_remote_frame(self):
        bus = bus_with("D", "R")
        bus.contend([
            ("D", Frame(identifier=0x123, data=b"\x01")),
            ("R", Frame.remote_frame(0x123, 1)),
        ])
        assert bus.arbitrations[0].winner == "D"

    def test_remote_frame_loses_at_rtr_bit(self):
        bus = bus_with("D", "R")
        bus.contend([
            ("D", Frame(identifier=0x123, data=b"\x01")),
            ("R", Frame.remote_frame(0x123, 1)),
        ])
        # 标准帧：SOF(1) + ID(11) + IDE(1) = 13 位之后是 RTR
        assert bus.arbitrations[0].losers[0][1] == 13

    def test_extended_wins_over_standard_with_same_base(self):
        """基本 ID 相同时，扩展帧的 SRR(隐性)+IDE(显性) 让它取胜。"""
        bus = bus_with("STD", "EXT")
        bus.contend([
            ("STD", Frame(identifier=0x123, data=b"\x01")),
            ("EXT", Frame(identifier=0x123, data=b"\x02", extended=True)),
        ])
        assert bus.arbitrations[0].winner == "EXT"

    def test_extended_wins_at_ide_position(self):
        bus = bus_with("STD", "EXT")
        bus.contend([
            ("STD", Frame(identifier=0x123, data=b"\x01")),
            ("EXT", Frame(identifier=0x123, data=b"\x02", extended=True)),
        ])
        assert bus.arbitrations[0].losers[0][1] == 3

    def test_lower_extended_id_wins(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x00001000, data=b"\x01", extended=True)),
            ("B", Frame(identifier=0x00002000, data=b"\x02", extended=True)),
        ])
        assert bus.arbitrations[0].winner == "A"

    def test_three_nodes_arbitration(self):
        bus = bus_with("A", "B", "C")
        bus.contend([
            ("A", Frame(identifier=0x300, data=b"\x01")),
            ("B", Frame(identifier=0x100, data=b"\x02")),
            ("C", Frame(identifier=0x200, data=b"\x03")),
        ])
        assert bus.arbitrations[0].winner == "B"
        assert len(bus.arbitrations[0].losers) == 2

    def test_three_nodes_all_frames_sent(self):
        bus = bus_with("A", "B", "C")
        bus.contend([
            ("A", Frame(identifier=0x300, data=b"\x01")),
            ("B", Frame(identifier=0x100, data=b"\x02")),
            ("C", Frame(identifier=0x200, data=b"\x03")),
        ])
        identifiers = [frame.identifier for frame in bus.trace]
        assert identifiers == [0x100, 0x200, 0x300]

    def test_same_id_arbitration_recorded(self):
        # 同 ID 时仲裁场完全一致，两个节点都要走完仲裁场；
        # 本实现在仲裁场内的位比较上不分胜负，因此两者都进入发送。
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x123, data=b"\x01")),
            ("B", Frame(identifier=0x123, data=b"\x02")),
        ])
        assert bus.stats.frames_sent == 2
        # 同 ID 时仲裁场不分胜负，两个节点都能发出去；其中一个会被记一次
        # "重发"（因为总线上的字节流互为显性/隐性的组合仍有一次让位）
        assert bus.node("A").tx_success == 1
        assert bus.node("B").tx_success == 1
        assert bus.node("A").retransmissions + bus.node("B").retransmissions <= 1

    def test_arbitration_events_counted(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        assert bus.stats.arbitration_events == 1

    def test_arbitration_losses_counted(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        assert bus.stats.arbitration_losses == 1

    def test_retransmissions_counted(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        assert bus.stats.retransmissions == 1

    def test_lost_at_bit_reported(self):
        bus = bus_with("A", "B")
        results = bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        assert results[1].lost_at_bit == 2
        assert results[1].winner == "A"

    def test_arbitration_start_time_recorded(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        assert bus.arbitrations[0].start_ms == 0.0

    def test_arbitration_record_identifiers(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        assert sorted(bus.arbitrations[0].identifiers) == [0x100, 0x200]

    def test_log_records_arbitration(self):
        bus = bus_with("A", "B")
        bus.contend([
            ("A", Frame(identifier=0x100, data=b"\x01")),
            ("B", Frame(identifier=0x200, data=b"\x02")),
        ])
        assert any("arbitration" in line for line in bus.log)

    def test_arbitration_constants(self):
        assert ARBITRATION_BITS_STANDARD == 13
        assert ARBITRATION_BITS_EXTENDED == 32


# ---------------------------------------------------------------------------
# 总线负载与统计
# ---------------------------------------------------------------------------


class TestBusStatistics(object):

    def test_load_percent_zero_when_idle(self):
        bus = CanBus()
        assert bus.stats.load_percent == 0.0

    def test_load_percent_full_when_back_to_back(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        assert bus.stats.load_percent == pytest.approx(100.0)

    def test_load_percent_with_idle_gap(self):
        bus = CanBus(min_gap_bits=100)
        bus.add_node("A")
        bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        assert bus.stats.load_percent < 100.0

    def test_idle_bits_counted(self):
        bus = bus_with("A")
        bus.idle(50)
        assert bus.stats.idle_bits == 50

    def test_idle_advances_time(self):
        bus = CanBus()
        bus.idle(500)
        assert abs(bus.now_ms - 1.0) < 1e-12

    def test_idle_ms_conversion(self):
        bus = CanBus()
        bits = bus.idle_ms(10.0)
        assert bits == 5000

    def test_idle_ms_rounds_up(self):
        bus = CanBus()
        bits = bus.idle_ms(1.1)
        assert bits == 550

    def test_idle_ms_rejects_negative(self):
        bus = CanBus()
        with pytest.raises(ValueError):
            bus.idle_ms(-1)

    def test_idle_bits_for_does_not_advance(self):
        bus = CanBus()
        bits = bus.idle_bits_for(5.0)
        assert bits == 2500
        assert bus.now_ms == 0.0

    def test_advance_to_ms(self):
        bus = CanBus()
        bus.advance_to_ms(5.0)
        assert abs(bus.now_ms - 5.0) < 1e-9

    def test_advance_to_ms_past_target(self):
        bus = CanBus()
        bus.advance_to_ms(5.0)
        assert bus.advance_to_ms(1.0) == 0

    def test_advance_ms(self):
        bus = CanBus()
        bus.advance_ms(2.0)
        assert abs(bus.now_ms - 2.0) < 1e-9

    def test_node_stats_snapshot(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        stats = bus.node_stats()
        assert stats["A"]["tx_success"] == 1
        assert stats["A"]["state"] == "error-active"

    def test_worst_case_response_single_frame(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.worst_case_response_ms("A") == 0.0

    def test_frame_gap_ms(self):
        bus = CanBus(min_gap_bits=100)
        bus.add_node("A")
        bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        bus.send("A", Frame(identifier=0x101, data=b"\x02"))
        gaps = bus.frame_gap_ms()
        assert len(gaps) == 1
        assert gaps[0] > 0

    def test_reset_clears_stats(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x100))
        bus.reset()
        assert bus.stats.frames_sent == 0
        assert bus.now_ms == 0.0
        assert bus.node("A").tx_success == 0

    def test_reset_clears_counters(self):
        bus = bus_with("A")
        bus.inject_bit_error("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.node("A").tec == 8
        bus.reset()
        assert bus.node("A").tec == 0

    def test_bus_idle_constant(self):
        assert BUS_IDLE == RECESSIVE


# ---------------------------------------------------------------------------
# 错误注入
# ---------------------------------------------------------------------------


class TestErrorInjection(object):

    def test_bit_error_injection(self):
        bus = bus_with("A")
        bus.inject_bit_error("A", at_bit=20)
        bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        assert bus.node("A").tec == 8

    def test_bit_error_prevents_delivery(self):
        bus = bus_with("A", "B")
        bus.inject_bit_error("A", at_bit=20)
        bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        assert bus.stats.frames_sent == 0
        assert bus.node("B").rx_success == 0

    def test_crc_error_injection(self):
        bus = bus_with("A")
        bus.inject_crc_error("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.node("A").counters.error_counts.get("crc") == 1

    def test_stuff_error_injection(self):
        bus = bus_with("A")
        bus.inject_stuff_error("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.node("A").counters.error_counts.get("stuff") == 1

    def test_form_error_injection(self):
        bus = bus_with("A")
        bus.inject_form_error("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.node("A").counters.error_counts.get("form") == 1

    def test_ack_error_injection(self):
        bus = bus_with("A")
        bus.inject_ack_error("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.node("A").counters.error_counts.get("ack") == 1

    def test_error_frame_counted(self):
        bus = bus_with("A")
        bus.inject_bit_error("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.stats.error_frames == 1
        assert bus.node("A").error_frames_sent == 1

    def test_receivers_count_receive_error(self):
        bus = bus_with("A", "B")
        bus.inject_bit_error("A")
        bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        assert bus.node("B").rec > 0

    def test_injection_is_one_shot(self):
        bus = bus_with("A")
        bus.inject_bit_error("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.node("A").tec == 8
        # 注入只用一次：第二次发送成功，TEC -= 1
        bus.send("A", Frame(identifier=0x101))
        assert bus.node("A").tec == 7

    def test_injection_then_success_decrements(self):
        bus = bus_with("A")
        bus.inject_bit_error("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.node("A").tec == 8
        bus.send("A", Frame(identifier=0x101))
        assert bus.node("A").tec == 7

    def test_clear_injections(self):
        bus = bus_with("A")
        bus.inject_bit_error("A")
        bus.clear_injections()
        bus.send("A", Frame(identifier=0x100))
        assert bus.node("A").tec == 0

    def test_injection_does_not_change_other_nodes_tec(self):
        # 错误帧会破坏当前帧，其它节点作为接收方只计 REC，不动 TEC
        bus = bus_with("A", "B")
        bus.inject_bit_error("A")
        bus.send("A", Frame(identifier=0x100))
        assert bus.node("B").tec == 0
        assert bus.node("B").rec == 1

    def test_repeated_injection_reaches_bus_off(self):
        bus = bus_with("A")
        for index in range(32):
            bus.inject_bit_error("A")
            bus.send("A", Frame(identifier=0x100, data=bytes([index & 0xFF])))
        assert bus.node("A").counters.is_bus_off

    def test_bus_off_node_rejects_send(self):
        bus = bus_with("A")
        bus.node("A").counters.force_bus_off()
        result = bus.send("A", Frame(identifier=0x100))
        assert not result.sent
        assert len(result.errors) == 1


# ---------------------------------------------------------------------------
# trace 导出 / 载入
# ---------------------------------------------------------------------------


class TestTraceExchange(object):

    def test_export_trace_fields(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x100, data=b"\x01\x02"))
        entry = bus.export_trace()[0]
        assert entry["id"] == 0x100
        assert entry["data"] == [1, 2]
        assert entry["src"] == "A"

    def test_trace_roundtrip(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x100, data=b"\x01\x02"))
        entries = bus.export_trace()
        frames = CanBus.load_trace(entries)
        assert frames[0].identifier == 0x100
        assert frames[0].data == b"\x01\x02"

    def test_trace_roundtrip_extended(self):
        bus = bus_with("A")
        bus.send("A", Frame(identifier=0x1ABCDEF, data=b"\x01", extended=True))
        frames = CanBus.load_trace(bus.export_trace())
        assert frames[0].extended

    def test_load_trace_empty(self):
        assert CanBus.load_trace([]) == []

    def test_export_trace_empty(self):
        assert CanBus().export_trace() == []


# ---------------------------------------------------------------------------
# 节点对象
# ---------------------------------------------------------------------------


class TestNodeObject(object):

    def test_node_defaults(self):
        node = Node(name="X")
        assert node.tec == 0
        assert node.rec == 0
        assert node.error_state == "error-active"

    def test_node_counter_binding(self):
        node = Node(name="X")
        assert node.counters.node == "X"

    def test_node_repr(self):
        assert "X" in repr(Node(name="X"))
