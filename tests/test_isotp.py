# -*- coding: utf-8 -*-
"""ISO 15765-2 (ISO-TP) 测试：PDU 编解码、BS/STmin、六个超时、异常处理。"""

import pytest

from canlab import isotp as I
from canlab.frame import Frame


class Clock(object):
    """可控虚拟时钟。"""

    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, delta):
        self.now += delta
        return self.now


def make_connection(**overrides):
    """建立一条带发送记录的连接。"""
    frames = []
    clock = Clock()
    config = I.IsoTpConfig(**overrides)
    conn = I.IsoTpConnection(lambda payload: frames.append(payload), config, clock)
    return conn, frames, clock


# ---------------------------------------------------------------------------
# STmin 编解码
# ---------------------------------------------------------------------------


class TestStmin(object):

    @pytest.mark.parametrize("value,expected", [
        (0x00, 0.0), (0x01, 1.0), (0x0A, 10.0), (0x7F, 127.0),
    ])
    def test_millisecond_range(self, value, expected):
        assert I.stmin_to_ms(value) == expected

    @pytest.mark.parametrize("value,expected", [
        (0xF1, 0.1), (0xF2, 0.2), (0xF5, 0.5), (0xF9, 0.9),
    ])
    def test_microsecond_range(self, value, expected):
        assert I.stmin_to_ms(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", [0x80, 0x90, 0xFA, 0xFF])
    def test_reserved_values_are_127ms(self, value):
        assert I.stmin_to_ms(value) == 127.0

    @pytest.mark.parametrize("ms,expected", [
        (0.0, 0x00), (1.0, 0x01), (10.0, 0x0A), (127.0, 0x7F), (200.0, 0x7F),
    ])
    def test_ms_to_stmin(self, ms, expected):
        assert I.ms_to_stmin(ms) == expected

    @pytest.mark.parametrize("ms", [0.1, 0.5, 0.9])
    def test_ms_to_stmin_microseconds(self, ms):
        encoded = I.ms_to_stmin(ms)
        assert 0xF1 <= encoded <= 0xF9
        assert I.stmin_to_ms(encoded) == pytest.approx(ms, abs=0.1)

    def test_ms_to_stmin_rejects_negative(self):
        with pytest.raises(ValueError):
            I.ms_to_stmin(-1)

    def test_resolve_stmin_matches_decode(self):
        assert I.resolve_stmin(0x20) == 32.0


# ---------------------------------------------------------------------------
# SF
# ---------------------------------------------------------------------------


class TestSingleFrame(object):

    def test_encode_sf_classic(self):
        assert I.encode_sf(b"\x10\x03") == b"\x02\x10\x03"

    def test_sf_max_classic(self):
        assert I.sf_max_data_length(False) == 7

    def test_sf_max_fd(self):
        assert I.sf_max_data_length(True) == 62

    def test_encode_sf_max_classic_length(self):
        payload = bytes(range(7))
        encoded = I.encode_sf(payload)
        assert encoded[0] == 0x07
        assert encoded[1:] == payload

    def test_encode_sf_too_long(self):
        with pytest.raises(I.IsoTpError) as info:
            I.encode_sf(bytes(8))
        assert info.value.kind == I.IsoTpErrorKind.LENGTH_MISMATCH

    def test_encode_sf_empty(self):
        with pytest.raises(I.IsoTpError):
            I.encode_sf(b"")

    def test_fd_escape_form(self):
        payload = bytes(range(8))
        encoded = I.encode_sf(payload, is_fd=True)
        assert encoded[0] == 0x00
        assert encoded[1] == 8
        assert encoded[2:] == payload

    def test_fd_short_form_still_length_nibble(self):
        encoded = I.encode_sf(b"\xAA\xBB", is_fd=True)
        assert encoded == b"\x02\xAA\xBB"

    def test_decode_sf(self):
        pdu = I.decode_pdu(b"\x03\x10\x03\x00")
        assert pdu.kind == I.N_PCI_SF
        assert pdu.length == 3
        assert pdu.data == b"\x10\x03\x00"

    def test_decode_sf_escape(self):
        pdu = I.decode_pdu(b"\x00\x08" + bytes(range(8)))
        assert pdu.kind == I.N_PCI_SF
        assert pdu.length == 8

    def test_decode_sf_length_mismatch(self):
        with pytest.raises(I.IsoTpError) as info:
            I.decode_pdu(b"\x05\x01\x02")
        assert info.value.kind == I.IsoTpErrorKind.LENGTH_MISMATCH

    def test_decode_sf_missing_length_byte(self):
        with pytest.raises(I.IsoTpError):
            I.decode_pdu(b"\x00")

    def test_single_frame_returns_payload(self):
        conn, frames, _clock = make_connection()
        result = conn.on_pdu(I.encode_sf(b"\x3E\x00"))
        assert result == b"\x3E\x00"

    def test_single_frame_records_event(self):
        conn, _frames, _clock = make_connection()
        conn.on_pdu(I.encode_sf(b"\x3E\x00"))
        assert any("received SF" in event for event in conn.events)


# ---------------------------------------------------------------------------
# FF 与 12/32 位长度
# ---------------------------------------------------------------------------

#: 12 位 FF 长度形式要求 payload[2] 为 0，否则与 32 位扩展形式同形
FF12_SAFE = bytes([0x00, 0x00, 0x00, 0x00]) + bytes([0xCC]) * 56


class TestFirstFrame(object):

    def test_encode_ff_12bit_header(self):
        encoded = I.encode_ff(FF12_SAFE)
        assert encoded[0] == 0x10
        assert encoded[1] == 60

    def test_encode_ff_takes_six_data_bytes(self):
        encoded = I.encode_ff(FF12_SAFE)
        assert len(encoded) == 8
        assert encoded[2:] == FF12_SAFE[:6]

    def test_encode_ff_32bit_requires_fd(self):
        with pytest.raises(I.IsoTpError):
            I.encode_ff(bytes(5000), is_fd=False)

    def test_encode_ff_32bit_header(self):
        encoded = I.encode_ff(bytes(5000), is_fd=True)
        assert encoded[0] == 0x10
        assert encoded[1] == 0x00
        assert int.from_bytes(encoded[2:6], "big") == 5000

    def test_decode_ff_12bit(self):
        pdu = I.decode_pdu(I.encode_ff(FF12_SAFE))
        assert pdu.kind == I.N_PCI_FF
        assert pdu.length == 60
        assert not pdu.extended_length

    def test_decode_ff_32bit(self):
        pdu = I.decode_pdu(I.encode_ff(bytes(5000), is_fd=True))
        assert pdu.length == 5000
        assert pdu.extended_length

    def test_ff_ambigram_detection(self):
        """12 位长度形式与 32 位形式同形时，解码器按长度自洽性区分。"""
        # payload[2] 非零 -> 32 位解释的值大于 12 位长度 -> 判为扩展形式
        encoded = I.encode_ff(bytes(5000), is_fd=True)
        pdu = I.decode_pdu(encoded)
        assert pdu.extended_length

    def test_ff_length_check(self):
        with pytest.raises(I.IsoTpError) as info:
            I.encode_ff(b"\x01", total_length=0)
        assert info.value.kind == I.IsoTpErrorKind.LENGTH_MISMATCH

    def test_ff_header_len_helper(self):
        assert I.ff_header_len(60, False) == 2
        assert I.ff_header_len(5000, True) == 6

    def test_ff_12bit_boundary(self):
        # 长度恰为 12 位表示上限 4095，且首帧数据不以非零字节开头
        payload = bytes([0x00] * 4) + bytes([0xDD]) * (I.FF_12BIT_MAX - 4)
        assert len(payload) == I.FF_12BIT_MAX
        encoded = I.encode_ff(payload)
        assert ((encoded[0] & 0x0F) << 8 | encoded[1]) == I.FF_12BIT_MAX

    def test_ff_above_12bit_uses_escape_on_fd(self):
        payload = bytes([0x00] * 4) + bytes([0xDD]) * (I.FF_12BIT_MAX + 10)
        encoded = I.encode_ff(payload, is_fd=True)
        assert encoded[1] == 0x00

    def test_ff_rejects_ambiguous_12bit_payload(self):
        """payload[2] 非零且整帧 >= 6 字节时会产生歧义，编码器应报错。"""
        ambiguous = bytes([0xAA, 0xBB, 0x00]) + bytes([0xCC]) * 57
        with pytest.raises(I.IsoTpError):
            I.encode_ff(ambiguous)


# ---------------------------------------------------------------------------
# CF
# ---------------------------------------------------------------------------


class TestConsecutiveFrame(object):

    def test_encode_cf_sequence(self):
        encoded = I.encode_cf(bytes(7), 3)
        assert encoded[0] == 0x23

    def test_sequence_wraps_at_16(self):
        assert I.encode_cf(bytes(7), 16)[0] == 0x20
        assert I.encode_cf(bytes(7), 17)[0] == 0x21

    def test_sequence_masked_to_4_bits(self):
        assert I.encode_cf(bytes(7), 0x1F)[0] == 0x2F

    def test_decode_cf(self):
        pdu = I.decode_pdu(I.encode_cf(bytes(range(7)), 5))
        assert pdu.kind == I.N_PCI_CF
        assert pdu.sequence == 5
        assert pdu.data == bytes(range(7))

    def test_cf_room_classic(self):
        assert len(I.encode_cf(bytes(20), 1)) == 8

    def test_cf_room_fd(self):
        assert len(I.encode_cf(bytes(100), 1, is_fd=True)) == 64


# ---------------------------------------------------------------------------
# FC
# ---------------------------------------------------------------------------


class TestFlowControl(object):

    def test_encode_fc_cts(self):
        assert I.encode_fc(I.FS_CTS, 8, 0x14) == b"\x30\x08\x14"

    def test_encode_fc_wait(self):
        assert I.encode_fc(I.FS_WAIT, 0, 0) == b"\x31\x00\x00"

    def test_encode_fc_overflow(self):
        assert I.encode_fc(I.FS_OVFLW, 0, 0) == b"\x32\x00\x00"

    def test_encode_fc_rejects_bad_status(self):
        with pytest.raises(I.IsoTpError):
            I.encode_fc(3)

    def test_encode_fc_rejects_bad_block_size(self):
        with pytest.raises(I.IsoTpError):
            I.encode_fc(I.FS_CTS, block_size=256)

    def test_encode_fc_rejects_bad_stmin(self):
        with pytest.raises(I.IsoTpError):
            I.encode_fc(I.FS_CTS, stmin=300)

    def test_decode_fc(self):
        pdu = I.decode_pdu(b"\x30\x08\x0A")
        assert pdu.kind == I.N_PCI_FC
        assert pdu.flow_status == I.FS_CTS
        assert pdu.block_size == 8
        assert pdu.stmin == 0x0A

    def test_decode_fc_too_short(self):
        with pytest.raises(I.IsoTpError):
            I.decode_pdu(b"\x30\x08")

    def test_fc_names(self):
        assert I.FC_NAMES[I.FS_CTS] == "CTS"
        assert I.FC_NAMES[I.FS_WAIT] == "WAIT"
        assert I.FC_NAMES[I.FS_OVFLW] == "OVFLW"


# ---------------------------------------------------------------------------
# 未知 PCI
# ---------------------------------------------------------------------------


class TestMalformedPdu(object):

    def test_unknown_pci_type(self):
        with pytest.raises(I.IsoTpError) as info:
            I.decode_pdu(b"\x40\x00")
        assert info.value.kind == I.IsoTpErrorKind.MALFORMED

    def test_empty_payload(self):
        with pytest.raises(I.IsoTpError):
            I.decode_pdu(b"")

    def test_kind_name_property(self):
        assert I.decode_pdu(b"\x01\xAA").kind_name == "SF"
        assert I.decode_pdu(b"\x30\x00\x00").kind_name == "FC"

    def test_pdu_repr(self):
        assert "FC" in repr(I.decode_pdu(b"\x30\x08\x14"))
        assert "CF" in repr(I.decode_pdu(b"\x21" + bytes(7)))


# ---------------------------------------------------------------------------
# 发送状态机
# ---------------------------------------------------------------------------


class TestSender(object):

    def test_short_payload_sends_sf(self):
        conn, frames, _clock = make_connection()
        transfer = conn.send(b"\x10\x03")
        assert transfer.finished
        assert len(frames) == 1
        assert frames[0][0] == 0x02

    def test_long_payload_sends_ff_and_waits(self):
        conn, frames, _clock = make_connection()
        transfer = conn.send(FF12_SAFE)
        assert len(frames) == 1
        assert transfer.waiting_for == "N_Bs"
        assert not transfer.finished

    def test_ff_advances_sent_length(self):
        conn, _frames, _clock = make_connection()
        transfer = conn.send(FF12_SAFE)
        assert transfer.sent_length == 6

    def test_second_send_while_busy_rejected(self):
        conn, _frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        with pytest.raises(I.IsoTpError):
            conn.send(FF12_SAFE)

    def test_fc_cts_clears_waiting(self):
        conn, _frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 0))
        assert conn.tx.waiting_for == ""

    def test_fc_wait_keeps_waiting(self):
        conn, _frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_WAIT, 0, 0))
        assert conn.tx.waiting_for == "N_Bs"

    def test_fc_overflow_aborts(self):
        conn, _frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        with pytest.raises(I.IsoTpError) as info:
            conn.on_pdu(I.encode_fc(I.FS_OVFLW, 0, 0))
        assert info.value.kind == I.IsoTpErrorKind.OVERFLOW

    def test_consecutive_frames_advance(self):
        conn, frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 0))
        more = conn.send_next_consecutive()
        assert more
        assert frames[-1][0] == 0x21

    def test_sequence_increments(self):
        conn, frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 0))
        conn.send_next_consecutive()
        conn.send_next_consecutive()
        assert frames[1][0] == 0x21
        assert frames[2][0] == 0x22

    def test_transfer_completes(self):
        conn, frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 0))
        while not conn.tx.finished:
            if not conn.send_next_consecutive():
                break
        assert conn.tx.sent_length == len(FF12_SAFE)

    def test_send_next_without_transfer(self):
        conn, _frames, _clock = make_connection()
        assert conn.send_next_consecutive() is False

    def test_block_size_limits_burst(self):
        conn, frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 3, 0))
        for _ in range(3):
            conn.send_next_consecutive()
        # 第 3 个连续帧发完后块满，必须等下一个 FC
        assert conn.tx.waiting_for == "N_Bs"
        assert len(frames) == 4             # FF + 3 个 CF

    def test_block_size_zero_means_unlimited(self):
        conn, _frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 0))
        for _ in range(8):
            if not conn.send_next_consecutive():
                break
        assert conn.tx.finished

    def test_stmin_recorded_from_fc(self):
        conn, _frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 0x14))
        assert conn.tx.stmin == 0x14

    def test_fc_without_transfer_rejected(self):
        conn, _frames, _clock = make_connection()
        with pytest.raises(I.IsoTpError) as info:
            conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 0))
        assert info.value.kind == I.IsoTpErrorKind.UNEXPECTED_PDU

    def test_frames_sent_counter(self):
        conn, frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        assert conn.frames_sent == 1
        assert len(frames) == 1

    def test_block_complete_event_logged(self):
        conn, _frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 2, 0))
        conn.send_next_consecutive()
        conn.send_next_consecutive()
        assert any("block complete" in event for event in conn.events)
        assert conn.tx.waiting_for == "N_Bs"

    def test_no_cf_sent_after_completion(self):
        conn, frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 0))
        while not conn.tx.finished:
            if not conn.send_next_consecutive():
                break
        before = len(frames)
        conn.send_next_consecutive()
        assert len(frames) == before


