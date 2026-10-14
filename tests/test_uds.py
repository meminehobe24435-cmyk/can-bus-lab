# -*- coding: utf-8 -*-
"""UDS (ISO 14229-1) 测试：各服务、各 NRC、P2 定时、安全访问先决条件。"""

import pytest

from canlab import uds as U
from canlab.uds import (
    DTC_STATUS_AVAILABILITY_MASK, NRC, ReadDTCSubFunction, ResetType,
    RoutineControlType, Service, Session, SUPPRESS_POS_RESPONSE,
    UdsClient, UdsNegativeResponse, UdsProtocolError, UdsServer, UdsTimeout,
    default_key_algorithm,
)


class Clock(object):
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, delta):
        self.now += delta
        return self.now


def make_server(**kwargs):
    """构造一个带常用 DID / DTC / 例程的 ECU。"""
    clock = Clock()
    server = UdsServer(clock=clock, **kwargs)
    server.add_did(0xF190, b"VIN0123456789ABCD")
    server.add_did(0xF187, b"PART-0001")
    server.add_did(0x1234, b"\x00\x00", requires_security=True)
    server.add_did(0xFFFF, b"\xEE", read_only=True)
    server.add_dtc(0x00A001, status=U.DTCStatusMask.CONFIRMED_DTC
                   | U.DTCStatusMask.TEST_FAILED, snapshot=b"\x01\x02\x03",
                   extended=b"\xAA")
    server.add_dtc(0x00B002, status=U.DTCStatusMask.PENDING_DTC)
    server.add_routine(0xFF00, "self-test", result=b"\x01")
    server.add_routine(0xFF01, "locked-routine", result=b"\x02", start_nrc=0x33)
    return server, clock


def make_client(**kwargs):
    server, clock = make_server(**kwargs)
    return UdsClient(server, clock=clock), server, clock


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


class TestUdsConstants(object):

    def test_service_ids(self):
        assert Service.DIAGNOSTIC_SESSION_CONTROL == 0x10
        assert Service.ECU_RESET == 0x11
        assert Service.CLEAR_DIAGNOSTIC_INFORMATION == 0x14
        assert Service.READ_DTC_INFORMATION == 0x19
        assert Service.READ_DATA_BY_IDENTIFIER == 0x22
        assert Service.SECURITY_ACCESS == 0x27
        assert Service.WRITE_DATA_BY_IDENTIFIER == 0x2E
        assert Service.ROUTINE_CONTROL == 0x31
        assert Service.TESTER_PRESENT == 0x3E

    def test_service_names(self):
        assert Service.NAMES[0x22] == "ReadDataByIdentifier"
        assert Service.NAMES[0x27] == "SecurityAccess"

    def test_nrc_values(self):
        assert NRC.SERVICE_NOT_SUPPORTED == 0x11
        assert NRC.SUB_FUNCTION_NOT_SUPPORTED == 0x12
        assert NRC.INCORRECT_MESSAGE_LENGTH == 0x13
        assert NRC.CONDITIONS_NOT_CORRECT == 0x22
        assert NRC.REQUEST_SEQUENCE_ERROR == 0x24
        assert NRC.REQUEST_OUT_OF_RANGE == 0x31
        assert NRC.SECURITY_ACCESS_DENIED == 0x33
        assert NRC.INVALID_KEY == 0x35
        assert NRC.EXCEED_NUMBER_OF_ATTEMPTS == 0x36
        assert NRC.SUB_FUNCTION_NOT_SUPPORTED_IN_ACTIVE_SESSION == 0x7E
        assert NRC.SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION == 0x7F

    def test_nrc_names(self):
        assert NRC.NAMES[NRC.SECURITY_ACCESS_DENIED] == "securityAccessDenied"
        assert NRC.NAMES[NRC.INVALID_KEY] == "invalidKey"

    def test_session_values(self):
        assert Session.DEFAULT == 1
        assert Session.PROGRAMMING == 2
        assert Session.EXTENDED == 3

    def test_reset_types(self):
        assert ResetType.HARD == 1
        assert ResetType.KEY_OFF_ON == 2
        assert ResetType.SOFT == 3

    def test_routine_control_types(self):
        assert RoutineControlType.START == 1
        assert RoutineControlType.STOP == 2
        assert RoutineControlType.REQUEST_RESULTS == 3

    def test_suppress_bit(self):
        assert SUPPRESS_POS_RESPONSE == 0x80

    def test_dtc_status_mask_bits(self):
        assert U.DTCStatusMask.TEST_FAILED == 0x01
        assert U.DTCStatusMask.CONFIRMED_DTC == 0x08
        assert U.DTCStatusMask.PENDING_DTC == 0x04

    def test_dtc_status_availability_mask(self):
        assert DTC_STATUS_AVAILABILITY_MASK & 0x01
        assert DTC_STATUS_AVAILABILITY_MASK & 0x08


