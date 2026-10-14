# -*- coding: utf-8 -*-
"""错误处理测试：五类错误检测、TEC/REC 计数、状态机迁移、bus-off 恢复。"""

import pytest

from canlab import errors as E
from canlab.errors import (
    BUS_OFF_RECOVERY_SEQUENCES, BUS_OFF_RECOVERY_SEQUENCE_BITS, CanError,
    ERROR_PASSIVE_THRESHOLD, ErrorCounters, ErrorKind, ErrorState,
    TEC_BUS_OFF_THRESHOLD,
)
from canlab.frame import DOMINANT, RECESSIVE


# ---------------------------------------------------------------------------
# 常量与结构
# ---------------------------------------------------------------------------


class TestErrorConstants(object):

    def test_error_kinds(self):
        assert ErrorKind.BIT == "bit"
        assert ErrorKind.STUFF == "stuff"
        assert ErrorKind.CRC == "crc"
        assert ErrorKind.FORM == "form"
        assert ErrorKind.ACK == "ack"

    def test_five_error_kinds(self):
        assert len(ErrorKind.ALL) == 5

    def test_error_states(self):
        assert ErrorState.ERROR_ACTIVE == "error-active"
        assert ErrorState.ERROR_PASSIVE == "error-passive"
        assert ErrorState.BUS_OFF == "bus-off"
        assert len(ErrorState.ALL) == 3

    def test_thresholds(self):
        assert ERROR_PASSIVE_THRESHOLD == 127
        assert TEC_BUS_OFF_THRESHOLD == 255

    def test_recovery_constants(self):
        assert BUS_OFF_RECOVERY_SEQUENCES == 128
        assert BUS_OFF_RECOVERY_SEQUENCE_BITS == 11

    def test_can_error_repr(self):
        error = CanError(kind=ErrorKind.BIT, node="X", bit_position=3)
        assert "bit" in repr(error)


# ---------------------------------------------------------------------------
# 五类错误检测
# ---------------------------------------------------------------------------


class TestBitErrorDetection(object):

    def test_transmitter_recessive_reads_dominant(self):
        assert E.detect_bit_error(RECESSIVE, DOMINANT)

    def test_transmitter_dominant_reads_dominant(self):
        assert not E.detect_bit_error(DOMINANT, DOMINANT)

    def test_transmitter_dominant_reads_recessive(self):
        # 发送显性、读到隐性不是位错误（那是仲裁时的正常情况）
        assert not E.detect_bit_error(DOMINANT, RECESSIVE)

    def test_transmitter_recessive_reads_recessive(self):
        assert not E.detect_bit_error(RECESSIVE, RECESSIVE)

    def test_arbitration_field_difference_is_not_bit_error(self):
        assert not E.detect_bit_error(RECESSIVE, DOMINANT, in_arbitration=True)

    def test_receiver_recessive_reads_dominant_in_arbitration(self):
        assert E.detect_bit_error(RECESSIVE, DOMINANT, in_arbitration=True,
                                  is_receiver=True)

    def test_receiver_recessive_reads_dominant_in_ack_slot(self):
        assert E.detect_bit_error(RECESSIVE, DOMINANT, is_ack_slot=True,
                                  is_receiver=True)

    def test_receiver_outside_arbitration_not_bit_error(self):
        assert not E.detect_bit_error(RECESSIVE, DOMINANT, is_receiver=True)

    def test_receiver_dominant_reads_recessive_not_error(self):
        assert not E.detect_bit_error(DOMINANT, RECESSIVE, is_receiver=True,
                                      is_ack_slot=True)


class TestStuffErrorDetection(object):

    def test_six_identical_bits_is_stuff_error(self):
        assert E.detect_stuff_error([0] * 6) == 5

    def test_seven_identical_ones(self):
        assert E.detect_stuff_error([1] * 7) == 5

    def test_five_identical_bits_ok(self):
        assert E.detect_stuff_error([0] * 5) is None

    def test_alternating_bits_ok(self):
        assert E.detect_stuff_error([0, 1] * 8) is None

    def test_stuff_error_after_valid_prefix(self):
        bits = [0, 1, 0, 1, 0, 0, 0, 0, 0, 0]
        assert E.detect_stuff_error(bits) == 9

    def test_empty_stream(self):
        assert E.detect_stuff_error([]) is None