# ---------------------------------------------------------------------------
# 接收状态机
# ---------------------------------------------------------------------------


class TestReceiver(object):

    def test_single_frame_delivered(self):
        conn, _frames, _clock = make_connection()
        assert conn.on_pdu(I.encode_sf(b"\xAA\xBB")) == b"\xAA\xBB"

    def test_ff_starts_reception(self):
        conn, _frames, _clock = make_connection()
        conn.on_pdu(I.encode_ff(FF12_SAFE))
        assert conn.rx.started
        assert conn.rx.declared_length == len(FF12_SAFE)

    def test_ff_sends_flow_control(self):
        conn, frames, _clock = make_connection()
        conn.on_pdu(I.encode_ff(FF12_SAFE))
        assert frames and frames[0][0] == 0x30

    def test_full_reassembly(self):
        conn, _frames, _clock = make_connection()
        result = None
        for pdu in I.split_message(FF12_SAFE):
            got = conn.on_pdu(pdu)
            if got is not None:
                result = got
        assert result == FF12_SAFE

    def test_cf_without_ff_rejected(self):
        conn, _frames, _clock = make_connection()
        with pytest.raises(I.IsoTpError) as info:
            conn.on_pdu(I.encode_cf(bytes(7), 1))
        assert info.value.kind == I.IsoTpErrorKind.UNEXPECTED_PDU

    def test_wrong_sequence_number(self):
        conn, _frames, _clock = make_connection()
        conn.on_pdu(I.encode_ff(FF12_SAFE))
        with pytest.raises(I.IsoTpError) as info:
            conn.on_pdu(I.encode_cf(bytes(7), 5))
        assert info.value.kind == I.IsoTpErrorKind.WRONG_SN

    def test_duplicate_frame_wrong_sn(self):
        conn, _frames, _clock = make_connection()
        conn.on_pdu(I.encode_ff(FF12_SAFE))
        conn.on_pdu(I.encode_cf(bytes(7), 1))
        with pytest.raises(I.IsoTpError) as info:
            conn.on_pdu(I.encode_cf(bytes(7), 1))
        assert info.value.kind == I.IsoTpErrorKind.WRONG_SN

    def test_missing_frame_wrong_sn(self):
        conn, _frames, _clock = make_connection()
        conn.on_pdu(I.encode_ff(FF12_SAFE))
        conn.on_pdu(I.encode_cf(bytes(7), 1))
        with pytest.raises(I.IsoTpError) as info:
            conn.on_pdu(I.encode_cf(bytes(7), 3))
        assert info.value.kind == I.IsoTpErrorKind.WRONG_SN

    def test_sf_during_reception_rejected(self):
        conn, _frames, _clock = make_connection()
        conn.on_pdu(I.encode_ff(FF12_SAFE))
        with pytest.raises(I.IsoTpError):
            conn.on_pdu(I.encode_sf(b"\x01\x02"))

    def test_ff_during_reception_rejected(self):
        conn, _frames, _clock = make_connection()
        conn.on_pdu(I.encode_ff(FF12_SAFE))
        with pytest.raises(I.IsoTpError):
            conn.on_pdu(I.encode_ff(FF12_SAFE))

    def test_received_list_accumulates(self):
        conn, _frames, _clock = make_connection()
        conn.on_pdu(I.encode_sf(b"\x01"))
        conn.on_pdu(I.encode_sf(b"\x02"))
        assert len(conn.received) == 2

    def test_last_received_tracks(self):
        conn, _frames, _clock = make_connection()
        conn.on_pdu(I.encode_sf(b"\xAA"))
        assert conn.last_received == b"\xAA"

    def test_block_boundary_refc(self):
        conn, frames, _clock = make_connection()
        conn.config.block_size = 2
        conn.on_pdu(I.encode_ff(FF12_SAFE))
        conn.on_pdu(I.encode_cf(bytes(7), 1))
        conn.on_pdu(I.encode_cf(bytes(7), 2))
        # FF 时一帧 FC，块边界再一帧
        assert len([f for f in frames if f[0] == 0x30]) == 2

    def test_rx_error_recorded(self):
        conn, _frames, _clock = make_connection()
        conn.on_pdu(I.encode_ff(FF12_SAFE))
        try:
            conn.on_pdu(I.encode_cf(bytes(7), 9))
        except I.IsoTpError:
            pass
        assert conn.errors
        assert conn.errors[-1].kind == I.IsoTpErrorKind.WRONG_SN