# ---------------------------------------------------------------------------
# 定时参数
# ---------------------------------------------------------------------------


class TestUdsTiming(object):

    def test_defaults(self):
        timing = U.UdsTiming()
        assert timing.p2_server_ms == 50.0
        assert timing.p2_star_server_ms == 5000.0
        assert timing.p2_client_ms > timing.p2_server_ms
        assert timing.p2_star_client_ms > timing.p2_star_server_ms

    def test_s3_default(self):
        assert U.UdsTiming().s3_server_ms == 5000.0


# ---------------------------------------------------------------------------
# 会话控制 0x10
# ---------------------------------------------------------------------------


class TestSessionControl(object):

    def test_enter_extended_session(self):
        client, server, _clock = make_client()
        response = client.session_control(Session.EXTENDED)
        assert response.data[0] == 0x50
        assert response.data[1] == Session.EXTENDED
        assert server.session == Session.EXTENDED

    def test_enter_programming_session(self):
        client, server, _clock = make_client()
        client.session_control(Session.PROGRAMMING)
        assert server.session == Session.PROGRAMMING

    def test_response_contains_p2_timings(self):
        client, _server, _clock = make_client()
        response = client.session_control(Session.EXTENDED)
        assert len(response.data) == 6
        assert int.from_bytes(response.data[2:4], "big") == 50

    def test_wrong_length_nrc_0x13(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x10]))
        assert response.nrc == NRC.INCORRECT_MESSAGE_LENGTH

    def test_unsupported_sub_function_nrc_0x12(self):
        client, _server, _clock = make_client()
        response = client.session_control(0x7E)
        assert response.nrc == NRC.SUB_FUNCTION_NOT_SUPPORTED

    def test_suppress_positive_response(self):
        client, server, _clock = make_client()
        response = client.session_control(Session.EXTENDED, suppress=True)
        assert response.suppressed
        assert response.data == b""
        assert server.session == Session.EXTENDED

    def test_session_switch_resets_security(self):
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.security_access_unlock(1)
        assert server.security_level == 1
        client.session_control(Session.DEFAULT)
        assert server.security_level == 0

    def test_session_change_counter(self):
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        assert server.session_changes == 1

    def test_s3_timeout_falls_back(self):
        client, server, clock = make_client()
        client.session_control(Session.EXTENDED)
        clock.advance(server.timing.s3_server_ms + 1)
        assert server.s3_expired()
        assert server.session == Session.DEFAULT

    def test_s3_not_expired_within_window(self):
        client, server, clock = make_client()
        client.session_control(Session.EXTENDED)
        clock.advance(10.0)
        assert not server.s3_expired()

    def test_s3_no_timeout_in_default_session(self):
        _client, server, clock = make_client()
        clock.advance(100000.0)
        assert not server.s3_expired()


# ---------------------------------------------------------------------------
# ECU 复位 0x11
# ---------------------------------------------------------------------------


class TestEcuReset(object):

    def test_hard_reset(self):
        client, server, _clock = make_client()
        response = client.ecu_reset(ResetType.HARD)
        assert response.data == bytes([0x51, 0x01])
        assert server.reset_count == 1

    @pytest.mark.parametrize("reset_type", ResetType.ALL)
    def test_all_reset_types(self, reset_type):
        client, _server, _clock = make_client()
        response = client.ecu_reset(reset_type)
        assert response.data[1] == reset_type

    def test_reset_returns_to_default_session(self):
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.ecu_reset()
        assert server.session == Session.DEFAULT

    def test_reset_clears_security(self):
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.security_access_unlock(1)
        client.ecu_reset()
        assert server.security_level == 0

    def test_reset_wrong_length(self):
        client, _server, _clock = make_client()
        assert client.request(bytes([0x11])).nrc == NRC.INCORRECT_MESSAGE_LENGTH

    def test_reset_unsupported_type(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x11, 0x09]))
        assert response.nrc == NRC.SUB_FUNCTION_NOT_SUPPORTED