class TestCrcErrorDetection(object):

    def test_mismatch_detected(self):
        assert E.detect_crc_error(0x1234, 0x5678)

    def test_match_not_detected(self):
        assert not E.detect_crc_error(0x1234, 0x1234)

    def test_zero_match(self):
        assert not E.detect_crc_error(0, 0)


class TestFormErrorDetection(object):

    def test_crc_delimiter_must_be_recessive(self):
        assert E.detect_form_error(crc_delimiter=DOMINANT) == E.FormErrorKind.CRC_DELIMITER

    def test_ack_delimiter_must_be_recessive(self):
        assert E.detect_form_error(ack_delimiter=DOMINANT) == E.FormErrorKind.ACK_DELIMITER

    def test_eof_must_be_recessive(self):
        assert E.detect_form_error(eof_bits=[1, 1, 1, 1, 1, 1, 0]) == E.FormErrorKind.EOF

    def test_ifs_must_be_recessive(self):
        assert E.detect_form_error(ifs_bits=[1, 1, 0]) == E.FormErrorKind.IFS

    def test_valid_frame_no_form_error(self):
        assert E.detect_form_error(eof_bits=[1] * 7, ifs_bits=[1] * 3) is None

    def test_default_arguments_ok(self):
        assert E.detect_form_error() is None


class TestAckErrorDetection(object):

    def test_ack_slot_recessive_is_error(self):
        assert E.detect_ack_error(RECESSIVE)

    def test_ack_slot_dominant_ok(self):
        assert not E.detect_ack_error(DOMINANT)


# ---------------------------------------------------------------------------
# 计数器规则
# ---------------------------------------------------------------------------


class TestCounterRules(object):

    def test_tec_error_increments_by_8(self):
        assert E.next_tec(0, error=True, success=False) == 8

    def test_tec_success_decrements_by_1(self):
        assert E.next_tec(10, error=False, success=True) == 9

    def test_tec_success_floors_at_zero(self):
        assert E.next_tec(0, error=False, success=True) == 0

    def test_tec_unchanged_without_event(self):
        assert E.next_tec(20, error=False, success=False) == 20

    def test_tec_repeated_errors(self):
        tec = 0
        for _ in range(4):
            tec = E.next_tec(tec, error=True, success=False)
        assert tec == 32

    def test_rec_first_error_increments_by_8(self):
        assert E.next_rec(0, error=True, success=False) == 8

    def test_rec_error_after_flag_increments_by_1(self):
        assert E.next_rec(0, error=True, success=False, already_flagged=True) == 1

    def test_rec_success_decrements_by_1(self):
        assert E.next_rec(5, error=False, success=True) == 4

    def test_rec_floors_at_zero(self):
        assert E.next_rec(0, error=False, success=True) == 0

    def test_rec_clamped_at_127_for_error_active(self):
        assert E.next_rec(120, error=True, success=False) == 127

    def test_rec_not_clamped_when_already_flagged(self):
        assert E.next_rec(126, error=True, success=False,
                          already_flagged=True) == 127

    def test_error_state_active_default(self):
        assert E.error_state_for(0, 0) == ErrorState.ERROR_ACTIVE

    def test_error_state_passive_on_tec(self):
        assert E.error_state_for(128, 0) == ErrorState.ERROR_PASSIVE

    def test_error_state_passive_on_rec(self):
        assert E.error_state_for(0, 128) == ErrorState.ERROR_PASSIVE

    def test_error_state_boundary_tec_127(self):
        assert E.error_state_for(127, 0) == ErrorState.ERROR_ACTIVE

    def test_error_state_boundary_tec_128(self):
        assert E.error_state_for(128, 0) == ErrorState.ERROR_PASSIVE

    def test_error_state_bus_off(self):
        assert E.error_state_for(256, 0) == ErrorState.BUS_OFF

    def test_error_state_boundary_tec_255(self):
        assert E.error_state_for(255, 0) == ErrorState.ERROR_PASSIVE

    def test_error_state_bus_off_boundary_256(self):
        assert E.error_state_for(256, 0) == ErrorState.BUS_OFF


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


