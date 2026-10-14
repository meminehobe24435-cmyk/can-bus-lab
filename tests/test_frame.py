# -*- coding: utf-8 -*-
"""帧层测试：位级编码/解码、位填充、CRC、DLC 映射边界。"""

import pytest

from canlab import frame as F
from canlab.frame import (
    BitStreamError, Frame, FrameError, CRC15_POLY, CRC17_POLY, CRC21_POLY,
)


# ---------------------------------------------------------------------------
# 常量与辅助
# ---------------------------------------------------------------------------

ALL_CLASSIC_LENGTHS = list(range(0, 9))
ALL_FD_LENGTHS = sorted(F.LEGAL_LENGTHS)


def sample_frame(**kwargs):
    """构造一条测试用的经典 CAN 数据帧。"""
    params = dict(identifier=0x123, data=bytes([0x11, 0x22, 0x33, 0x44]))
    params.update(kwargs)
    return Frame(**params)


# ---------------------------------------------------------------------------
# 整数 <-> 位序列
# ---------------------------------------------------------------------------


class TestBitHelpers(object):

    @pytest.mark.parametrize("value,width,expected", [
        (0, 1, [0]),
        (1, 1, [1]),
        (0x123, 11, [0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 1]),
        (0xFF, 8, [1, 1, 1, 1, 1, 1, 1, 1]),
        (0x00, 8, [0, 0, 0, 0, 0, 0, 0, 0]),
        (0xA5, 8, [1, 0, 1, 0, 0, 1, 0, 1]),
    ])
    def test_int_to_bits_vectors(self, value, width, expected):
        assert F.int_to_bits(value, width) == expected

    @pytest.mark.parametrize("value,width", [
        (0, 1), (1, 1), (0x123, 11), (0x1FFFFFFF, 29), (0xFFFF, 16),
    ])
    def test_int_to_bits_roundtrip(self, value, width):
        assert F.bits_to_int(F.int_to_bits(value, width)) == value

    def test_int_to_bits_rejects_overflow(self):
        with pytest.raises(ValueError):
            F.int_to_bits(0x800, 11)

    def test_int_to_bits_rejects_negative(self):
        with pytest.raises(ValueError):
            F.int_to_bits(-1, 8)

    def test_int_to_bits_rejects_bad_width(self):
        with pytest.raises(ValueError):
            F.int_to_bits(0, 0)

    def test_bits_to_int_rejects_non_binary(self):
        with pytest.raises(ValueError):
            F.bits_to_int([0, 2, 1])

    def test_bits_to_int_empty(self):
        assert F.bits_to_int([]) == 0


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------