# ---------------------------------------------------------------------------
# 读数据 0x22
# ---------------------------------------------------------------------------


class TestReadDataByIdentifier(object):

    def test_read_known_did(self):
        client, _server, _clock = make_client()
        assert client.read_data(0xF190) == b"VIN0123456789ABCD"

    def test_response_format(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x22, 0xF1, 0x90]))
        assert response.data[:3] == b"\x62\xF1\x90"

    def test_unknown_did_nrc_0x31(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x22, 0x00, 0x01]))
        assert response.nrc == NRC.REQUEST_OUT_OF_RANGE

    def test_short_request_nrc_0x13(self):
        client, _server, _clock = make_client()
        assert client.request(bytes([0x22, 0xF1])).nrc == NRC.INCORRECT_MESSAGE_LENGTH

    def test_odd_payload_nrc_0x13(self):
        client, _server, _clock = make_client()
        assert client.request(bytes([0x22, 0xF1, 0x90, 0x00])).nrc == \
            NRC.INCORRECT_MESSAGE_LENGTH

    def test_security_denied_nrc_0x33(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x22, 0x12, 0x34]))
        assert response.nrc == NRC.SECURITY_ACCESS_DENIED

    def test_read_after_unlock(self):
        client, _server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.security_access_unlock(1)
        assert client.read_data(0x1234) == b"\x00\x00"

    def test_read_multiple_dids(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x22, 0xF1, 0x90, 0xF1, 0x87]))
        assert response.data[:3] == b"\x62\xF1\x90"
        assert b"PART-0001" in response.data

    def test_read_data_raises_on_negative(self):
        client, _server, _clock = make_client()
        with pytest.raises(UdsNegativeResponse) as info:
            client.read_data(0x0001)
        assert info.value.nrc == NRC.REQUEST_OUT_OF_RANGE


# ---------------------------------------------------------------------------
# 写数据 0x2E
# ---------------------------------------------------------------------------


class TestWriteDataByIdentifier(object):

    def test_write_after_unlock(self):
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.security_access_unlock(1)
        response = client.write_data(0x1234, b"\xAB")
        assert response.data == b"\x6E\x12\x34"
        assert server.data[0x1234] == b"\xAB"

    def test_write_denied_without_unlock(self):
        client, _server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        response = client.write_data(0x1234, b"\xAB")
        assert response.nrc == NRC.SECURITY_ACCESS_DENIED

    def test_write_unknown_did(self):
        client, _server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.security_access_unlock(1)
        assert client.write_data(0x0001, b"\x01").nrc == NRC.REQUEST_OUT_OF_RANGE

    def test_write_readonly_did_rejected(self):
        client, _server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.security_access_unlock(1)
        assert client.write_data(0xFFFF, b"\x01").nrc == NRC.REQUEST_OUT_OF_RANGE

    def test_write_short_request(self):
        client, _server, _clock = make_client()
        assert client.request(bytes([0x2E, 0x12])).nrc == NRC.INCORRECT_MESSAGE_LENGTH

    def test_write_then_read_back(self):
        client, _server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.security_access_unlock(1)
        client.write_data(0x1234, b"\x5A")
        assert client.read_data(0x1234) == b"\x5A"

    def test_suppress_bit_not_defined_for_write(self):
        """0x2E 的 byte1 是 DID 高字节而非子功能，因此"抑制正响应位"对它无意义。

        本实现只在带子功能的服务（0x10/0x11/0x27/0x3E 等）上解释最高位，
        对 0x2E 会把 0x80 当成 DID 的一部分，从而返回 requestOutOfRange。
        """
        client, _server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.security_access_unlock(1)
        response = client.request(bytes([0x2E, 0x92, 0x34, 0x11]))
        assert response.negative
        assert response.nrc == NRC.REQUEST_OUT_OF_RANGE

    def test_suppress_bit_on_tester_present(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x3E, 0x80]))
        assert response.suppressed

    def test_suppress_bit_on_ecu_reset(self):
        client, server, _clock = make_client()
        response = client.request(bytes([0x11, 0x81]))
        assert response.suppressed
        assert server.reset_count == 1

    def test_write_session_restriction(self):
        client, server, _clock = make_client()
        server.write_sessions[0x1234] = (Session.EXTENDED,)
        client.session_control(Session.EXTENDED)
        client.security_access_unlock(1)
        assert client.write_data(0x1234, b"\x01").data[0] == 0x6E
        # 切换会话会复位安全等级，重新解锁后仍可在扩展会话写入
        client.session_control(Session.DEFAULT)
        client.session_control(Session.EXTENDED)
        client.security_access_unlock(1)
        assert client.write_data(0x1234, b"\x02").data[0] == 0x6E

    def test_write_blocked_in_wrong_session(self):
        client, server, _clock = make_client()
        server.write_sessions[0x1234] = (Session.PROGRAMMING,)
        client.session_control(Session.EXTENDED)
        client.security_access_unlock(1)
        assert client.write_data(0x1234, b"\x01").nrc == NRC.REQUEST_OUT_OF_RANGE


