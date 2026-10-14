# -*- coding: utf-8 -*-
r"""ISO 14229-1 (UDS) 诊断服务子集：服务端（ECU）+ 客户端（诊断仪）。

实现的服务
----------
======  ==============================  ============================================
SID     名称                            说明
======  ==============================  ============================================
0x10    DiagnosticSessionControl       默认 / 编程 / 扩展会话
0x11    ECUReset                       硬复位 / 钥匙开-关复位 / 软复位
0x14    ClearDiagnosticInformation     清 DTC（可按组）
0x19    ReadDTCInformation             子功能 0x01/0x02/0x04/0x06
0x22    ReadDataByIdentifier           按 DID 读数据
0x27    SecurityAccess                请求种子 / 发送密钥
0x2E    WriteDataByIdentifier          按 DID 写数据
0x31    RoutineControl                子功能 0x01/0x02/0x03
0x3E    TesterPresent                  保持会话
======  ==============================  ============================================

支持的负响应码（NRC）
--------------------
0x11 serviceNotSupported、0x12 subFunctionNotSupported、0x13 incorrectMessageLength\
OrInvalidFormat、0x14 responseTooLong、0x22 conditionsNotCorrect、
0x24 requestSequenceError、0x31 requestOutOfRange、0x33 securityAccessDenied、
0x35 invalidKey、0x36 exceedNumberOfAttempts、0x37 requiredTimeDelayNotExpired、
0x7E subFunctionNotSupportedInActiveSession、0x7F serviceNotSupportedInActiveSession、
0x78 requestCorrectlyReceivedResponsePending（非负响应，用于 P2* 延长）。

定时
----
* **P2_server**：收到请求后必须在 P2 内给出首个响应，否则客户端超时。
* **P2\*_server**：发出 0x78 之后，后续响应的间隔上限改为 P2\*。
* **S3_server**：默认会话下无请求容忍时间（本实现用于会话自动回落）。

**抑制正响应**：子功能字节的最高位（0x80）置位时，ECU 执行操作但不返回正响应。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 服务与 NRC 常量
# ---------------------------------------------------------------------------


class Service(object):
    """诊断服务标识符。"""

    DIAGNOSTIC_SESSION_CONTROL = 0x10
    ECU_RESET = 0x11
    CLEAR_DIAGNOSTIC_INFORMATION = 0x14
    READ_DTC_INFORMATION = 0x19
    READ_DATA_BY_IDENTIFIER = 0x22
    SECURITY_ACCESS = 0x27
    WRITE_DATA_BY_IDENTIFIER = 0x2E
    ROUTINE_CONTROL = 0x31
    TESTER_PRESENT = 0x3E

    ALL = (
        DIAGNOSTIC_SESSION_CONTROL,
        ECU_RESET,
        CLEAR_DIAGNOSTIC_INFORMATION,
        READ_DTC_INFORMATION,
        READ_DATA_BY_IDENTIFIER,
        SECURITY_ACCESS,
        WRITE_DATA_BY_IDENTIFIER,
        ROUTINE_CONTROL,
        TESTER_PRESENT,
    )

    NAMES = {
        DIAGNOSTIC_SESSION_CONTROL: "DiagnosticSessionControl",
        ECU_RESET: "ECUReset",
        CLEAR_DIAGNOSTIC_INFORMATION: "ClearDiagnosticInformation",
        READ_DTC_INFORMATION: "ReadDTCInformation",
        READ_DATA_BY_IDENTIFIER: "ReadDataByIdentifier",
        SECURITY_ACCESS: "SecurityAccess",
        WRITE_DATA_BY_IDENTIFIER: "WriteDataByIdentifier",
        ROUTINE_CONTROL: "RoutineControl",
        TESTER_PRESENT: "TesterPresent",
    }


class NRC(object):
    """负响应码。"""

    GENERAL_REJECT = 0x10
    SERVICE_NOT_SUPPORTED = 0x11
    SUB_FUNCTION_NOT_SUPPORTED = 0x12
    INCORRECT_MESSAGE_LENGTH = 0x13
    RESPONSE_TOO_LONG = 0x14
    BUSY_REPEAT_REQUEST = 0x21
    CONDITIONS_NOT_CORRECT = 0x22
    REQUEST_SEQUENCE_ERROR = 0x24
    NO_RESPONSE_FROM_SUBNET_COMPONENT = 0x25
    FAILURE_PREVENTS_EXECUTION = 0x26
    REQUEST_OUT_OF_RANGE = 0x31
    SECURITY_ACCESS_DENIED = 0x33
    INVALID_KEY = 0x35
    EXCEED_NUMBER_OF_ATTEMPTS = 0x36
    REQUIRED_TIME_DELAY_NOT_EXPIRED = 0x37
    UPLOAD_DOWNLOAD_NOT_ACCEPTED = 0x70
    TRANSFER_DATA_SUSPENDED = 0x71
    GENERAL_PROGRAMMING_FAILURE = 0x72
    WRONG_BLOCK_SEQUENCE_COUNTER = 0x73
    REQUEST_CORRECTLY_RECEIVED_RESPONSE_PENDING = 0x78
    SUB_FUNCTION_NOT_SUPPORTED_IN_ACTIVE_SESSION = 0x7E
    SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION = 0x7F

    NAMES = {
        GENERAL_REJECT: "generalReject",
        SERVICE_NOT_SUPPORTED: "serviceNotSupported",
        SUB_FUNCTION_NOT_SUPPORTED: "subFunctionNotSupported",
        INCORRECT_MESSAGE_LENGTH: "incorrectMessageLengthOrInvalidFormat",
        RESPONSE_TOO_LONG: "responseTooLong",
        BUSY_REPEAT_REQUEST: "busyRepeatRequest",
        CONDITIONS_NOT_CORRECT: "conditionsNotCorrect",
        REQUEST_SEQUENCE_ERROR: "requestSequenceError",
        NO_RESPONSE_FROM_SUBNET_COMPONENT: "noResponseFromSubnetComponent",
        FAILURE_PREVENTS_EXECUTION: "failurePreventsExecutionOfRequestedAction",
        REQUEST_OUT_OF_RANGE: "requestOutOfRange",
        SECURITY_ACCESS_DENIED: "securityAccessDenied",
        INVALID_KEY: "invalidKey",
        EXCEED_NUMBER_OF_ATTEMPTS: "exceedNumberOfAttempts",
        REQUIRED_TIME_DELAY_NOT_EXPIRED: "requiredTimeDelayNotExpired",
        UPLOAD_DOWNLOAD_NOT_ACCEPTED: "uploadDownloadNotAccepted",
        TRANSFER_DATA_SUSPENDED: "transferDataSuspended",
        GENERAL_PROGRAMMING_FAILURE: "generalProgrammingFailure",
        WRONG_BLOCK_SEQUENCE_COUNTER: "wrongBlockSequenceCounter",
        REQUEST_CORRECTLY_RECEIVED_RESPONSE_PENDING: "requestCorrectlyReceivedResponsePending",
        SUB_FUNCTION_NOT_SUPPORTED_IN_ACTIVE_SESSION:
            "subFunctionNotSupportedInActiveSession",
        SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION:
            "serviceNotSupportedInActiveSession",
    }


class Session(object):
    """诊断会话类型。"""

    DEFAULT = 0x01
    PROGRAMMING = 0x02
    EXTENDED = 0x03

    NAMES = {DEFAULT: "default", PROGRAMMING: "programming", EXTENDED: "extended"}


class ResetType(object):
    """ECU 复位类型。"""

    HARD = 0x01
    KEY_OFF_ON = 0x02
    SOFT = 0x03

    ALL = (HARD, KEY_OFF_ON, SOFT)


class RoutineControlType(object):
    """例程控制子功能。"""

    START = 0x01
    STOP = 0x02
    REQUEST_RESULTS = 0x03

    ALL = (START, STOP, REQUEST_RESULTS)


class DTCStatusMask(object):
    """DTC 状态字节的位定义（ISO 14229-1 表 D.2 的常用子集）。"""

    TEST_FAILED = 0x01
    TEST_FAILED_THIS_OPERATION_CYCLE = 0x02
    PENDING_DTC = 0x04
    CONFIRMED_DTC = 0x08
    TEST_NOT_COMPLETED_SINCE_CLEAR = 0x10
    TEST_FAILED_SINCE_CLEAR = 0x20
    TEST_NOT_COMPLETED_THIS_OPERATION_CYCLE = 0x40
    WARNING_INDICATOR_REQUESTED = 0x80


class ReadDTCSubFunction(object):
    """0x19 服务的子功能。"""

    REPORT_NUMBER_OF_DTC_BY_STATUS_MASK = 0x01
    REPORT_DTC_BY_STATUS_MASK = 0x02
    REPORT_DTC_SNAPSHOT_IDENTIFICATION = 0x03
    REPORT_DTC_SNAPSHOT_RECORD_BY_DTC_NUMBER = 0x04
    REPORT_DTC_EXTENDED_DATA_RECORD_BY_DTC_NUMBER = 0x06

    ALL = (REPORT_NUMBER_OF_DTC_BY_STATUS_MASK, REPORT_DTC_BY_STATUS_MASK,
           REPORT_DTC_SNAPSHOT_IDENTIFICATION,
           REPORT_DTC_SNAPSHOT_RECORD_BY_DTC_NUMBER,
           REPORT_DTC_EXTENDED_DATA_RECORD_BY_DTC_NUMBER)


#: 抑制正响应位
SUPPRESS_POS_RESPONSE = 0x80

#: DTC 状态可用性掩码：本 ECU 支持哪些状态位
DTC_STATUS_AVAILABILITY_MASK = (
    DTCStatusMask.TEST_FAILED
    | DTCStatusMask.CONFIRMED_DTC
    | DTCStatusMask.PENDING_DTC
    | DTCStatusMask.TEST_NOT_COMPLETED_SINCE_CLEAR
    | DTCStatusMask.TEST_FAILED_SINCE_CLEAR
    | DTCStatusMask.WARNING_INDICATOR_REQUESTED
)


# ---------------------------------------------------------------------------
# 定时参数
# ---------------------------------------------------------------------------


@dataclass
class UdsTiming(object):
    """诊断定时参数（毫秒）。"""

    #: 收到请求到首个响应的最大时间
    p2_server_ms: float = 50.0
    #: 发出 0x78 之后到下一个响应的最大时间
    p2_star_server_ms: float = 5000.0
    #: 客户端等待首个响应的超时（一般取 P2_server，略留余量）
    p2_client_ms: float = 150.0
    #: 客户端在收到 0x78 之后等待的超时
    p2_star_client_ms: float = 5100.0
    #: S3 会话保持定时（默认会话下无请求的容忍时间）
    s3_server_ms: float = 5000.0


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class Dtc(object):
    """一个诊断故障码。"""

    code: int
    status: int = DTCStatusMask.CONFIRMED_DTC | DTCStatusMask.TEST_FAILED
    snapshot: bytes = b""
    extended: bytes = b""

    def three_bytes(self) -> bytes:
        """3 字节 DTC 编码（ISO 14229-1 的高中低字节顺序）。"""
        return self.code.to_bytes(3, "big")


@dataclass
class Routine(object):
    """一个例程。"""

    identifier: int
    name: str = ""
    #: 例程执行结果（由调用方注入，便于测试）
    result: bytes = b"\x00"
    running: bool = False
    #: 是否允许重复启动
    restartable: bool = False
    #: 启动时返回的 NRC（非 0 表示启动失败）
    start_nrc: int = 0x00


@dataclass
class DiagnosticRequest(object):
    """一次诊断请求。"""

    data: bytes
    #: 请求到达的虚拟时刻（毫秒）
    timestamp_ms: float = 0.0

    @property
    def service(self) -> int:
        return self.data[0] if self.data else 0

    @property
    def sub_function(self) -> int:
        if len(self.data) < 2:
            return 0
        return self.data[1] & 0x7F

    @property
    def suppress_positive_response(self) -> bool:
        return len(self.data) >= 2 and bool(self.data[1] & SUPPRESS_POS_RESPONSE)


@dataclass
class DiagnosticResponse(object):
    """一次诊断响应。"""

    data: bytes
    #: 产生该响应的虚拟时刻
    timestamp_ms: float = 0.0
    #: 是否是负响应
    negative: bool = False
    #: 是否被抑制（无响应）
    suppressed: bool = False
    #: 是否先发了 0x78
    pending_sent: bool = False

    @property
    def nrc(self) -> int:
        return self.data[2] if self.negative and len(self.data) >= 3 else 0x00

    @property
    def nrc_name(self) -> str:
        return NRC.NAMES.get(self.nrc, "0x%02X" % self.nrc)


# ---------------------------------------------------------------------------
# 安全访问算法（自研，可配置）
# ---------------------------------------------------------------------------


def default_key_algorithm(seed: bytes, level: int = 0x01) -> bytes:
    """默认的种子→密钥算法（自研、可替换）。

    算法（教学用，**不是任何厂商的真实算法**）：

    1. 把种子按 16 位大端分成若干字，逐字做异或折叠得到 16 位状态 ``s``；
    2. ``s = (s * 0x0801 + 0x1234 + level) & 0xFFFF`` 迭代 8 轮（线性同余）；
    3. 交换高低字节后输出 2 字节。

    该算法与 ISO 14229-1 无关，ECU 与客户端必须使用同一个实现才能通过。
    """
    if not seed:
        raise ValueError("seed must not be empty")
    state = 0
    for index in range(0, len(seed) - 1, 2):
        state ^= (seed[index] << 8) | seed[index + 1]
    if len(seed) % 2:
        state ^= seed[-1]
    for _ in range(8):
        state = (state * 0x0801 + 0x1234 + level) & 0xFFFF
    return bytes([(state >> 8) & 0xFF, state & 0xFF])


# ---------------------------------------------------------------------------
# 服务端（ECU）
# ---------------------------------------------------------------------------


class UdsServer(object):
    """一个软件 ECU，实现 ISO 14229-1 的诊断服务子集。

    :param timing: 定时参数。
    :param key_algorithm: 种子→密钥算法，默认 :func:`default_key_algorithm`。
    :param clock: 返回当前虚拟毫秒的回调。
    """

    def __init__(
        self,
        timing: Optional[UdsTiming] = None,
        key_algorithm: Optional[Callable[[bytes, int], bytes]] = None,
        clock: Optional[Callable[[], float]] = None,
        name: str = "ECU",
    ) -> None:
        self.name = name
        self.timing = timing or UdsTiming()
        self.key_algorithm = key_algorithm or default_key_algorithm
        self._clock = clock or (lambda: 0.0)

        #: 当前会话
        self.session = Session.DEFAULT
        #: 安全等级；0 表示未解锁
        self.security_level = 0
        #: 每个安全等级的种子与尝试记录
        self.seeds: Dict[int, bytes] = {}
        self.failed_attempts: Dict[int, int] = {}
        self.lockout_until_ms: Dict[int, float] = {}
        #: 允许的最大连续失败次数
        self.max_attempts = 3
        #: 超过尝试次数后的锁定时间
        self.attempt_lockout_ms = 10000.0

        #: 支持的服务
        self.supported_services = set(Service.ALL)
        #: 各服务支持的子功能；缺省表示该服务没有子功能定义
        self.supported_sub_functions: Dict[int, Tuple[int, ...]] = {
            Service.DIAGNOSTIC_SESSION_CONTROL: (Session.DEFAULT, Session.PROGRAMMING,
                                                 Session.EXTENDED),
            Service.ECU_RESET: tuple(ResetType.ALL),
            Service.SECURITY_ACCESS: (0x01, 0x02, 0x03, 0x04),
            Service.READ_DTC_INFORMATION: tuple(ReadDTCSubFunction.ALL),
            Service.ROUTINE_CONTROL: tuple(RoutineControlType.ALL),
            Service.TESTER_PRESENT: (0x00,),
        }
        #: 会话内可用的服务（未列出的按全局支持判断）
        self.session_services: Dict[int, Tuple[int, ...]] = {
            Session.PROGRAMMING: (
                Service.DIAGNOSTIC_SESSION_CONTROL, Service.ECU_RESET,
                Service.CLEAR_DIAGNOSTIC_INFORMATION, Service.READ_DTC_INFORMATION,
                Service.READ_DATA_BY_IDENTIFIER, Service.SECURITY_ACCESS,
                Service.WRITE_DATA_BY_IDENTIFIER, Service.ROUTINE_CONTROL,
                Service.TESTER_PRESENT,
            ),
        }

        #: DID 数据库
        self.data: Dict[int, bytes] = {}
        #: 每个 DID 的读写权限：None 表示读写均可，True 表示只有解锁后能读
        self.read_requires_security: Dict[int, bool] = {}
        self.write_requires_security: Dict[int, bool] = {}
        #: 每个 DID 允许写入的会话
        self.write_sessions: Dict[int, Tuple[int, ...]] = {}
        #: 只读 DID
        self.read_only: set = set()

        #: DTC 存储
        self.dtcs: List[Dtc] = []
        #: 例程
        self.routines: Dict[int, Routine] = {}

        #: 复位计数
        self.reset_count = 0
        self.last_reset_type = 0
        #: 会话切换次数
        self.session_changes = 0
        #: 最后一次请求的时刻（用于 S3 定时）
        self.last_request_ms: Optional[float] = None
        #: 事件日志
        self.events: List[str] = []
        #: 注入的响应延迟（毫秒），用于 P2 / P2* 测试
        self.response_delay_ms = 0.0
        #: 注入的 0x78（responsePending）次数
        self.pending_count = 0
        #: 已处理的请求数
        self.requests_handled = 0

    # -- 配置 -------------------------------------------------------------

    def add_did(self, did: int, value: bytes, requires_security: bool = False,
                read_only: bool = False,
                write_sessions: Optional[Sequence[int]] = None) -> None:
        """注册一个数据标识符。"""
        self.data[did] = bytes(value)
        self.read_requires_security[did] = requires_security
        self.write_requires_security[did] = requires_security
        if read_only:
            self.read_only.add(did)
        if write_sessions is not None:
            self.write_sessions[did] = tuple(write_sessions)

    def add_dtc(self, code: int, status: int = DTCStatusMask.CONFIRMED_DTC
                | DTCStatusMask.TEST_FAILED, snapshot: bytes = b"",
                extended: bytes = b"") -> Dtc:
        """加入一个 DTC。"""
        dtc = Dtc(code=code, status=status, snapshot=snapshot, extended=extended)
        self.dtcs.append(dtc)
        return dtc

    def add_routine(self, identifier: int, name: str = "",
                    result: bytes = b"\x00", restartable: bool = False,
                    start_nrc: int = 0) -> Routine:
        """注册一个例程。"""
        routine = Routine(identifier=identifier, name=name, result=bytes(result),
                          restartable=restartable, start_nrc=start_nrc)
        self.routines[identifier] = routine
        return routine

    # -- 时间相关 ---------------------------------------------------------

    @property
    def now_ms(self) -> float:
        return self._clock()

    def s3_expired(self) -> bool:
        """默认/非默认会话是否已因 S3 超时回落到默认会话。"""
        if self.session == Session.DEFAULT or self.last_request_ms is None:
            return False
        if self.now_ms - self.last_request_ms > self.timing.s3_server_ms:
            self._enter_session(Session.DEFAULT)
            self.events.append("S3 timeout: fell back to default session")
            return True
        return False

    def _enter_session(self, session: int) -> None:
        self.session = session
        self.session_changes += 1
        # 真实 ECU 行为：切换会话会复位安全状态
        self.security_level = 0
        self.seeds.clear()
        self.events.append("session -> %s" % Session.NAMES.get(session, session))

    # -- 请求处理 ---------------------------------------------------------

    def handle(self, request: bytes, timestamp_ms: Optional[float] = None) -> DiagnosticResponse:
        """处理一次诊断请求，返回响应。

        :param request: 完整请求数据（不含 ISO-TP 头）。
        :param timestamp_ms: 请求到达的虚拟时刻。
        """
        now = self.now_ms if timestamp_ms is None else timestamp_ms
        self.last_request_ms = now
        self.requests_handled += 1
        req = DiagnosticRequest(data=bytes(request), timestamp_ms=now)

        pending_sent = False
        if self.pending_count > 0:
            self.pending_count -= 1
            pending_sent = True

        if not req.data:
            return DiagnosticResponse(
                data=bytes([0x7F, 0x00, NRC.INCORRECT_MESSAGE_LENGTH]),
                timestamp_ms=now + self.response_delay_ms, negative=True,
            )

        response = self._dispatch(req, now)
        response.pending_sent = pending_sent
        if response.suppressed:
            return response
        return response

    def _negative(self, service: int, nrc: int, now: float) -> DiagnosticResponse:
        return DiagnosticResponse(
            data=bytes([0x7F, service, nrc]),
            timestamp_ms=now + self.response_delay_ms,
            negative=True,
        )

    def _positive(self, data: bytes, now: float) -> DiagnosticResponse:
        return DiagnosticResponse(data=bytes(data),
                                  timestamp_ms=now + self.response_delay_ms)

    def _dispatch(self, req: DiagnosticRequest, now: float) -> DiagnosticResponse:
        service = req.service
        if service not in Service.ALL:
            return self._negative(service, NRC.SERVICE_NOT_SUPPORTED, now)
        if service not in self.supported_services:
            return self._negative(service, NRC.SERVICE_NOT_SUPPORTED, now)
        allowed = self.session_services.get(self.session)
        if allowed is not None and service not in allowed:
            return self._negative(service, NRC.SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION,
                                  now)

        handler = {
            Service.DIAGNOSTIC_SESSION_CONTROL: self._svc_session_control,
            Service.ECU_RESET: self._svc_ecu_reset,
            Service.CLEAR_DIAGNOSTIC_INFORMATION: self._svc_clear_dtc,
            Service.READ_DTC_INFORMATION: self._svc_read_dtc,
            Service.READ_DATA_BY_IDENTIFIER: self._svc_read_data,
            Service.SECURITY_ACCESS: self._svc_security_access,
            Service.WRITE_DATA_BY_IDENTIFIER: self._svc_write_data,
            Service.ROUTINE_CONTROL: self._svc_routine_control,
            Service.TESTER_PRESENT: self._svc_tester_present,
        }[service]
        return handler(req, now)

    # -- 各服务实现 -------------------------------------------------------

    def _check_sub_function(self, req: DiagnosticRequest, now: float
                            ) -> Optional[DiagnosticResponse]:
        """校验子功能是否受支持。"""
        supported = self.supported_sub_functions.get(req.service)
        if supported is None:
            return None
        if req.sub_function not in supported:
            return self._negative(req.service, NRC.SUB_FUNCTION_NOT_SUPPORTED, now)
        return None

    def _svc_session_control(self, req: DiagnosticRequest, now: float) -> DiagnosticResponse:
        if len(req.data) != 2:
            return self._negative(req.service, NRC.INCORRECT_MESSAGE_LENGTH, now)
        bad = self._check_sub_function(req, now)
        if bad is not None:
            return bad
        session = req.sub_function
        self._enter_session(session)
        p2 = int(self.timing.p2_server_ms)
        p2star = int(self.timing.p2_star_server_ms)
        body = bytes([0x50, session,
                      (p2 >> 8) & 0xFF, p2 & 0xFF,
                      (p2star >> 8) & 0xFF, p2star & 0xFF])
        if req.suppress_positive_response:
            return DiagnosticResponse(data=b"", timestamp_ms=now, suppressed=True)
        return self._positive(body, now)

    def _svc_ecu_reset(self, req: DiagnosticRequest, now: float) -> DiagnosticResponse:
        if len(req.data) != 2:
            return self._negative(req.service, NRC.INCORRECT_MESSAGE_LENGTH, now)
        bad = self._check_sub_function(req, now)
        if bad is not None:
            return bad
        self.reset_count += 1
        self.last_reset_type = req.sub_function
        # 复位后回到默认会话并清除安全状态；同时清掉安全访问的失败计数与锁定窗口
        self._enter_session(Session.DEFAULT)
        self.failed_attempts.clear()
        self.lockout_until_ms.clear()
        self.seeds.clear()
        self.security_level = 0
        self.events.append("ECU reset type=0x%02X" % req.sub_function)
        if req.suppress_positive_response:
            return DiagnosticResponse(data=b"", timestamp_ms=now, suppressed=True)
        return self._positive(bytes([0x51, req.sub_function]), now)

    def _svc_clear_dtc(self, req: DiagnosticRequest, now: float) -> DiagnosticResponse:
        if len(req.data) != 4:
            return self._negative(req.service, NRC.INCORRECT_MESSAGE_LENGTH, now)
        group = int.from_bytes(req.data[1:4], "big")
        # 会话要求：清 DTC 只在非默认会话下允许（常见整车行为）
        if self.session == Session.DEFAULT:
            return self._negative(req.service, NRC.CONDITIONS_NOT_CORRECT, now)
        if group == 0xFFFFFF:
            self.dtcs = []
        else:
            self.dtcs = [d for d in self.dtcs if (d.code >> 16) != ((group >> 16) & 0xFF)]
        self.events.append("clear DTC group=0x%06X remaining=%d" % (group, len(self.dtcs)))
        # 注意：0x14 的 byte1 是 groupOfDTC 的高字节而不是子功能字节，
        # 因此这里不解释"抑制正响应位"。
        return self._positive(bytes([0x54]), now)

    def _dtcs_matching(self, mask: int) -> List[Dtc]:
        return [d for d in self.dtcs if d.status & mask]

    def _svc_read_dtc(self, req: DiagnosticRequest, now: float) -> DiagnosticResponse:
        if len(req.data) < 2:
            return self._negative(req.service, NRC.INCORRECT_MESSAGE_LENGTH, now)
        bad = self._check_sub_function(req, now)
        if bad is not None:
            return bad
        sub = req.sub_function
        # 各子功能的最小请求长度（含 SID 与子功能字节）
        #   0x01 报告数量   : 19 01 <mask>                        -> 3
        #   0x02 报告 DTC   : 19 02 <mask>                        -> 3
        #   0x03 快照标识   : 19 03 <DTC[3]> <recordNumber>       -> 6
        #   0x04 快照记录   : 19 04 <DTC[3]> <recordNumber>       -> 6
        #   0x06 扩展数据   : 19 06 <DTC[3]> <recordNumber>       -> 6
        required = {
            ReadDTCSubFunction.REPORT_NUMBER_OF_DTC_BY_STATUS_MASK: 3,
            ReadDTCSubFunction.REPORT_DTC_BY_STATUS_MASK: 3,
            ReadDTCSubFunction.REPORT_DTC_SNAPSHOT_IDENTIFICATION: 6,
            ReadDTCSubFunction.REPORT_DTC_SNAPSHOT_RECORD_BY_DTC_NUMBER: 6,
            ReadDTCSubFunction.REPORT_DTC_EXTENDED_DATA_RECORD_BY_DTC_NUMBER: 6,
        }
        if sub not in required:
            return self._negative(req.service, NRC.SUB_FUNCTION_NOT_SUPPORTED, now)
        if len(req.data) < required[sub]:
            return self._negative(req.service, NRC.INCORRECT_MESSAGE_LENGTH, now)
        # 状态掩码：0x01/0x02 在 byte2；带 DTC 号的子功能用全状态掩码
        mask = req.data[2] if sub in (
            ReadDTCSubFunction.REPORT_NUMBER_OF_DTC_BY_STATUS_MASK,
            ReadDTCSubFunction.REPORT_DTC_BY_STATUS_MASK) else 0xFF

        matched = self._dtcs_matching(mask)
        if sub == ReadDTCSubFunction.REPORT_NUMBER_OF_DTC_BY_STATUS_MASK:
            count = len(matched)
            # 响应：59 01 <statusMask> <DTCStatusAvailabilityMask> <countHi> <countLo>
            body = bytes([0x59, sub, mask, DTC_STATUS_AVAILABILITY_MASK,
                          (count >> 8) & 0xFF, count & 0xFF])
            return self._positive(body, now)
        if sub == ReadDTCSubFunction.REPORT_DTC_BY_STATUS_MASK:
            if not matched:
                return self._negative(req.service, NRC.REQUEST_OUT_OF_RANGE, now)
            body = bytes([0x59, sub, mask])
            for dtc in matched:
                body += dtc.three_bytes() + bytes([dtc.status])
            return self._positive(body, now)
        if sub in (ReadDTCSubFunction.REPORT_DTC_SNAPSHOT_IDENTIFICATION,
                   ReadDTCSubFunction.REPORT_DTC_SNAPSHOT_RECORD_BY_DTC_NUMBER):
            if not matched:
                return self._negative(req.service, NRC.REQUEST_OUT_OF_RANGE, now)
            body = bytes([0x59, sub]) + matched[0].three_bytes() + bytes([matched[0].status])
            body += bytes([0xFF, len(matched[0].snapshot)]) + matched[0].snapshot
            return self._positive(body, now)
        # 0x06 扩展数据
        if not matched:
            return self._negative(req.service, NRC.REQUEST_OUT_OF_RANGE, now)
        body = bytes([0x59, sub]) + matched[0].three_bytes()
        body += bytes([0x01, len(matched[0].extended)]) + matched[0].extended
        return self._positive(body, now)

    def _svc_read_data(self, req: DiagnosticRequest, now: float) -> DiagnosticResponse:
        if len(req.data) < 3 or (len(req.data) - 1) % 2 != 0:
            return self._negative(req.service, NRC.INCORRECT_MESSAGE_LENGTH, now)
        body = bytes([0x62])
        for index in range(1, len(req.data), 2):
            did = (req.data[index] << 8) | req.data[index + 1]
            if did not in self.data:
                return self._negative(req.service, NRC.REQUEST_OUT_OF_RANGE, now)
            if self.read_requires_security.get(did, False) and self.security_level == 0:
                return self._negative(req.service, NRC.SECURITY_ACCESS_DENIED, now)
            body += bytes([(did >> 8) & 0xFF, did & 0xFF]) + self.data[did]
        return self._positive(body, now)

    def _svc_write_data(self, req: DiagnosticRequest, now: float) -> DiagnosticResponse:
        if len(req.data) < 4:
            return self._negative(req.service, NRC.INCORRECT_MESSAGE_LENGTH, now)
        did = (req.data[1] << 8) | req.data[2]
        value = req.data[3:]
        if did not in self.data:
            return self._negative(req.service, NRC.REQUEST_OUT_OF_RANGE, now)
        if did in self.read_only:
            return self._negative(req.service, NRC.REQUEST_OUT_OF_RANGE, now)
        allowed_sessions = self.write_sessions.get(did)
        if allowed_sessions is not None and self.session not in allowed_sessions:
            return self._negative(req.service, NRC.REQUEST_OUT_OF_RANGE, now)
        if self.write_requires_security.get(did, False) and self.security_level == 0:
            return self._negative(req.service, NRC.SECURITY_ACCESS_DENIED, now)
        self.data[did] = bytes(value)
        self.events.append("write DID 0x%04X = %s" % (did, value.hex().upper()))
        # 同上：0x2E 的 byte1 是 DID 高字节，没有子功能字节
        return self._positive(bytes([0x6E, (did >> 8) & 0xFF, did & 0xFF]), now)

    def _svc_security_access(self, req: DiagnosticRequest, now: float) -> DiagnosticResponse:
        if len(req.data) < 2:
            return self._negative(req.service, NRC.INCORRECT_MESSAGE_LENGTH, now)
        bad = self._check_sub_function(req, now)
        if bad is not None:
            return bad
        sub = req.sub_function
        level = (sub + 1) // 2 if sub % 2 == 1 else sub // 2
        # 锁定窗口的检查必须放在最前面：窗口内一律 0x37，即使尝试次数已达上限
        if now < self.lockout_until_ms.get(sub, 0.0):
            return self._negative(req.service, NRC.REQUIRED_TIME_DELAY_NOT_EXPIRED, now)
        if self.failed_attempts.get(sub, 0) >= self.max_attempts:
            return self._negative(req.service, NRC.EXCEED_NUMBER_OF_ATTEMPTS, now)

        if sub % 2 == 1:
            # 请求种子
            if len(req.data) != 2:
                return self._negative(req.service, NRC.INCORRECT_MESSAGE_LENGTH, now)
            # 超过尝试次数后（无论锁定窗口是否已过）都不再发放种子
            if self.failed_attempts.get(sub, 0) >= self.max_attempts:
                return self._negative(req.service, NRC.EXCEED_NUMBER_OF_ATTEMPTS, now)
            seed = self._make_seed(level, now)
            self.seeds[sub] = seed
            if req.suppress_positive_response:
                return DiagnosticResponse(data=b"", timestamp_ms=now, suppressed=True)
            return self._positive(bytes([0x67, sub]) + seed, now)

        # 发送密钥
        if sub % 2 != 0:
            return self._negative(req.service, NRC.SUB_FUNCTION_NOT_SUPPORTED, now)
        request_sub = sub - 1
        if request_sub not in self.seeds:
            return self._negative(req.service, NRC.REQUEST_SEQUENCE_ERROR, now)
        expected = self.key_algorithm(self.seeds[request_sub], level)
        key = req.data[2:]
        if len(key) != len(expected):
            return self._negative(req.service, NRC.INVALID_KEY, now)
        if key != expected:
            self.failed_attempts[sub] = self.failed_attempts.get(sub, 0) + 1
            if self.failed_attempts[sub] >= self.max_attempts:
                self.lockout_until_ms[sub] = now + self.attempt_lockout_ms
                self.events.append("security level %d locked out" % level)
                return self._negative(req.service, NRC.EXCEED_NUMBER_OF_ATTEMPTS, now)
            return self._negative(req.service, NRC.INVALID_KEY, now)
        self.security_level = level
        self.failed_attempts[sub] = 0
        self.seeds.pop(request_sub, None)
        self.events.append("security level %d unlocked" % level)
        if req.suppress_positive_response:
            return DiagnosticResponse(data=b"", timestamp_ms=now, suppressed=True)
        return self._positive(bytes([0x67, sub]), now)

    def _make_seed(self, level: int, now: float) -> bytes:
        """生成种子（确定性：由安全等级与请求序号推导，便于可复现测试）。"""
        value = (0x1234 + level * 0x100 + self.requests_handled) & 0xFFFF
        return bytes([(value >> 8) & 0xFF, value & 0xFF])

    def _svc_routine_control(self, req: DiagnosticRequest, now: float) -> DiagnosticResponse:
        if len(req.data) < 4:
            return self._negative(req.service, NRC.INCORRECT_MESSAGE_LENGTH, now)
        bad = self._check_sub_function(req, now)
        if bad is not None:
            return bad
        sub = req.sub_function
        routine_id = (req.data[2] << 8) | req.data[3]
        if routine_id not in self.routines:
            return self._negative(req.service, NRC.REQUEST_OUT_OF_RANGE, now)
        routine = self.routines[routine_id]
        if sub == RoutineControlType.START:
            if routine.running and not routine.restartable:
                return self._negative(req.service, NRC.REQUEST_SEQUENCE_ERROR, now)
            if routine.start_nrc:
                return self._negative(req.service, routine.start_nrc, now)
            routine.running = True
            body = bytes([0x71, sub, (routine_id >> 8) & 0xFF, routine_id & 0xFF])
            return self._positive(body, now)
        if sub == RoutineControlType.STOP:
            if not routine.running:
                return self._negative(req.service, NRC.REQUEST_SEQUENCE_ERROR, now)
            routine.running = False
            body = bytes([0x71, sub, (routine_id >> 8) & 0xFF, routine_id & 0xFF])
            return self._positive(body, now)
        # REQUEST_RESULTS
        body = bytes([0x71, sub, (routine_id >> 8) & 0xFF, routine_id & 0xFF])
        body += bytes([len(routine.result)]) + routine.result
        return self._positive(body, now)

    def _svc_tester_present(self, req: DiagnosticRequest, now: float
                            ) -> DiagnosticResponse:
        if len(req.data) != 2:
            return self._negative(req.service, NRC.INCORRECT_MESSAGE_LENGTH, now)
        bad = self._check_sub_function(req, now)
        if bad is not None:
            return bad
        if req.suppress_positive_response:
            return DiagnosticResponse(data=b"", timestamp_ms=now, suppressed=True)
        return self._positive(bytes([0x7E, req.sub_function]), now)

    # -- 内部辅助 ---------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<UdsServer %s session=%s sec=%d dtcs=%d>" % (
            self.name, Session.NAMES.get(self.session, self.session),
            self.security_level, len(self.dtcs)
        )


# ---------------------------------------------------------------------------
# 客户端（诊断仪）
# ---------------------------------------------------------------------------


@dataclass
class UdsExchange(object):
    """一次请求/响应往返的记录。"""

    request: bytes
    response: bytes
    request_ms: float = 0.0
    response_ms: float = 0.0
    negative: bool = False
    nrc: int = 0x00
    pending_count: int = 0

    @property
    def elapsed_ms(self) -> float:
        return self.response_ms - self.request_ms

    @property
    def positive(self) -> bool:
        return not self.negative


class UdsClient(object):
    """诊断仪侧：把 UDS 请求发给 :class:`UdsServer` 并检查定时。

    客户端会检查 P2 / P2\\* 定时：若响应迟于 ``p2_client_ms``（未收到 0x78）
    或迟于 ``p2_star_client_ms``（收到过 0x78），抛出超时错误。
    """

    def __init__(self, server: UdsServer, timing: Optional[UdsTiming] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.server = server
        self.timing = timing or server.timing
        self._clock = clock or (lambda: server.now_ms)
        self.exchanges: List[UdsExchange] = []
        self.timeouts = 0
        #: 安全等级（客户端自己的视图）
        self.security_level = 0

    @property
    def now_ms(self) -> float:
        return self._clock()

    def request(self, data: bytes) -> DiagnosticResponse:
        """发送一次请求。"""
        now = self.now_ms
        response = self.server.handle(data, timestamp_ms=now)
        if response.suppressed:
            exchange = UdsExchange(request=bytes(data), response=b"",
                                   request_ms=now, response_ms=response.timestamp_ms)
            self.exchanges.append(exchange)
            return response
        limit = (self.timing.p2_star_client_ms if response.pending_sent
                 else self.timing.p2_client_ms)
        elapsed = response.timestamp_ms - now
        if elapsed > limit:
            self.timeouts += 1
            raise UdsTimeout(
                "response after %.3f ms exceeds %s (%.3f ms)"
                % (elapsed, "P2*" if response.pending_sent else "P2", limit)
            )
        exchange = UdsExchange(
            request=bytes(data), response=bytes(response.data),
            request_ms=now, response_ms=response.timestamp_ms,
            negative=response.negative, nrc=response.nrc,
            pending_count=1 if response.pending_sent else 0,
        )
        self.exchanges.append(exchange)
        return response

    # -- 高层封装 ---------------------------------------------------------

    def session_control(self, session: int, suppress: bool = False) -> DiagnosticResponse:
        """0x10 会话控制。"""
        sub = session | (SUPPRESS_POS_RESPONSE if suppress else 0x00)
        return self.request(bytes([Service.DIAGNOSTIC_SESSION_CONTROL, sub]))

    def ecu_reset(self, reset_type: int = ResetType.HARD) -> DiagnosticResponse:
        """0x11 ECU 复位。"""
        return self.request(bytes([Service.ECU_RESET, reset_type]))

    def read_data(self, did: int) -> bytes:
        """0x22 读数据；返回 DID 对应的数据。"""
        response = self.request(bytes([Service.READ_DATA_BY_IDENTIFIER,
                                       (did >> 8) & 0xFF, did & 0xFF]))
        if response.negative:
            raise UdsNegativeResponse(response)
        if len(response.data) < 3 or response.data[0] != 0x62:
            raise UdsProtocolError("malformed 0x22 response: %s" % response.data.hex())
        return response.data[3:]

    def write_data(self, did: int, value: bytes) -> DiagnosticResponse:
        """0x2E 写数据。"""
        return self.request(bytes([Service.WRITE_DATA_BY_IDENTIFIER,
                                   (did >> 8) & 0xFF, did & 0xFF]) + bytes(value))

    def security_access_unlock(self, level: int = 0x01) -> DiagnosticResponse:
        """0x27 完整解锁流程：请求种子 → 计算密钥 → 发送密钥。"""
        request_sub = level * 2 - 1
        seed_response = self.request(bytes([Service.SECURITY_ACCESS, request_sub]))
        if seed_response.negative:
            raise UdsNegativeResponse(seed_response)
        seed = seed_response.data[2:]
        key = self.server.key_algorithm(seed, level)
        response = self.request(bytes([Service.SECURITY_ACCESS, request_sub + 1]) + key)
        if not response.negative:
            self.security_level = level
        return response

    def read_dtc_count(self, mask: int = 0xFF) -> int:
        """0x19 子功能 0x01：读取符合状态掩码的 DTC 数量。"""
        response = self.request(bytes([
            Service.READ_DTC_INFORMATION,
            ReadDTCSubFunction.REPORT_NUMBER_OF_DTC_BY_STATUS_MASK, mask]))
        if response.negative:
            raise UdsNegativeResponse(response)
        return (response.data[-2] << 8) | response.data[-1]

    def read_dtcs(self, mask: int = 0xFF) -> List[Tuple[int, int]]:
        """0x19 子功能 0x02：返回 ``[(dtc, status), ...]``。"""
        response = self.request(bytes([
            Service.READ_DTC_INFORMATION,
            ReadDTCSubFunction.REPORT_DTC_BY_STATUS_MASK, mask]))
        if response.negative:
            raise UdsNegativeResponse(response)
        body = response.data[3:]
        out: List[Tuple[int, int]] = []
        for index in range(0, len(body) - 3, 4):
            code = int.from_bytes(body[index:index + 3], "big")
            out.append((code, body[index + 3]))
        return out

    def clear_dtc(self, group: int = 0xFFFFFF) -> DiagnosticResponse:
        """0x14 清 DTC。"""
        return self.request(bytes([Service.CLEAR_DIAGNOSTIC_INFORMATION])
                            + group.to_bytes(3, "big"))

    def routine_control(self, sub_function: int, routine_id: int) -> DiagnosticResponse:
        """0x31 例程控制。"""
        return self.request(bytes([Service.ROUTINE_CONTROL, sub_function,
                                   (routine_id >> 8) & 0xFF, routine_id & 0xFF]))

    def tester_present(self, suppress: bool = True) -> DiagnosticResponse:
        """0x3E TesterPresent。"""
        sub = 0x00 | (SUPPRESS_POS_RESPONSE if suppress else 0x00)
        return self.request(bytes([Service.TESTER_PRESENT, sub]))

    def last_exchange(self) -> Optional[UdsExchange]:
        return self.exchanges[-1] if self.exchanges else None


class UdsTimeout(Exception):
    """客户端侧 P2 / P2* 定时超时。"""


class UdsNegativeResponse(Exception):
    """服务端返回负响应。"""

    def __init__(self, response: DiagnosticResponse) -> None:
        super(UdsNegativeResponse, self).__init__(
            "NRC 0x%02X (%s)" % (response.nrc, response.nrc_name)
        )
        self.nrc = response.nrc
        self.response = response


class UdsProtocolError(Exception):
    """响应格式与预期不符。"""