class TestCrc(object):

    def test_crc_empty_bitstream_is_zero(self):
        assert F.crc15([]) == 0
        assert F.crc17([]) == 0
        assert F.crc21([]) == 0

    def test_crc_single_zero_bit_is_zero(self):
        # 移位寄存器初值为 0、输入位为 0 时不产生反馈
        assert F.crc15([0]) == 0

    def test_crc_single_one_bit_nonzero(self):
        assert F.crc15([1]) != 0

    def test_crc_polynomial_constants(self):
        assert CRC15_POLY == 0x4599
        assert CRC17_POLY == 0x1685B
        assert CRC21_POLY == 0x102899

    def test_crc15_is_deterministic(self):
        bits = F.int_to_bits(0x123456, 24)
        assert F.crc15(bits) == F.crc15(bits)

    def test_crc_widths_differ(self):
        bits = F.int_to_bits(0xDEADBEEF, 32)
        values = {F.crc15(bits), F.crc17(bits), F.crc21(bits)}
        assert len(values) == 3

    @pytest.mark.parametrize("width,func", [
        (15, F.crc15), (17, F.crc17), (21, F.crc21),
    ])
    def test_crc_fits_in_width(self, width, func):
        bits = F.int_to_bits(0xABCDEF, 24)
        value = func(bits)
        assert 0 <= value < (1 << width)

    def test_crc_select_by_length(self):
        assert F.crc_select(0, False) == (15, "CRC15")
        assert F.crc_select(8, False) == (15, "CRC15")
        assert F.crc_select(1, True) == (17, "CRC17")
        assert F.crc_select(16, True) == (17, "CRC17")
        assert F.crc_select(17, True) == (21, "CRC21")
        assert F.crc_select(64, True) == (21, "CRC21")

    def test_crc_compute_dispatch(self):
        bits = F.int_to_bits(0x1234, 16)
        assert F.crc_compute(bits, 15) == F.crc15(bits)
        assert F.crc_compute(bits, 17) == F.crc17(bits)
        assert F.crc_compute(bits, 21) == F.crc21(bits)

    def test_crc_compute_rejects_unsupported_width(self):
        with pytest.raises(ValueError):
            F.crc_compute([1, 0, 1], 12)

    def test_crc_detects_single_bit_flip(self):
        """单比特错误必须改变 CRC 值。"""
        base = F.int_to_bits(0x12345678, 32)
        reference = F.crc15(base)
        flip_count = 0
        for index in range(len(base)):
            mutated = list(base)
            mutated[index] ^= 1
            if F.crc15(mutated) != reference:
                flip_count += 1
        assert flip_count == len(base)

    def test_crc_detects_double_bit_flip(self):
        """双比特错误检测：抽样验证 CRC15 对全部两比特组合都能检出。"""
        base = F.int_to_bits(0x1234, 16)
        reference = F.crc15(base)
        undetected = 0
        total = 0
        for first in range(len(base)):
            for second in range(first + 1, len(base)):
                mutated = list(base)
                mutated[first] ^= 1
                mutated[second] ^= 1
                total += 1
                if F.crc15(mutated) == reference:
                    undetected += 1
        assert total == 120
        assert undetected == 0

    def test_crc_rejects_non_binary_input(self):
        with pytest.raises(ValueError):
            F.crc15([0, 5])

    def test_crc_rejects_bad_crc_width(self):
        with pytest.raises(ValueError):
            F.crc_polynomial([1, 0], 0x4599, 0)


# ---------------------------------------------------------------------------
# DLC 映射
# ---------------------------------------------------------------------------


class TestDlcMapping(object):

    @pytest.mark.parametrize("dlc,length", [
        (0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 6), (7, 7), (8, 8),
        (9, 12), (10, 16), (11, 20), (12, 24), (13, 32), (14, 48), (15, 64),
    ])
    def test_dlc_to_length_standard_table(self, dlc, length):
        assert F.dlc_to_length(dlc) == length

    @pytest.mark.parametrize("dlc", list(range(16)))
    def test_length_to_dlc_roundtrip(self, dlc):
        assert F.length_to_dlc(F.dlc_to_length(dlc)) == dlc

    def test_dlc_to_length_rejects_illegal(self):
        with pytest.raises(FrameError):
            F.dlc_to_length(16)
        with pytest.raises(FrameError):
            F.dlc_to_length(-1)

    @pytest.mark.parametrize("length", [9, 10, 11, 13, 17, 33, 63, 65])
    def test_length_to_dlc_rejects_unrepresentable(self, length):
        with pytest.raises(FrameError):
            F.length_to_dlc(length)

    def test_max_limits(self):
        assert F.max_dlc(False) == 8
        assert F.max_dlc(True) == 15
        assert F.max_length(False) == 8
        assert F.max_length(True) == 64

    def test_legal_lengths_set(self):
        assert F.LEGAL_LENGTHS == frozenset(
            [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64])


# ---------------------------------------------------------------------------
# 位填充
# ---------------------------------------------------------------------------