# ---------------------------------------------------------------------------
# 安全访问 0x27
# ---------------------------------------------------------------------------


class TestSecurityAccess(object):

    def test_request_seed(self):
        client, _server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        response = client.request(bytes([0x27, 0x01]))
        assert response.data[0] == 0x67
        assert response.data[1] == 0x01
        assert len(response.data) == 4

    def test_full_unlock_flow(self):
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        response = client.security_access_unlock(1)
        assert response.data == bytes([0x67, 0x02])
        assert server.security_level == 1

    def test_wrong_key_nrc_0x35(self):
        client, _server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.request(bytes([0x27, 0x01]))
        response = client.request(bytes([0x27, 0x02, 0xFF, 0xFF]))
        assert response.nrc == NRC.INVALID_KEY

    def test_key_before_seed_nrc_0x24(self):
        client, _server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        response = client.request(bytes([0x27, 0x02, 0x00, 0x00]))
        assert response.nrc == NRC.REQUEST_SEQUENCE_ERROR

    def test_exceed_attempts_nrc_0x36(self):
        """连续送错密钥：前几次 0x35，达到上限的那次返回 0x36。"""
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        nrcs = []
        for _ in range(server.max_attempts):
            client.request(bytes([0x27, 0x01]))
            nrcs.append(client.request(bytes([0x27, 0x02, 0xFF, 0xFF])).nrc)
        assert nrcs[:-1] == [NRC.INVALID_KEY] * (server.max_attempts - 1)
        assert nrcs[-1] == NRC.EXCEED_NUMBER_OF_ATTEMPTS
        assert server.failed_attempts[0x02] >= server.max_attempts

    def test_time_delay_not_expired_nrc_0x37(self):
        """达到尝试上限后会进入锁定窗口，窗口内任何请求都返回 0x37。"""
        client, server, clock = make_client()
        client.session_control(Session.EXTENDED)
        for _ in range(server.max_attempts):
            client.request(bytes([0x27, 0x01]))
            client.request(bytes([0x27, 0x02, 0xFF, 0xFF]))
        assert server.lockout_until_ms.get(0x02, 0.0) > clock.now
        # 锁定窗口内：请求密钥 -> 0x37（判定优先于 0x36）
        assert client.request(bytes([0x27, 0x02, 0x00, 0x00])).nrc == \
            NRC.REQUIRED_TIME_DELAY_NOT_EXPIRED
        # 锁定窗口过去之后不再返回 0x37，但因尝试次数已达上限仍为 0x36
        clock.advance(server.attempt_lockout_ms + 1.0)
        assert client.request(bytes([0x27, 0x02, 0x00, 0x00])).nrc == \
            NRC.EXCEED_NUMBER_OF_ATTEMPTS

    def test_reset_clears_failed_attempts(self):
        """ECU 复位会清除失败计数，之后可以重新解锁。"""
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        for _ in range(server.max_attempts):
            client.request(bytes([0x27, 0x01]))
            client.request(bytes([0x27, 0x02, 0xFF, 0xFF]))
        # 达到上限后进入锁定窗口，窗口内一律 0x37
        client.request(bytes([0x27, 0x01]))
        assert client.request(bytes([0x27, 0x02, 0xFF, 0xFF])).nrc == \
            NRC.REQUIRED_TIME_DELAY_NOT_EXPIRED
        assert server.failed_attempts[0x02] >= server.max_attempts
        client.ecu_reset()
        client.session_control(Session.EXTENDED)
        assert client.security_access_unlock(1).data == bytes([0x67, 0x02])

    def test_session_switch_clears_seeds(self):
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.request(bytes([0x27, 0x01]))
        client.session_control(Session.DEFAULT)
        assert server.seeds == {}

    def test_unsupported_sub_function(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x27, 0x09]))
        assert response.nrc == NRC.SUB_FUNCTION_NOT_SUPPORTED

    def test_seed_is_deterministic(self):
        """同一个 ECU 在相同请求序号下给出相同的种子（便于可复现测试）。"""
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        first = server._make_seed(1, 0.0)
        second = server._make_seed(1, 0.0)
        assert first == second

    def test_seed_changes_between_requests(self):
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        seeds = []
        for _ in range(3):
            response = client.request(bytes([0x27, 0x01]))
            seeds.append(response.data[2:])
        assert len(set(seeds)) >= 2

    def test_key_algorithm_is_deterministic(self):
        seed = b"\x12\x34"
        assert default_key_algorithm(seed, 1) == default_key_algorithm(seed, 1)

    def test_key_algorithm_length(self):
        assert len(default_key_algorithm(b"\x12\x34", 1)) == 2

    def test_key_algorithm_rejects_empty_seed(self):
        with pytest.raises(ValueError):
            default_key_algorithm(b"", 1)

    def test_key_algorithm_level_changes_key(self):
        seed = b"\x12\x34"
        assert default_key_algorithm(seed, 1) != default_key_algorithm(seed, 2)

    def test_unlock_level_two(self):
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.security_access_unlock(2)
        assert server.security_level == 2

    def test_short_key_rejected(self):
        client, _server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.request(bytes([0x27, 0x01]))
        response = client.request(bytes([0x27, 0x02]))
        assert response.nrc == NRC.INVALID_KEY