# ---------------------------------------------------------------------------
# 六个超时
# ---------------------------------------------------------------------------


class TestTimeouts(object):

    def test_n_bs_pending_after_ff(self):
        conn, _frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        assert "N_Bs" in conn.timeout_names()

    def test_n_cs_pending_after_cf(self):
        conn, _frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 0))
        conn.send_next_consecutive()
        assert "N_Cs" in conn.timeout_names()

    def test_n_cr_pending_after_ff_received(self):
        conn, _frames, _clock = make_connection()
        conn.on_pdu(I.encode_ff(FF12_SAFE))
        assert "N_Cr" in conn.timeout_names()

    def test_n_bs_timeout_fires(self):
        conn, _frames, clock = make_connection(n_bs_ms=100.0)
        conn.send(FF12_SAFE)
        clock.advance(101.0)
        fired = conn.check_timeouts()
        assert "N_Bs" in fired
        assert conn.errors[-1].timeout_name == "N_Bs"

    def test_n_bs_not_fired_before_deadline(self):
        conn, _frames, clock = make_connection(n_bs_ms=100.0)
        conn.send(FF12_SAFE)
        clock.advance(99.0)
        assert conn.check_timeouts() == []

    def test_n_cs_timeout_fires(self):
        conn, _frames, clock = make_connection(n_cs_ms=50.0)
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 0))
        conn.send_next_consecutive()
        clock.advance(51.0)
        assert "N_Cs" in conn.check_timeouts()

    def test_n_cr_timeout_fires(self):
        conn, _frames, clock = make_connection(n_cr_ms=30.0)
        conn.on_pdu(I.encode_ff(FF12_SAFE))
        clock.advance(31.0)
        assert "N_Cr" in conn.check_timeouts()

    def test_timeout_aborts_transfer(self):
        conn, _frames, clock = make_connection(n_bs_ms=10.0)
        conn.send(FF12_SAFE)
        clock.advance(11.0)
        conn.check_timeouts()
        assert conn.tx.finished

    def test_timeout_error_kind(self):
        conn, _frames, clock = make_connection(n_bs_ms=10.0)
        conn.send(FF12_SAFE)
        clock.advance(11.0)
        conn.check_timeouts()
        assert conn.errors[-1].kind == I.IsoTpErrorKind.TIMEOUT

    def test_next_deadline_reported(self):
        conn, _frames, _clock = make_connection(n_bs_ms=100.0)
        conn.send(FF12_SAFE)
        assert conn.next_deadline_ms() == pytest.approx(100.0)

    def test_next_deadline_none_when_idle(self):
        conn, _frames, _clock = make_connection()
        assert conn.next_deadline_ms() is None

    def test_all_six_timeout_names_exist(self):
        config = I.IsoTpConfig()
        assert config.n_as_ms > 0
        assert config.n_ar_ms > 0
        assert config.n_bs_ms > 0
        assert config.n_br_ms > 0
        assert config.n_cs_ms > 0
        assert config.n_cr_ms > 0

    def test_default_timeout_values(self):
        assert I.DEFAULT_N_BS_MS == 1000.0
        assert I.DEFAULT_N_CR_MS == 1000.0

    def test_abort_clears_state(self):
        conn, _frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.abort("test")
        assert conn.tx.finished
        assert conn.errors[-1].kind == I.IsoTpErrorKind.ABORTED