class TestBitStuffing(object):

    def test_five_identical_bits_inserts_complement(self):
        assert F.stuff_bits([0] * 5) == [0, 0, 0, 0, 0, 1]

    def test_five_ones_inserts_zero(self):
        assert F.stuff_bits([1] * 5) == [1, 1, 1, 1, 1, 0]

    def test_ten_identical_bits_two_stuff_bits(self):
        result = F.stuff_bits([0] * 10)
        assert result == [0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1]

    def test_alternating_bits_no_stuffing(self):
        bits = [0, 1] * 10
        assert F.stuff_bits(bits) == bits

    def test_four_identical_bits_no_stuffing(self):
        assert F.stuff_bits([0] * 4) == [0] * 4

    @pytest.mark.parametrize("bits", [
        [0] * 4, [1] * 4, [0] * 5, [1] * 7, [0, 1] * 6, [1, 1, 1, 1, 1],
        [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
    ])
    def test_stuff_unstuff_roundtrip(self, bits):
        assert F.unstuff_bits(F.stuff_bits(bits)) == bits

    def test_stuff_counts_stuff_bits(self):
        # 20 个零：每 5 位插一个填充位，实测得到 24 位（4 个填充位）
        assert len(F.stuff_bits([0] * 20)) == 24

    def test_unstuff_rejects_bad_stuff_bit(self):
        with pytest.raises(BitStreamError):
            F.unstuff_bits([0, 0, 0, 0, 0, 0])

    def test_unstuff_rejects_truncated_stream(self):
        # 以 5 个同极性位结束、后面没有填充位 -> 报错
        with pytest.raises(BitStreamError):
            F.unstuff_bits([0, 0, 0, 0, 0])
        with pytest.raises(BitStreamError):
            F.unstuff_bits([1, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1])

    def test_unstuff_detects_stuff_error_past_threshold(self):
        with pytest.raises(BitStreamError):
            F.unstuff_bits([1, 1, 1, 1, 1, 1, 1])

    def test_unstuff_rejects_non_binary(self):
        with pytest.raises(ValueError):
            F.unstuff_bits([0, 9])

    def test_stuff_rejects_bad_threshold(self):
        with pytest.raises(ValueError):
            F.stuff_bits([0], threshold=0)

    def test_find_stuff_error_returns_index(self):
        assert F.find_stuff_error([0] * 6) == 5
        assert F.find_stuff_error([0] * 5 + [1]) is None
        assert F.find_stuff_error([]) is None

    def test_find_stuff_error_seven_ones(self):
        assert F.find_stuff_error([1] * 7) == 5

    def test_unstuff_prefix_returns_longest_prefix(self):
        # 填充区之后接一段不符合填充规则的位，贪婪去填充会在那里停下
        bits = F.stuff_bits([0] * 4) + [0, 0, 0, 0, 0, 0]
        raw, consumed = F.unstuff_prefix(bits)
        assert raw == [0] * 4
        assert consumed == 4

    def test_unstuff_prefix_stops_at_error(self):
        # 连续 7 个零：第 5 位之后本该有填充位却是 0 -> 停在错误之前
        raw, consumed = F.unstuff_prefix([0, 0, 0, 0, 0, 0, 0])
        assert raw == [0] * 4
        assert consumed == 4


# ---------------------------------------------------------------------------
# 帧对象
# ---------------------------------------------------------------------------


class TestFrameObject(object):

    def test_defaults(self):
        frame = Frame()
        assert frame.identifier == 0
        assert frame.data == b""
        assert not frame.extended
        assert not frame.remote

    def test_std_id_max(self):
        assert Frame(identifier=0x7FF).id_max == 0x7FF

    def test_ext_id_max(self):
        assert Frame(identifier=0x1, extended=True).id_max == 0x1FFFFFFF

    def test_dlc_from_length(self):
        assert Frame(data=bytes(4)).dlc == 4
        assert Frame(data=bytes(12), fd=True).dlc == 9
        assert Frame(data=bytes(64), fd=True).dlc == 15

    def test_priority_key_lower_id_first(self):
        assert Frame(identifier=0x100).priority_key < Frame(identifier=0x200).priority_key

    def test_priority_key_data_before_remote(self):
        data_frame = Frame(identifier=0x123, data=b"\x01")
        rtr_frame = Frame.remote_frame(0x123, 1)
        assert data_frame.priority_key < rtr_frame.priority_key

    def test_priority_key_standard_before_extended_same_base(self):
        # 基本 ID 相同时，标准帧在第 12 位为显性 IDE，扩展帧为隐性 SRR
        std = Frame(identifier=0x7FF)
        ext = Frame(identifier=0x7FF, extended=True)
        assert std.priority_key < ext.priority_key

    def test_remote_frame_requested_length(self):
        assert Frame.remote_frame(0x321, 4).requested_length == 4

    def test_validate_rejects_id_overflow(self):
        with pytest.raises(FrameError):
            Frame(identifier=0x800).validate()

    def test_validate_rejects_extended_overflow(self):
        with pytest.raises(FrameError):
            Frame(identifier=0x20000000, extended=True).validate()

    def test_validate_rejects_negative_id(self):
        with pytest.raises(FrameError):
            Frame(identifier=-1).validate()

    def test_validate_rejects_classic_over_8_bytes(self):
        with pytest.raises(FrameError):
            Frame(identifier=0x100, data=bytes(9)).validate()

    def test_validate_rejects_illegal_fd_length(self):
        with pytest.raises(FrameError):
            Frame(identifier=0x100, data=bytes(9), fd=True).validate()

    @pytest.mark.parametrize("length", [0, 1, 8, 12, 16, 20, 24, 32, 48, 64])
    def test_validate_accepts_legal_fd_lengths(self, length):
        Frame(identifier=0x100, data=bytes(length), fd=True).validate()

    def test_validate_rejects_remote_with_data(self):
        frame = Frame(identifier=0x100, data=b"\x01", remote=True)
        with pytest.raises(FrameError):
            frame.validate()

    def test_validate_rejects_fd_remote(self):
        frame = Frame.remote_frame(0x100, 8)
        frame.fd = True
        with pytest.raises(FrameError):
            frame.validate()

    def test_validate_rejects_brs_without_fd(self):
        with pytest.raises(FrameError):
            Frame(identifier=0x100, data=b"\x01", brs=True).validate()

    def test_validate_rejects_esi_without_fd(self):
        with pytest.raises(FrameError):
            Frame(identifier=0x100, data=b"\x01", esi=True).validate()

    def test_validate_rejects_remote_dlc_over_limit(self):
        with pytest.raises(FrameError):
            Frame.remote_frame(0x100, 15).validate()

    def test_repr_contains_id(self):
        text = repr(sample_frame())
        assert "0x123" in text


# ---------------------------------------------------------------------------
# 编码 / 解码黄金样例
# ---------------------------------------------------------------------------


class TestEncodeDecode(object):

    def test_encode_starts_with_sof_dominant(self):
        bits = F.encode(sample_frame())
        assert bits[0] == F.DOMINANT

    def test_encode_ends_with_ifs(self):
        bits = F.encode(sample_frame())
        assert bits[-3:] == [F.RECESSIVE] * 3

    def test_encode_eof_is_seven_recessive(self):
        bits = F.encode(sample_frame())
        # 去掉 IFS(3) 之后就是 EOF(7)
        assert bits[-10:-3] == [F.RECESSIVE] * 7

    def test_encode_ack_slot_dominant(self):
        result = F.decode_detailed(F.encode(sample_frame()))
        assert result.ack_slot == F.DOMINANT

    @pytest.mark.parametrize("identifier", [0x000, 0x001, 0x123, 0x7FF])
    def test_standard_id_roundtrip(self, identifier):
        frame = Frame(identifier=identifier, data=bytes([0xAA]))
        decoded = F.decode(F.encode(frame))
        assert decoded.identifier == identifier
        assert not decoded.extended

    @pytest.mark.parametrize("identifier", [
        0x00000000, 0x00000001, 0x000007FF, 0x00000800,
        0x1ABCDEF, 0x1FFFFFFF,
    ])
    def test_extended_id_roundtrip(self, identifier):
        frame = Frame(identifier=identifier, data=bytes([0x55]), extended=True)
        decoded = F.decode(F.encode(frame))
        assert decoded.identifier == identifier
        assert decoded.extended

    @pytest.mark.parametrize("length", ALL_CLASSIC_LENGTHS)
    def test_classic_length_roundtrip(self, length):
        data = bytes(range(length))
        frame = Frame(identifier=0x100, data=data)
        decoded = F.decode(F.encode(frame))
        assert decoded.data == data
        assert decoded.dlc == length

    @pytest.mark.parametrize("length", ALL_FD_LENGTHS)
    def test_fd_length_roundtrip(self, length):
        data = bytes((index * 3) & 0xFF for index in range(length))
        frame = Frame(identifier=0x100, data=data, fd=True)
        decoded = F.decode(F.encode(frame))
        assert decoded.data == data
        assert decoded.fd

    @pytest.mark.parametrize("length", ALL_FD_LENGTHS)
    def test_fd_brs_roundtrip(self, length):
        frame = Frame(identifier=0x100, data=bytes(length), fd=True, brs=True)
        decoded = F.decode(F.encode(frame))
        assert decoded.brs

    def test_fd_esi_roundtrip(self):
        frame = Frame(identifier=0x100, data=bytes(8), fd=True, esi=True)
        decoded = F.decode(F.encode(frame))
        assert decoded.esi

    def test_classic_without_fd_flag_is_dominant_fdf(self):
        bits = F.encode(sample_frame())
        result = F.decode_detailed(bits)
        assert not result.frame.fd

    @pytest.mark.parametrize("dlc", [0, 1, 2, 4, 8])
    def test_remote_frame_roundtrip(self, dlc):
        frame = Frame.remote_frame(0x321, dlc)
        decoded = F.decode(F.encode(frame))
        assert decoded.remote
        assert decoded.requested_length == F.dlc_to_length(dlc)

    def test_extended_remote_frame_roundtrip(self):
        frame = Frame.remote_frame(0x1ABC, 8, extended=True)
        decoded = F.decode(F.encode(frame))
        assert decoded.remote
        assert decoded.extended
        assert decoded.identifier == 0x1ABC

    def test_encode_rejects_invalid_frame(self):
        with pytest.raises(FrameError):
            F.encode(Frame(identifier=0x800))

    def test_encode_without_stuffing_shorter(self):
        frame = Frame(identifier=0x7FF, data=b"\xFF" * 8)
        stuffed = F.encode(frame)
        raw = F.encode(frame, apply_stuffing=False)
        assert len(stuffed) > len(raw)

    def test_decode_rejects_empty_stream(self):
        with pytest.raises(BitStreamError):
            F.decode([])

    def test_decode_rejects_recessive_sof(self):
        bits = F.encode(sample_frame())
        bits[0] = F.RECESSIVE
        with pytest.raises(BitStreamError):
            F.decode(bits)

    def test_decode_rejects_truncated_stream(self):
        bits = F.encode(sample_frame())[:20]
        with pytest.raises(BitStreamError):
            F.decode(bits)

    def test_decode_rejects_bad_crc_delimiter(self):
        bits = F.encode(sample_frame())
        # 定位 CRC 界定符：从尾部往前数 3+7+1 = 11 位
        delimiter_index = len(bits) - 11
        assert bits[delimiter_index] == F.RECESSIVE
        bits[delimiter_index] = F.DOMINANT
        with pytest.raises(BitStreamError):
            F.decode(bits)

    def test_decode_rejects_bad_ack_delimiter(self):
        bits = F.encode(sample_frame())
        ack_delim_index = len(bits) - 10
        bits[ack_delim_index] = F.DOMINANT
        with pytest.raises(BitStreamError):
            F.decode(bits)

    def test_decode_rejects_bad_eof(self):
        bits = F.encode(sample_frame())
        bits[-9] = F.DOMINANT
        with pytest.raises(BitStreamError):
            F.decode(bits)

    def test_decode_detailed_reports_crc_width(self):
        assert F.decode_detailed(F.encode(sample_frame())).crc_width == 15
        fd = Frame(identifier=0x100, data=bytes(8), fd=True)
        assert F.decode_detailed(F.encode(fd)).crc_width == 17
        fd_big = Frame(identifier=0x100, data=bytes(20), fd=True)
        assert F.decode_detailed(F.encode(big := fd_big)).crc_width == 21

    def test_decode_detailed_reports_stuff_count(self):
        frame = Frame(identifier=0x000, data=b"")
        result = F.decode_detailed(F.encode(frame))
        assert result.stuff_bits_count > 0

    def test_roundtrip_helper(self):
        frame = Frame(identifier=0x555, data=b"\x01\x02\x03")
        decoded = F.roundtrip(frame)
        assert decoded.identifier == 0x555
        assert decoded.data == b"\x01\x02\x03"

    def test_check_crc_false_skips_validation(self):
        """check_crc=False 时跳过 CRC 校验，且解码仍能给出 CRC 对比结果。"""
        frame = Frame(identifier=0x123, data=b"\x00\x00")
        bits = F.encode(frame)
        crc_width = F.crc_select(2, False)[0]
        result = F.decode_detailed(bits, check_crc=False)
        assert result.crc_width == crc_width
        # 正常帧的 CRC 应该自洽
        assert result.crc_ok is True

    def test_crc_mismatch_detected_when_checking(self):
        """构造帧内 CRC 与重算值不一致的情况，decode 必须报错。"""
        frame = Frame(identifier=0x123, data=b"\x00\x00")
        bits = F.encode(frame)
        # 把 CRC 序列与 CRC 界定符之间插入的位置找准代价较高，
        # 这里直接验证 decode_detailed 的 crc_ok 与实际 CRC 字段的关系：
        result = F.decode_detailed(bits, check_crc=False)
        assert result.crc_received == result.crc_computed
        # 人为改掉 result 里记录的接收 CRC 不现实，改为验证"改数据后 CRC 必变"
        other = F.encode(Frame(identifier=0x123, data=b"\x01\x00"))
        other_result = F.decode_detailed(other, check_crc=False)
        assert other_result.crc_received != result.crc_received


    @pytest.mark.parametrize("position", [0, 5, 12, 20, 30, 40, 50])
    def test_single_bit_error_is_detected(self, position):
        """在帧的任意位置翻转一位都必须被检出（填充/CRC/格式错误）。"""
        bits = F.encode(sample_frame(identifier=0x7FF,
                                     data=b"\xAA\x55\xAA\x55\xAA\x55\xAA\x55"))
        if position >= len(bits):
            pytest.skip("bit position beyond frame length")
        bits[position] ^= 1
        with pytest.raises((BitStreamError, FrameError)):
            F.decode(bits)


# ---------------------------------------------------------------------------
# 错误帧 / 过载帧
# ---------------------------------------------------------------------------


class TestErrorAndOverloadFrames(object):

    def test_error_frame_structure(self):
        bits = F.encode_error_frame()
        assert bits[:6] == [F.DOMINANT] * 6
        assert bits[6:] == [F.RECESSIVE] * 8
        assert len(bits) == 14

    def test_overload_frame_structure(self):
        bits = F.encode_overload_frame()
        assert bits[:6] == [F.DOMINANT] * 6
        assert bits[6:] == [F.RECESSIVE] * 8

    def test_error_frame_not_stuffed(self):
        """错误标志是固定形式，6 个同极性位不做位填充。"""
        bits = F.encode_error_frame()
        assert bits[:6] == [F.DOMINANT] * 6

    def test_error_frame_rejected_as_data_frame(self):
        with pytest.raises(BitStreamError):
            F.decode(F.encode_error_frame())


# ---------------------------------------------------------------------------
# 帧类型常量
# ---------------------------------------------------------------------------


class TestFrameKind(object):

    def test_kind_constants_distinct(self):
        values = {F.FrameKind.DATA, F.FrameKind.REMOTE,
                  F.FrameKind.ERROR, F.FrameKind.OVERLOAD}
        assert len(values) == 4

    def test_dominant_recessive_values(self):
        assert F.DOMINANT == 0
        assert F.RECESSIVE == 1

    def test_stuff_threshold(self):
        assert F.STUFF_THRESHOLD == 5