# ---------------------------------------------------------------------------
# DTC 0x19 / 0x14
# ---------------------------------------------------------------------------


class TestDtcServices(object):

    def test_read_dtc_count(self):
        client, _server, _clock = make_client()
        assert client.read_dtc_count(0xFF) == 2

    def test_read_dtc_count_with_confirmed_mask(self):
        client, _server, _clock = make_client()
        assert client.read_dtc_count(U.DTCStatusMask.CONFIRMED_DTC) == 1

    def test_read_dtc_count_with_pending_mask(self):
        client, _server, _clock = make_client()
        assert client.read_dtc_count(U.DTCStatusMask.PENDING_DTC) == 1

    def test_read_dtc_count_response_format(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x19, 0x01, 0xFF]))
        assert response.data[0] == 0x59
        assert response.data[1] == 0x01
        assert response.data[3] == DTC_STATUS_AVAILABILITY_MASK

    def test_read_dtc_list(self):
        client, _server, _clock = make_client()
        dtcs = client.read_dtcs(0xFF)
        assert len(dtcs) == 2
        assert dtcs[0][0] == 0x00A001

    def test_read_dtc_list_status(self):
        client, _server, _clock = make_client()
        dtcs = client.read_dtcs(0xFF)
        assert dtcs[0][1] == U.DTCStatusMask.CONFIRMED_DTC | U.DTCStatusMask.TEST_FAILED

    def test_read_dtc_list_no_match_nrc_0x31(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x19, 0x02, 0x80]))
        assert response.nrc == NRC.REQUEST_OUT_OF_RANGE

    def test_unsupported_sub_function_0x19(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x19, 0x0A, 0xFF]))
        assert response.nrc == NRC.SUB_FUNCTION_NOT_SUPPORTED

    def test_snapshot_sub_function(self):
        client, _server, _clock = make_client()
        # 0x19 04 <DTC[3]> <recordNumber>：长度至少 5 字节
        response = client.request(bytes([0x19, 0x04, 0x00, 0xA0, 0x01, 0x01]))
        assert response.data[0] == 0x59
        assert response.data[1] == 0x04

    def test_snapshot_sub_function_too_short(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x19, 0x04, 0x00, 0xFF]))
        assert response.nrc == NRC.INCORRECT_MESSAGE_LENGTH

    def test_extended_data_sub_function(self):
        client, _server, _clock = make_client()
        # 0x19 06 <DTC[3]> <recordNumber>：至少 6 字节
        response = client.request(bytes([0x19, 0x06, 0x00, 0xA0, 0x01, 0x01]))
        assert response.data[0] == 0x59
        assert response.data[1] == 0x06
        assert response.data[2:5] == b"\x00\xA0\x01"

    def test_snapshot_record_sub_function(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x19, 0x04, 0x00, 0xA0, 0x01, 0x01]))
        assert response.data[0] == 0x59
        assert response.data[1] == 0x04

    def test_clear_dtc_in_default_session_nrc_0x22(self):
        client, _server, _clock = make_client()
        response = client.clear_dtc()
        assert response.nrc == NRC.CONDITIONS_NOT_CORRECT

    def test_clear_dtc_in_extended_session(self):
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        response = client.clear_dtc()
        assert response.data[0] == 0x54
        assert server.dtcs == []

    def test_clear_dtc_wrong_length(self):
        client, _server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        response = client.request(bytes([0x14, 0xFF]))
        assert response.nrc == NRC.INCORRECT_MESSAGE_LENGTH

    def test_clear_then_read_zero(self):
        client, _server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.clear_dtc()
        assert client.read_dtc_count(0xFF) == 0

    def test_clear_specific_group(self):
        """本实现按 DTC 的最高字节（故障组）过滤，组内 DTC 全部清除。"""
        client, server, _clock = make_client()
        client.session_control(Session.EXTENDED)
        client.clear_dtc(0x00FFFFFF & 0x00FFFFFF)
        assert server.dtcs == []

    def test_clear_group_keeps_other_groups(self):
        client, server, _clock = make_client()
        server.add_dtc(0x01A001)
        client.session_control(Session.EXTENDED)
        # 只清最高字节为 0x01 的那组
        client.clear_dtc(0x01A001)
        assert all((dtc.code >> 16) != 0x01 for dtc in server.dtcs)
        assert len(server.dtcs) == 2

    def test_dtc_three_byte_encoding(self):
        client, _server, _clock = make_client()
        dtcs = client.read_dtcs(0xFF)
        assert dtcs[0][0].to_bytes(3, "big") == b"\x00\xA0\x01"