class TestErrorStateMachine(object):

    def test_initial_state(self):
        counters = ErrorCounters(node="X")
        assert counters.state == ErrorState.ERROR_ACTIVE
        assert counters.tec == 0
        assert counters.rec == 0

    def test_transmit_error_raises_tec(self):
        counters = ErrorCounters(node="X")
        counters.on_transmit_error(ErrorKind.BIT)
        assert counters.tec == 8

    def test_transmit_error_records_kind(self):
        counters = ErrorCounters(node="X")
        counters.on_transmit_error(ErrorKind.CRC, "test detail")
        assert counters.error_counts[ErrorKind.CRC] == 1

    def test_transmit_error_returns_record(self):
        counters = ErrorCounters(node="X")
        record = counters.on_transmit_error(ErrorKind.BIT)
        assert record.kind == ErrorKind.BIT
        assert record.tec_delta == 8

    def test_enter_error_passive_at_128(self):
        counters = ErrorCounters(node="X")
        for _ in range(16):
            counters.on_transmit_error(ErrorKind.BIT)
        assert counters.tec == 128
        assert counters.state == ErrorState.ERROR_PASSIVE

    def test_enter_bus_off_at_256(self):
        counters = ErrorCounters(node="X")
        for _ in range(32):
            counters.on_transmit_error(ErrorKind.BIT)
        assert counters.tec == 256
        assert counters.state == ErrorState.BUS_OFF

    def test_bus_off_sets_flag(self):
        counters = ErrorCounters(node="X")
        for _ in range(32):
            counters.on_transmit_error(ErrorKind.BIT)
        assert counters.is_bus_off

    def test_tec_peak_tracked(self):
        counters = ErrorCounters(node="X")
        for _ in range(10):
            counters.on_transmit_error(ErrorKind.BIT)
        counters.on_transmit_success()
        assert counters.tec_peak == 80

    def test_rec_peak_tracked(self):
        counters = ErrorCounters(node="X")
        counters.on_receive_error(ErrorKind.BIT)
        assert counters.rec_peak == 8

    def test_receive_error_raises_rec(self):
        counters = ErrorCounters(node="X")
        counters.on_receive_error(ErrorKind.CRC)
        assert counters.rec == 8
        assert counters.state == ErrorState.ERROR_ACTIVE

    def test_receive_error_flagged_delta_is_one(self):
        counters = ErrorCounters(node="X")
        record = counters.on_receive_error(ErrorKind.BIT, already_flagged=True)
        assert record.rec_delta == 1

    def test_passive_by_rec(self):
        counters = ErrorCounters(node="X")
        counters.on_receive_error(ErrorKind.BIT)
        counters.rec = 127
        counters.state = E.error_state_for(counters.tec, counters.rec)
        assert counters.rec == 127

    def test_transmit_success_decrements(self):
        counters = ErrorCounters(node="X")
        counters.tec = 10
        counters.on_transmit_success()
        assert counters.tec == 9

    def test_receive_success_decrements(self):
        counters = ErrorCounters(node="X")
        counters.rec = 10
        counters.on_receive_success()
        assert counters.rec == 9

    def test_success_noop_in_bus_off(self):
        counters = ErrorCounters(node="X")
        counters.force_bus_off()
        counters.on_transmit_success()
        assert counters.tec == 256

    def test_force_bus_off(self):
        counters = ErrorCounters(node="X")
        counters.force_bus_off()
        assert counters.is_bus_off
        assert counters.tec == 256

    def test_recover_from_bus_off_resets(self):
        counters = ErrorCounters(node="X")
        counters.force_bus_off()
        counters.recover_from_bus_off()
        assert counters.tec == 0
        assert counters.rec == 0
        assert counters.state == ErrorState.ERROR_ACTIVE

    def test_repr_contains_node(self):
        assert "X" in repr(ErrorCounters(node="X"))


