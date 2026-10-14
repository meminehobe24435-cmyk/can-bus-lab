# -*- coding: utf-8 -*-
"""ISO 15765-2 (CAN 传输层 / ISO-TP) 实现。

支持四种协议数据单元（PDU）：

=======  ==========================  ==========================================
类型      首字节                      说明
=======  ==========================  ==========================================
SF        0x0n                        单帧，``n`` = 数据长度（经典 CAN 0-7；
                                     CAN FD 下为 0 时用第二个字节表示长度）
FF        0x1L LL                     首帧，12 位长度；长度为 0 时用后续
                                     4 字节表示 32 位扩展长度
CF        0x2n                        连续帧，``n`` = 序号 SN (0-15)
FC        0x3F BS STmin               流控帧，``F`` = FS (0=CTS,1=WAIT,2=OVFLW)
=======  ==========================  ==========================================

超时（ISO 15765-2 表 11 的六个网络层定时参数）：

================  ==========================================================
N_As              发送方：从请求发送到帧真正发出
N_Ar              接收方：从请求发送流控帧到其真正发出
N_Bs              发送方：等待流控帧
N_Br              接收方：等待下一个连续帧前发送流控帧
N_Cs              发送方：等待连续帧 / 发送连续帧
N_Cr              接收方：等待下一个连续帧
================  ==========================================================

本实现用**虚拟时间**（毫秒）驱动，所有定时参数可配置。默认值取自
ISO 15765-2 的典型推荐（N_Bs=1000ms、N_Cr=1000ms，其余 1000ms）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .frame import Frame

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 协议数据单元类型
N_PCI_SF = 0x0
N_PCI_FF = 0x1
N_PCI_CF = 0x2
N_PCI_FC = 0x3

#: 流控状态
FS_CTS = 0x0      # ContinueToSend
FS_WAIT = 0x1     # Wait
FS_OVFLW = 0x2    # Overflow

FC_NAMES = {FS_CTS: "CTS", FS_WAIT: "WAIT", FS_OVFLW: "OVFLW"}

#: 默认网络层超时（毫秒）
DEFAULT_N_AS_MS = 1000.0
DEFAULT_N_AR_MS = 1000.0
DEFAULT_N_BS_MS = 1000.0
DEFAULT_N_BR_MS = 1000.0
DEFAULT_N_CS_MS = 1000.0
DEFAULT_N_CR_MS = 1000.0

#: 经典 CAN 下单帧能携带的最大数据长度（1 字节 PCI）
CLASSIC_SF_MAX = 7
#: 经典 CAN 下首帧可表示的 12 位长度上限
FF_12BIT_MAX = 0xFFF


class IsoTpErrorKind(object):
    """ISO-TP 协议错误类型。"""

    UNEXPECTED_PDU = "unexpected-pdu"
    WRONG_SN = "wrong-sequence-number"
    OVERFLOW = "flow-control-overflow"
    TIMEOUT = "timeout"
    LENGTH_MISMATCH = "length-mismatch"
    MALFORMED = "malformed-pdu"
    ABORTED = "aborted"

    ALL = (UNEXPECTED_PDU, WRONG_SN, OVERFLOW, TIMEOUT,
           LENGTH_MISMATCH, MALFORMED, ABORTED)


class IsoTpError(Exception):
    """ISO-TP 层错误（携带错误类型与超时参数名）。"""

    def __init__(self, kind: str, message: str, timeout_name: str = "") -> None:
        super(IsoTpError, self).__init__(message)
        self.kind = kind
        self.timeout_name = timeout_name

    def __str__(self) -> str:  # pragma: no cover - 继承自父类
        return super(IsoTpError, self).__str__()


# ---------------------------------------------------------------------------
# STmin 编解码
# ---------------------------------------------------------------------------


def stmin_to_ms(value: int) -> float:
    """把 STmin 字节解码为毫秒。

    * ``0x00``-``0x7F``：0-127 毫秒
    * ``0xF1``-``0xF9``：100-900 微秒
    * 其余取值（含 ``0x80``-``0xF0``、``0xFA``-``0xFF``）为保留值，
      按 ISO 15765-2 应视为 127 ms
    """
    if 0x00 <= value <= 0x7F:
        return float(value)
    if 0xF1 <= value <= 0xF9:
        return (value - 0xF0) * 0.1
    return 127.0


def ms_to_stmin(milliseconds: float) -> int:
    """把毫秒尽量精确地编码为 STmin 字节。"""
    if milliseconds < 0:
        raise ValueError("STmin must be non-negative")
    if milliseconds <= 0:
        return 0x00
    if milliseconds < 1.0:
        micro = int(round(milliseconds * 1000.0))
        micro = max(100, min(900, micro))
        return 0xF0 + max(1, min(9, (micro + 99) // 100))
    if milliseconds < 127.0:
        return int(milliseconds + 0.5)
    return 0x7F


def resolve_stmin(value: int) -> float:
    """把 STmin 字段解析为实际等待毫秒数（保留值按 127 ms 处理）。"""
    return stmin_to_ms(value)


# ---------------------------------------------------------------------------
# PDU 结构
# ---------------------------------------------------------------------------


@dataclass
class IsoTpPdu(object):
    """一个 ISO-TP 协议数据单元。"""

    kind: int
    data: bytes = b""
    #: SF/FF 的报文长度
    length: int = 0
    #: CF 的序号
    sequence: int = 0
    #: FC 的流控状态
    flow_status: int = FS_CTS
    #: FC 的块大小
    block_size: int = 0
    #: FC 的 STmin 原始字节
    stmin: int = 0
    #: 是否存在 32 位扩展长度
    extended_length: bool = False

    @property
    def kind_name(self) -> str:
        return {N_PCI_SF: "SF", N_PCI_FF: "FF",
                N_PCI_CF: "CF", N_PCI_FC: "FC"}.get(self.kind, "?")

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        if self.kind == N_PCI_FC:
            return "<FC %s bs=%d stmin=0x%02X>" % (
                FC_NAMES.get(self.flow_status, "?"), self.block_size, self.stmin
            )
        if self.kind == N_PCI_CF:
            return "<CF sn=%d data=%s>" % (self.sequence, self.data.hex().upper())
        return "<%s len=%d data=%s>" % (self.kind_name, self.length, self.data.hex().upper())


# ---------------------------------------------------------------------------
# 编解码
# ---------------------------------------------------------------------------


def max_payload(is_fd: bool) -> int:
    """返回一帧能承载的最大 ISO-TP 有效载荷字节数（含 PCI）。"""
    return 64 if is_fd else 8


def ff_header_len(total_length: int, is_fd: bool) -> int:
    """首帧 PDU 的头部长度（2 字节普通长度 / 6 字节扩展长度）。"""
    if is_fd and total_length > FF_12BIT_MAX:
        return 6
    return 2


def sf_max_data_length(is_fd: bool) -> int:
    """单帧能承载的最大数据字节数。

    经典 CAN：8 - 1 = 7 字节。
    CAN FD：首字节高 4 位为 0 且低 4 位为 0（转义形式）时用第二个字节表示长度，
    因此最多 64 - 2 = 62 字节。
    """
    return 62 if is_fd else CLASSIC_SF_MAX


def encode_sf(data: bytes, is_fd: bool = False) -> bytes:
    """把数据编码为单帧 PDU。"""
    if not data:
        raise IsoTpError(IsoTpErrorKind.MALFORMED, "single frame cannot be empty")
    limit = sf_max_data_length(is_fd)
    if len(data) > limit:
        raise IsoTpError(
            IsoTpErrorKind.LENGTH_MISMATCH,
            "single frame payload %d exceeds limit %d" % (len(data), limit),
        )
    if is_fd and len(data) > 7:
        return bytes([0x00, len(data)]) + data
    return bytes([len(data) & 0x0F]) + data


def encode_ff(data: bytes, is_fd: bool = False,
              total_length: Optional[int] = None,
              use_extended_length: Optional[bool] = None) -> bytes:
    """把**整条报文**编码为首帧 PDU。

    :param data: 完整报文（本函数自行截取首帧能承载的部分）。
    :param total_length: 报文的真实总长度，必须 >= ``len(data)``。
        缺省用 ``len(data)``。**长度字段写的是报文总长度**，而不是本帧实际
        装载的字节数——写错了接收方会一直等不到收尾。
    :param use_extended_length: 是否强制使用 32 位扩展长度形式。
        缺省在 CAN FD 下为 ``True``、经典 CAN 下为 ``False``。

    为什么 CAN FD 默认走 32 位形式：12 位形式的 PCI 是 ``1H LL``，当长度不是
    4096 的整数倍而首帧被补齐到 8 字节时，紧跟其后的 4 个字节看起来就像一段
    32 位长度，解码端无法区分。用 32 位形式（``10 00`` + 4 字节长度）彻底
    消除歧义，代价是首帧少装 4 个字节。
    """
    total = len(data) if total_length is None else total_length
    if total < len(data):
        raise IsoTpError(
            IsoTpErrorKind.LENGTH_MISMATCH,
            "total_length %d is smaller than the supplied payload %d" % (total, len(data)),
        )
    if use_extended_length is None:
        use_extended_length = is_fd
    use_escape = use_extended_length or total > FF_12BIT_MAX
    if use_escape:
        if not is_fd:
            raise IsoTpError(
                IsoTpErrorKind.LENGTH_MISMATCH,
                "32-bit extended length form requires CAN FD "
                "(total length %d)" % total,
            )
        header_len = 6
        room = max_payload(True) - header_len
        header = bytes([0x10, 0x00]) + total.to_bytes(4, "big")
        return header + data[:room]

    header_len = 2
    room = min(max_payload(is_fd) - header_len, 6)
    header = bytes([0x10 | ((total >> 8) & 0x0F), total & 0xFF])
    chunk = data[:room]
    # 同形判定：解码端在整帧长度 >= 6 字节时会把紧跟首字节的 4 个字节当成
    # 32 位长度解释。若该解释值大于本次的 12 位长度，报文会被误判成扩展长度
    # 形式。这种情况直接报错，而不是发出一个无法唯一解码的首帧。
    if len(chunk) >= 4 and (len(chunk) + header_len) >= 6:
        candidate = int.from_bytes(chunk[0:4], "big")
        if candidate > total:
            raise IsoTpError(
                IsoTpErrorKind.MALFORMED,
                "12-bit first frame for this payload is ambiguous with the 32-bit "
                "extended length form (the first 4 data bytes read as %d > declared "
                "length %d); keep data[0:4] small, or use the 32-bit extended "
                "length form" % (candidate, total),
            )
    return bytes(header) + chunk


def encode_cf(data: bytes, sequence: int, is_fd: bool = False) -> bytes:
    """把数据编码为连续帧 PDU。"""
    seq = sequence & 0x0F
    room = max_payload(is_fd) - 1
    chunk = data[:room]
    return bytes([0x20 | seq]) + chunk


def encode_fc(flow_status: int, block_size: int = 0,
              stmin: int = 0) -> bytes:
    """把流控参数编码为 FC PDU。"""
    if flow_status not in (FS_CTS, FS_WAIT, FS_OVFLW):
        raise IsoTpError(IsoTpErrorKind.MALFORMED,
                         "illegal flow status %r" % (flow_status,))
    if not 0 <= block_size <= 0xFF:
        raise IsoTpError(IsoTpErrorKind.MALFORMED,
                         "block size must be 0-255, got %r" % (block_size,))
    if not 0 <= stmin <= 0xFF:
        raise IsoTpError(IsoTpErrorKind.MALFORMED,
                         "STmin must be 0-255, got %r" % (stmin,))
    return bytes([0x30 | flow_status, block_size, stmin])


def decode_pdu(payload: bytes) -> IsoTpPdu:
    """解析一帧的有效载荷为 PDU。"""
    if len(payload) < 1:
        raise IsoTpError(IsoTpErrorKind.MALFORMED, "empty ISO-TP payload")
    pci = payload[0]
    kind = (pci >> 4) & 0x0F
    low = pci & 0x0F

    if kind == N_PCI_SF:
        if low == 0:
            if len(payload) < 2:
                raise IsoTpError(IsoTpErrorKind.MALFORMED,
                                 "escape single frame needs a length byte")
            length = payload[1]
            data = payload[2:2 + length]
        else:
            length = low
            data = payload[1:1 + length]
        if len(data) != length:
            raise IsoTpError(
                IsoTpErrorKind.LENGTH_MISMATCH,
                "single frame declares %d bytes but carries %d" % (length, len(data)),
            )
        return IsoTpPdu(kind=N_PCI_SF, data=bytes(data), length=length)

    if kind == N_PCI_FF:
        if low == 0:
            # 首字节低 4 位为 0 时有两种形式：32 位扩展长度形式，或 "报文总长度
            # 的高 4 位恰好为 0" 的 12 位长度形式（此时两者 PCI 字节同形）。
            # 用长度自洽性区分：把紧跟的 4 个字节当成 32 位长度解释，
            # 若它比 12 位解释**大**，则只能是扩展长度形式。
            length12 = (low << 8) | payload[1]
            if len(payload) >= 6:
                candidate = int.from_bytes(payload[2:6], "big")
                if candidate > length12:
                    return IsoTpPdu(kind=N_PCI_FF, data=bytes(payload[6:]),
                                    length=candidate, extended_length=True)
            return IsoTpPdu(kind=N_PCI_FF, data=bytes(payload[2:]), length=length12)
        length = (low << 8) | payload[1]
        return IsoTpPdu(kind=N_PCI_FF, data=bytes(payload[2:]), length=length)

    if kind == N_PCI_CF:
        return IsoTpPdu(kind=N_PCI_CF, data=bytes(payload[1:]), sequence=low)

    if kind == N_PCI_FC:
        if len(payload) < 3:
            raise IsoTpError(IsoTpErrorKind.MALFORMED,
                             "flow control frame needs 3 bytes")
        return IsoTpPdu(kind=N_PCI_FC, flow_status=low,
                        block_size=payload[1], stmin=payload[2])

    raise IsoTpError(IsoTpErrorKind.MALFORMED, "unknown PCI type 0x%X" % kind)


# ---------------------------------------------------------------------------
# 发送 / 接收状态
# ---------------------------------------------------------------------------


@dataclass
class IsoTpConfig(object):
    """ISO-TP 连接定时与流控参数。"""

    n_as_ms: float = DEFAULT_N_AS_MS
    n_ar_ms: float = DEFAULT_N_AR_MS
    n_bs_ms: float = DEFAULT_N_BS_MS
    n_br_ms: float = DEFAULT_N_BR_MS
    n_cs_ms: float = DEFAULT_N_CS_MS
    n_cr_ms: float = DEFAULT_N_CR_MS
    #: 发送方希望接收方使用的块大小
    block_size: int = 0
    #: 发送方希望接收方使用的最小间隔
    stmin_ms: float = 0.0
    is_fd: bool = False


@dataclass
class IsoTpTransfer(object):
    """一次多帧传输的状态。"""

    payload: bytes
    total_length: int
    sent_length: int = 0
    sequence: int = 1        # 首帧之后第一个连续帧的 SN 是 1
    block_counter: int = 0
    #: 等待中的超时名称（空表示不在等待）
    waiting_for: str = ""
    #: 超时截止的虚拟时刻（毫秒）
    deadline_ms: Optional[float] = None
    finished: bool = False
    #: 对端在流控帧里给出的块大小与 STmin
    block_size_peer: int = 0
    stmin: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.total_length - self.sent_length)


@dataclass
class IsoTpReception(object):
    """一次多帧接收的状态。"""

    declared_length: int = 0
    buffer: bytearray = field(default_factory=bytearray)
    expected_sequence: int = 1
    block_counter: int = 0
    waiting_for: str = ""
    deadline_ms: Optional[float] = None
    finished: bool = False
    #: 是否正在等待首帧之后的流控
    started: bool = False


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


class IsoTpConnection(object):
    """一条 ISO-TP 连接（抽象掉底层物理通道，由调用方注入收发与调度）。

    :param send_frame: ``send_frame(payload: bytes) -> None``，
        把一段 ISO-TP 有效载荷装载到 CAN 帧并送上总线。
    :param config: 定时与流控参数。
    :param clock: 返回当前虚拟毫秒的回调；缺省用内部计数器。
    """

    def __init__(
        self,
        send_frame: Callable[[bytes], None],
        config: Optional[IsoTpConfig] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.send_frame = send_frame
        self.config = config or IsoTpConfig()
        self._clock = clock or (lambda: 0.0)
        #: 已完成的接收报文
        self.received: List[bytes] = []
        #: 最近一次接收的完整报文
        self.last_received: Optional[bytes] = None
        #: 传输状态
        self.tx: Optional[IsoTpTransfer] = None
        self.rx = IsoTpReception()
        #: 收到流控帧时的回调
        self.on_flow_control: Optional[Callable[[IsoTpPdu], None]] = None
        #: 事件日志（便于测试断言）
        self.events: List[str] = []
        #: 发送计数器（用于 STmin 生效验证）
        self.frames_sent: int = 0
        #: 每个已发送连续帧的时刻
        self.frame_times: List[float] = []
        #: 定时参数统计：本次传输的 STmin 实际等待总量
        self.stmin_wait_ms: float = 0.0
        #: 错误记录
        self.errors: List[IsoTpError] = []

    # -- 工具 -------------------------------------------------------------

    @property
    def now_ms(self) -> float:
        return self._clock()

    def _emit(self, payload: bytes) -> None:
        self.frames_sent += 1
        self.frame_times.append(self.now_ms)
        self.send_frame(payload)

    # -- 发送 -------------------------------------------------------------

    def send(self, data: bytes) -> IsoTpTransfer:
        """发起一次 ISO-TP 发送。

        * 长度不超过单帧上限时直接发 SF，传输立即完成。
        * 否则发 FF，然后等待 FC；调用方需要用 :meth:`on_pdu` 送回流控帧。
        """
        if self.tx is not None and not self.tx.finished:
            raise IsoTpError(IsoTpErrorKind.UNEXPECTED_PDU,
                             "a transmission is already in progress")
        sf_limit = sf_max_data_length(self.config.is_fd)
        if len(data) <= sf_limit:
            self._emit(encode_sf(data, self.config.is_fd))
            transfer = IsoTpTransfer(payload=data, total_length=len(data),
                                     sent_length=len(data), finished=True)
            self.tx = transfer
            self.events.append("SF len=%d" % len(data))
            return transfer
        if self.config.is_fd and len(data) <= 60 and len(data) <= sf_limit:
            # 理论上不会到这里（sf_limit 已经是 62），保留分支只为文档完整
            pass

        first = encode_ff(data, self.config.is_fd)
        self._emit(first)
        transfer = IsoTpTransfer(
            payload=data,
            total_length=len(data),
            sent_length=6,
            sequence=1,
        )
        transfer.waiting_for = "N_Bs"
        transfer.deadline_ms = self.now_ms + self.config.n_bs_ms
        self.tx = transfer
        self.events.append("FF len=%d" % len(data))
        return transfer

    def send_flow_control(self, flow_status: int = FS_CTS,
                          block_size: Optional[int] = None,
                          stmin: Optional[int] = None) -> None:
        """发送流控帧（接收方角色）。"""
        bs = self.config.block_size if block_size is None else block_size
        st = ms_to_stmin(self.config.stmin_ms) if stmin is None else stmin
        self._emit(encode_fc(flow_status, bs, st))
        self.events.append("FC %s bs=%d stmin=0x%02X"
                           % (FC_NAMES.get(flow_status, "?"), bs, st))

    # -- 接收 / 事件入口 ---------------------------------------------------

    def on_pdu(self, payload: bytes) -> Optional[bytes]:
        """把来自总线的 ISO-TP 有效载荷送进状态机。

        :return: 若本次调用凑齐了一整条报文，返回该报文；否则返回 ``None``。
        :raises IsoTpError: 协议错误（序号错、溢出、长度不符等）。
        """
        pdu = decode_pdu(payload)
        self.events.append("RX %s" % pdu.kind_name)
        if pdu.kind == N_PCI_FC:
            return self._handle_flow_control(pdu)
        if pdu.kind == N_PCI_SF:
            return self._handle_single_frame(pdu)
        if pdu.kind == N_PCI_FF:
            return self._handle_first_frame(pdu)
        if pdu.kind == N_PCI_CF:
            return self._handle_consecutive_frame(pdu)
        raise IsoTpError(IsoTpErrorKind.MALFORMED, "unhandled PDU type")

    def _fail(self, error: IsoTpError) -> None:
        self.errors.append(error)
        self.events.append("ERROR %s: %s" % (error.kind, error))
        if self.tx is not None and not self.tx.finished:
            self.tx.waiting_for = ""
            self.tx.deadline_ms = None
            self.tx.finished = True
        raise error

    def _handle_single_frame(self, pdu: IsoTpPdu) -> bytes:
        if self.rx.started and not self.rx.finished:
            self._fail(IsoTpError(IsoTpErrorKind.UNEXPECTED_PDU,
                                  "SF received in the middle of a multi-frame reception"))
        self.rx = IsoTpReception(declared_length=pdu.length,
                                 buffer=bytearray(pdu.data),
                                 finished=True,
                                 started=True)
        data = bytes(self.rx.buffer)
        self.received.append(data)
        self.last_received = data
        self.events.append("received SF len=%d" % len(data))
        return data

    def _handle_first_frame(self, pdu: IsoTpPdu) -> None:
        if self.rx.started and not self.rx.finished:
            self._fail(IsoTpError(IsoTpErrorKind.UNEXPECTED_PDU,
                                  "FF received while a reception is in progress"))
        self.rx = IsoTpReception(
            declared_length=pdu.length,
            buffer=bytearray(pdu.data),
            expected_sequence=1,
            started=True,
        )
        self.rx.waiting_for = "N_Cr"
        self.rx.deadline_ms = self.now_ms + self.config.n_cr_ms
        self.events.append("started FF reception len=%d" % pdu.length)
        # 接收方回 FC
        self.send_flow_control(FS_CTS)
        self.rx.waiting_for = "N_Cr"
        self.rx.deadline_ms = self.now_ms + self.config.n_cr_ms
        return None

    def _handle_consecutive_frame(self, pdu: IsoTpPdu) -> Optional[bytes]:
        if not self.rx.started or self.rx.finished:
            self._fail(IsoTpError(IsoTpErrorKind.UNEXPECTED_PDU,
                                  "CF received without an active reception"))
        if pdu.sequence != (self.rx.expected_sequence & 0x0F):
            self._fail(IsoTpError(
                IsoTpErrorKind.WRONG_SN,
                "expected SN %d, got %d" % (self.rx.expected_sequence & 0x0F, pdu.sequence),
            ))
        self.rx.buffer.extend(pdu.data)
        self.rx.expected_sequence = (self.rx.expected_sequence + 1) & 0x0F
        self.rx.block_counter += 1
        if len(self.rx.buffer) >= self.rx.declared_length:
            data = bytes(self.rx.buffer[:self.rx.declared_length])
            self.rx.finished = True
            self.rx.waiting_for = ""
            self.rx.deadline_ms = None
            self.received.append(data)
            self.last_received = data
            self.events.append("received multi-frame len=%d" % len(data))
            return data
        if len(self.rx.buffer) > self.rx.declared_length:
            self._fail(IsoTpError(IsoTpErrorKind.LENGTH_MISMATCH,
                                  "received more bytes than declared"))
        # 块边界：需要再发一次 FC
        if self.config.block_size and self.rx.block_counter >= self.config.block_size:
            self.rx.block_counter = 0
            self.send_flow_control(FS_CTS)
        self.rx.waiting_for = "N_Cr"
        self.rx.deadline_ms = self.now_ms + self.config.n_cr_ms
        return None

    def _handle_flow_control(self, pdu: IsoTpPdu) -> None:
        if self.tx is None or self.tx.finished:
            self._fail(IsoTpError(IsoTpErrorKind.UNEXPECTED_PDU,
                                  "flow control received without an active transmission"))
        if pdu.flow_status == FS_OVFLW:
            self._fail(IsoTpError(IsoTpErrorKind.OVERFLOW,
                                  "receiver reported overflow (FS=OVFLW)"))
        if self.on_flow_control is not None:
            self.on_flow_control(pdu)
        if pdu.flow_status == FS_WAIT:
            self.tx.waiting_for = "N_Bs"
            self.tx.deadline_ms = self.now_ms + self.config.n_bs_ms
            self.events.append("FC WAIT, still waiting for CTS")
            return
        self.tx.block_size_peer = pdu.block_size
        self.tx.stmin = pdu.stmin
        self.tx.waiting_for = ""
        self.tx.deadline_ms = None

    # -- 发送推进（由调度器在 STmin 到点后调用） ---------------------------

    def send_next_consecutive(self) -> bool:
        """发送一个连续帧。

        :return: 是否还有后续数据（``False`` 表示本次传输已结束）。
        """
        if self.tx is None or self.tx.finished:
            return False
        transfer = self.tx
        block_size = transfer.block_size_peer
        offset = transfer.sent_length
        chunk = transfer.payload[offset:offset + (max_payload(self.config.is_fd) - 1)]
        if not chunk:
            transfer.finished = True
            transfer.waiting_for = ""
            transfer.deadline_ms = None
            return False
        self._emit(encode_cf(chunk, transfer.sequence, self.config.is_fd))
        transfer.sent_length += len(chunk)
        transfer.sequence = (transfer.sequence + 1) & 0x0F
        transfer.block_counter += 1
        self.events.append("CF sn=%d len=%d" % ((transfer.sequence - 1) & 0x0F, len(chunk)))
        if transfer.sent_length >= transfer.total_length:
            transfer.finished = True
            transfer.waiting_for = ""
            transfer.deadline_ms = None
            return False
        if block_size and transfer.block_counter >= block_size:
            # 一个块刚好发完：立刻进入等待下一个流控帧的状态
            transfer.block_counter = 0
            transfer.waiting_for = "N_Bs"
            transfer.deadline_ms = self.now_ms + self.config.n_bs_ms
            self.events.append("block complete, waiting for next FC")
            return True
        transfer.waiting_for = "N_Cs"
        transfer.deadline_ms = self.now_ms + self.config.n_cs_ms
        return True

    # -- 超时 -------------------------------------------------------------

    def timeout_names(self) -> List[str]:
        """返回当前所有挂起的超时参数名。"""
        names: List[str] = []
        if self.tx is not None and not self.tx.finished and self.tx.waiting_for:
            names.append(self.tx.waiting_for)
        if self.rx.started and not self.rx.finished and self.rx.waiting_for:
            names.append(self.rx.waiting_for)
        return names

    def next_deadline_ms(self) -> Optional[float]:
        """返回最近的超时截止时刻（毫秒）。"""
        deadlines = []
        if self.tx is not None and not self.tx.finished and self.tx.deadline_ms is not None:
            deadlines.append(self.tx.deadline_ms)
        if self.rx.started and not self.rx.finished and self.rx.deadline_ms is not None:
            deadlines.append(self.rx.deadline_ms)
        return min(deadlines) if deadlines else None

    def check_timeouts(self, now_ms: Optional[float] = None) -> List[str]:
        """检查并触发超时（把所有已过期的超时报告出来并中止传输）。

        :return: 触发的超时参数名列表。
        """
        now = self.now_ms if now_ms is None else now_ms
        fired: List[str] = []
        if (self.tx is not None and not self.tx.finished
                and self.tx.deadline_ms is not None and now >= self.tx.deadline_ms):
            name = self.tx.waiting_for
            self.tx.waiting_for = ""
            self.tx.deadline_ms = None
            self.tx.finished = True
            fired.append(name)
            err = IsoTpError(IsoTpErrorKind.TIMEOUT,
                             "%s expired while sending" % name, timeout_name=name)
            self.errors.append(err)
            self.events.append("ERROR %s" % err)
        if (self.rx.started and not self.rx.finished
                and self.rx.deadline_ms is not None and now >= self.rx.deadline_ms):
            name = self.rx.waiting_for
            self.rx.waiting_for = ""
            self.rx.deadline_ms = None
            self.rx.finished = True
            fired.append(name)
            err = IsoTpError(IsoTpErrorKind.TIMEOUT,
                             "%s expired while receiving" % name, timeout_name=name)
            self.errors.append(err)
            self.events.append("ERROR %s" % err)
        return fired

    def abort(self, reason: str = "") -> None:
        """中止当前传输与接收。"""
        if self.tx is not None and not self.tx.finished:
            self.tx.finished = True
            self.tx.waiting_for = ""
            self.tx.deadline_ms = None
        if self.rx.started and not self.rx.finished:
            self.rx.finished = True
            self.rx.waiting_for = ""
            self.rx.deadline_ms = None
        err = IsoTpError(IsoTpErrorKind.ABORTED, reason or "aborted")
        self.errors.append(err)
        self.events.append("ABORT %s" % reason)


def split_message(data: bytes, is_fd: bool = False) -> List[bytes]:
    """把一条报文拆成 ISO-TP PDU 序列（便于测试与报文长度核对）。

    :return: ``[SF]`` 或 ``[FF, CF, CF, ...]``；PDU 中不含 CAN 帧的填充字节。
    """
    if len(data) <= sf_max_data_length(is_fd):
        return [encode_sf(data, is_fd)]
    room_cf = max_payload(is_fd) - 1
    pdus = [encode_ff(data, is_fd)]
    offset = 6
    sequence = 1
    while offset < len(data):
        chunk = data[offset:offset + room_cf]
        pdus.append(encode_cf(chunk, sequence, is_fd))
        offset += len(chunk)
        sequence = (sequence + 1) & 0x0F
    return pdus

def frame_for(payload: bytes, can_id: int, is_fd: bool = False) -> Frame:
    """把 ISO-TP 有效载荷装进一帧 CAN 帧（长度不足时补 0，符合真实控制器行为）。"""
    data = bytearray(payload)
    if is_fd:
        # CAN FD 帧长度必须落在合法 DLC 表上
        for candidate in (8, 12, 16, 20, 24, 32, 48, 64):
            if len(data) <= candidate:
                data.extend(b"\x00" * (candidate - len(data)))
                break
    else:
        data.extend(b"\x00" * (8 - len(data)))
    return Frame(identifier=can_id, data=bytes(data), fd=is_fd, brs=is_fd)