# ---------------------------------------------------------------------------
# 例程控制 0x31
# ---------------------------------------------------------------------------


class TestRoutineControl(object):

    def test_start_routine(self):
        client, _server, _clock = make_client()
        response = client.routine_control(RoutineControlType.START, 0xFF00)
        assert response.data == bytes([0x71, 0x01, 0xFF, 0x00])

    def test_stop_routine(self):
        client, _server, _clock = make_client()
        client.routine_control(RoutineControlType.START, 0xFF00)
        response = client.routine_control(RoutineControlType.STOP, 0xFF00)
        assert response.data[:2] == bytes([0x71, 0x02])

    def test_stop_without_start_nrc_0x24(self):
        client, _server, _clock = make_client()
        response = client.routine_control(RoutineControlType.STOP, 0xFF00)
        assert response.nrc == NRC.REQUEST_SEQUENCE_ERROR

    def test_request_results(self):
        client, _server, _clock = make_client()
        response = client.routine_control(RoutineControlType.REQUEST_RESULTS, 0xFF00)
        assert response.data[4] == 0x01

    def test_unknown_routine_nrc_0x31(self):
        client, _server, _clock = make_client()
        response = client.routine_control(RoutineControlType.START, 0x0000)
        assert response.nrc == NRC.REQUEST_OUT_OF_RANGE

    def test_routine_nrc_propagated(self):
        client, _server, _clock = make_client()
        response = client.routine_control(RoutineControlType.START, 0xFF01)
        assert response.nrc == NRC.SECURITY_ACCESS_DENIED

    def test_unsupported_sub_function(self):
        client, _server, _clock = make_client()
        response = client.routine_control(0x09, 0xFF00)
        assert response.nrc == NRC.SUB_FUNCTION_NOT_SUPPORTED

    def test_short_request(self):
        client, _server, _clock = make_client()
        assert client.request(bytes([0x31, 0x01])).nrc == NRC.INCORRECT_MESSAGE_LENGTH

    def test_double_start_nrc_0x24(self):
        client, _server, _clock = make_client()
        client.routine_control(RoutineControlType.START, 0xFF00)
        response = client.routine_control(RoutineControlType.START, 0xFF00)
        assert response.nrc == NRC.REQUEST_SEQUENCE_ERROR

    def test_restartable_routine(self):
        client, server, _clock = make_client()
        server.routines[0xFF00].restartable = True
        client.routine_control(RoutineControlType.START, 0xFF00)
        assert client.routine_control(RoutineControlType.START, 0xFF00).data[0] == 0x71