# ---------------------------------------------------------------------------
# bus-off 恢复计时
# ---------------------------------------------------------------------------


class TestBusOffRecovery(object):

    def test_observe_idle_bit_ignored_when_active(self):
        counters = ErrorCounters(node="X")
        for _ in range(20000):
            counters.observe_bus_idle_bit(RECESSIVE)
        assert counters.recovery_sequences == 0

    def test_recovery_after_128_sequences(self):
        counters = ErrorCounters(node="X")
        counters.force_bus_off()
        recovered = False
        for _ in range(BUS_OFF_RECOVERY_SEQUENCES * BUS_OFF_RECOVERY_SEQUENCE_BITS):
            recovered = counters.observe_bus_idle_bit(RECESSIVE)
        assert recovered
        assert not counters.is_bus_off

    def test_recovery_requires_exact_bit_count(self):
        counters = ErrorCounters(node="X")
        counters.force_bus_off()
        total = BUS_OFF_RECOVERY_SEQUENCES * BUS_OFF_RECOVERY_SEQUENCE_BITS
        for _ in range(total - 1):
            counters.observe_bus_idle_bit(RECESSIVE)
        assert counters.is_bus_off
        counters.observe_bus_idle_bit(RECESSIVE)
        assert not counters.is_bus_off

    def test_dominant_bit_resets_progress(self):
        counters = ErrorCounters(node="X")
        counters.force_bus_off()
        for _ in range(64 * BUS_OFF_RECOVERY_SEQUENCE_BITS):
            counters.observe_bus_idle_bit(RECESSIVE)
        assert counters.recovery_sequences == 64
        counters.observe_bus_idle_bit(DOMINANT)
        assert counters.recovery_sequences == 0

    def test_dominant_bit_resets_partial_sequence(self):
        counters = ErrorCounters(node="X")
        counters.force_bus_off()
        for _ in range(5):
            counters.observe_bus_idle_bit(RECESSIVE)
        assert counters.idle_run == 5
        counters.observe_bus_idle_bit(DOMINANT)
        assert counters.idle_run == 0

    def test_disabled_auto_recovery_never_recovers(self):
        counters = ErrorCounters(node="X", auto_recovery=False)
        counters.force_bus_off()
        for _ in range(BUS_OFF_RECOVERY_SEQUENCES * BUS_OFF_RECOVERY_SEQUENCE_BITS * 2):
            assert not counters.observe_bus_idle_bit(RECESSIVE)
        assert counters.is_bus_off

    def test_recovery_clears_counters(self):
        counters = ErrorCounters(node="X")
        counters.force_bus_off()
        counters.rec = 50
        for _ in range(BUS_OFF_RECOVERY_SEQUENCES * BUS_OFF_RECOVERY_SEQUENCE_BITS):
            counters.observe_bus_idle_bit(RECESSIVE)
        assert counters.tec == 0
        assert counters.rec == 0

    def test_reset_idle_run(self):
        counters = ErrorCounters(node="X")
        counters.force_bus_off()
        for _ in range(5):
            counters.observe_bus_idle_bit(RECESSIVE)
        counters.reset_idle_run()
        assert counters.idle_run == 0

    def test_recovery_sequence_count_increments(self):
        counters = ErrorCounters(node="X")
        counters.force_bus_off()
        for _ in range(BUS_OFF_RECOVERY_SEQUENCE_BITS):
            counters.observe_bus_idle_bit(RECESSIVE)
        assert counters.recovery_sequences == 1

    def test_recovery_takes_1408_bits(self):
        assert BUS_OFF_RECOVERY_SEQUENCES * BUS_OFF_RECOVERY_SEQUENCE_BITS == 1408