# ---------------------------------------------------------------------------
# 分帧工具
# ---------------------------------------------------------------------------


class TestSplitMessage(object):

    @pytest.mark.parametrize("length", [1, 7])
    def test_short_payload_single_frame(self, length):
        assert len(I.split_message(bytes(length))) == 1

    def test_60_bytes_classic_is_nine_frames(self):
        assert len(I.split_message(FF12_SAFE)) == 9

    def test_100_bytes_classic_is_fifteen_frames(self):
        payload = bytes([0x00, 0x00, 0x00, 0x00]) + bytes([0xAA]) * 96
        assert len(I.split_message(payload)) == 15

    def test_fd_100_bytes_three_frames(self):
        payload = bytes([0x00, 0x00, 0x00, 0x00]) + bytes([0xAA]) * 96
        assert len(I.split_message(payload, is_fd=True)) == 3

    def test_frame_lengths_classic(self):
        pdus = I.split_message(FF12_SAFE)
        # 除最后一个 CF 外都是满 8 字节
        assert all(len(pdu) == 8 for pdu in pdus[:-1])
        assert len(pdus[-1]) <= 8

    def test_reassembly_roundtrip(self):
        conn, _frames, _clock = make_connection()
        result = None
        for pdu in I.split_message(FF12_SAFE):
            got = conn.on_pdu(pdu)
            if got is not None:
                result = got
        assert result == FF12_SAFE

    def test_max_payload_helpers(self):
        assert I.max_payload(False) == 8
        assert I.max_payload(True) == 64

    def test_frame_for_pads_classic(self):
        frame = I.frame_for(b"\x02\x10\x03", 0x7DF)
        assert len(frame.data) == 8
        assert frame.data[0] == 0x02

    def test_frame_for_pads_fd(self):
        frame = I.frame_for(b"\x02\x10\x03", 0x7DF, is_fd=True)
        assert len(frame.data) == 8

    def test_frame_for_sets_id(self):
        assert I.frame_for(b"\x02", 0x7E8).identifier == 0x7E8