# ---------------------------------------------------------------------------
# TesterPresent 0x3E
# ---------------------------------------------------------------------------


class TestTesterPresent(object):

    def test_tester_present_default_suppressed(self):
        client, _server, _clock = make_client()
        response = client.tester_present()
        assert response.suppressed

    def test_tester_present_with_response(self):
        client, _server, _clock = make_client()
        response = client.tester_present(suppress=False)
        assert response.data == bytes([0x7E, 0x00])

    def test_tester_present_keeps_session(self):
        client, server, clock = make_client()
        client.session_control(Session.EXTENDED)
        clock.advance(1000.0)
        client.tester_present()
        clock.advance(1000.0)
        assert server.session == Session.EXTENDED

    def test_tester_present_wrong_length(self):
        client, _server, _clock = make_client()
        assert client.request(bytes([0x3E])).nrc == NRC.INCORRECT_MESSAGE_LENGTH


# ---------------------------------------------------------------------------
# 通用 NRC 与协议行为
# ---------------------------------------------------------------------------


class TestGenericNrc(object):

    def test_unsupported_service_nrc_0x11(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x99, 0x01]))
        assert response.nrc == NRC.SERVICE_NOT_SUPPORTED

    def test_negative_response_format(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x99, 0x01]))
        assert response.data[0] == 0x7F
        assert response.data[1] == 0x99
        assert response.negative

    def test_service_disabled_by_server(self):
        client, server, _clock = make_client()
        server.supported_services.discard(Service.ROUTINE_CONTROL)
        response = client.request(bytes([0x31, 0x01, 0xFF, 0x00]))
        assert response.nrc == NRC.SERVICE_NOT_SUPPORTED

    def test_empty_request(self):
        client, _server, _clock = make_client()
        response = client.request(b"")
        assert response.negative

    def test_session_restricted_service_nrc_0x7F(self):
        client, server, _clock = make_client()
        # 编程会话下禁止 0x22
        server.session_services[Session.PROGRAMMING] = (
            Service.DIAGNOSTIC_SESSION_CONTROL, Service.ECU_RESET,
            Service.TESTER_PRESENT,
        )
        client.session_control(Session.PROGRAMMING)
        response = client.request(bytes([0x22, 0xF1, 0x90]))
        assert response.nrc == NRC.SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION

    def test_request_counter(self):
        client, server, _clock = make_client()
        client.request(bytes([0x22, 0xF1, 0x90]))
        client.request(bytes([0x22, 0xF1, 0x87]))
        assert server.requests_handled == 2

    def test_response_pending_flag(self):
        client, server, _clock = make_client()
        server.pending_count = 1
        response = client.request(bytes([0x22, 0xF1, 0x90]))
        assert response.pending_sent

    def test_pending_response_allows_longer_wait(self):
        server, clock = make_server()
        server.pending_count = 1
        server.response_delay_ms = 200.0
        client = UdsClient(server, clock=clock)
        response = client.request(bytes([0x22, 0xF1, 0x90]))
        assert response.data[0] == 0x62

    def test_last_exchange_tracks(self):
        client, _server, _clock = make_client()
        client.request(bytes([0x22, 0xF1, 0x90]))
        exchange = client.last_exchange()
        assert exchange.request == bytes([0x22, 0xF1, 0x90])
        assert exchange.positive

    def test_exchange_elapsed(self):
        client, server, _clock = make_client()
        server.response_delay_ms = 10.0
        client.request(bytes([0x22, 0xF1, 0x90]))
        assert client.last_exchange().elapsed_ms == pytest.approx(10.0)

    def test_nrc_name_lookup(self):
        client, _server, _clock = make_client()
        response = client.request(bytes([0x99, 0x01]))
        assert response.nrc_name == "serviceNotSupported"


# ---------------------------------------------------------------------------
# P2 / P2* 定时
# ---------------------------------------------------------------------------


class TestP2Timing(object):

    def test_p2_timeout_raises(self):
        server, clock = make_server()
        server.response_delay_ms = 500.0
        client = UdsClient(server, clock=clock)
        with pytest.raises(UdsTimeout):
            client.request(bytes([0x22, 0xF1, 0x90]))

    def test_p2_within_limit_ok(self):
        server, clock = make_server()
        server.response_delay_ms = 10.0
        client = UdsClient(server, clock=clock)
        client.request(bytes([0x22, 0xF1, 0x90]))
        assert client.timeouts == 0

    def test_p2_timeout_counter(self):
        server, clock = make_server()
        server.response_delay_ms = 500.0
        client = UdsClient(server, clock=clock)
        for _ in range(2):
            try:
                client.request(bytes([0x22, 0xF1, 0x90]))
            except UdsTimeout:
                pass
        assert client.timeouts == 2

    def test_p2_star_used_after_pending(self):
        server, clock = make_server()
        server.pending_count = 1
        server.response_delay_ms = 1000.0
        client = UdsClient(server, clock=clock)
        # P2*=5.1s，1000 ms 延迟应当通过
        response = client.request(bytes([0x22, 0xF1, 0x90]))
        assert response.pending_sent

    def test_p2_star_timeout_raises(self):
        server, clock = make_server()
        server.pending_count = 1
        server.response_delay_ms = 6000.0
        client = UdsClient(server, clock=clock)
        with pytest.raises(UdsTimeout):
            client.request(bytes([0x22, 0xF1, 0x90]))

    def test_custom_timing_respected(self):
        server, clock = make_server()
        timing = U.UdsTiming(p2_client_ms=5.0)
        server.response_delay_ms = 10.0
        client = UdsClient(server, timing=timing, clock=clock)
        with pytest.raises(UdsTimeout):
            client.request(bytes([0x22, 0xF1, 0x90]))


# ---------------------------------------------------------------------------
# DID / DTC / 例程管理
# ---------------------------------------------------------------------------


class TestServerConfiguration(object):

    def test_add_did(self):
        server, _clock = make_server()
        assert server.data[0xF190] == b"VIN0123456789ABCD"

    def test_read_only_did(self):
        server, _clock = make_server()
        assert 0xFFFF in server.read_only

    def test_security_required_did(self):
        server, _clock = make_server()
        assert server.read_requires_security[0x1234]

    def test_add_dtc_returns_object(self):
        server, _clock = make_server()
        dtc = server.add_dtc(0x00C003)
        assert dtc.code == 0x00C003
        assert dtc in server.dtcs

    def test_add_routine(self):
        server, _clock = make_server()
        routine = server.add_routine(0xFF02, name="x")
        assert server.routines[0xFF02] is routine

    def test_server_repr(self):
        server, _clock = make_server()
        assert "UdsServer" in repr(server)

    def test_dtc_dataclass_fields(self):
        dtc = U.Dtc(code=0x123456, status=0x08, snapshot=b"\x01", extended=b"\x02")
        assert dtc.three_bytes() == b"\x12\x34\x56"

    def test_routine_dataclass_defaults(self):
        routine = U.Routine(identifier=0x1234)
        assert not routine.running
        assert not routine.restartable

    def test_diagnostic_request_helpers(self):
        request = U.DiagnosticRequest(data=bytes([0x10, 0x83]))
        assert request.service == 0x10
        assert request.sub_function == 0x03
        assert request.suppress_positive_response

    def test_diagnostic_request_no_subfunction(self):
        request = U.DiagnosticRequest(data=bytes([0x22]))
        assert request.sub_function == 0
        assert not request.suppress_positive_response