# ---------------------------------------------------------------------------
# 端到端（含 STmin 实际耗时）
# ---------------------------------------------------------------------------


class TestEndToEnd(object):

    def test_full_transfer_with_stmin(self):
        conn, frames, clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 5))     # STmin = 5 ms
        ticks = 0
        while not conn.tx.finished and ticks < 100:
            clock.advance(5.0)
            ticks += 1
            if not conn.send_next_consecutive():
                break
        assert conn.tx.finished
        assert conn.tx.sent_length == len(FF12_SAFE)
        assert ticks == 8

    def test_stmin_elapsed_time(self):
        conn, _frames, clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 5))
        start = clock.now
        while not conn.tx.finished:
            clock.advance(5.0)
            if not conn.send_next_consecutive():
                break
        assert clock.now - start == pytest.approx(40.0)

    def test_stmin_zero_transfers_fast(self):
        conn, _frames, clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 0))
        while not conn.tx.finished:
            if not conn.send_next_consecutive():
                break
        assert clock.now == 0.0

    def test_block_size_forces_refc(self):
        conn, _frames, _clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 4, 0))
        for _ in range(4):
            conn.send_next_consecutive()
        assert conn.tx.waiting_for == "N_Bs"
        conn.on_pdu(I.encode_fc(I.FS_CTS, 4, 0))
        while not conn.tx.finished:
            if not conn.send_next_consecutive():
                break
        assert conn.tx.sent_length == len(FF12_SAFE)

    def test_frame_times_recorded(self):
        conn, _frames, clock = make_connection()
        conn.send(FF12_SAFE)
        conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 5))
        while not conn.tx.finished:
            clock.advance(5.0)
            if not conn.send_next_consecutive():
                break
        assert len(conn.frame_times) == 9
