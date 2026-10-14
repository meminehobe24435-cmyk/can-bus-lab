# -*- coding: utf-8 -*-
"""CAN / CAN FD 帧层：位级编码与解码。

本模块按公开标准自行实现经典 CAN（ISO 11898-1:2015 中的 Classical CAN）与
CAN FD 的帧结构，**逐位**生成和解析总线位流，不做任何物理层建模。

位流约定（与标准一致）::

    显性位 (dominant)  = 0
    隐性位 (recessive) = 1

经典 CAN 数据帧 / 远程帧的位流结构::

    SOF(1) | 仲裁场 | 控制场 | 数据场 | CRC 场 | ACK 场 | EOF(7) | IFS(3)

    * 标准帧仲裁场: 11 位 ID + RTR
    * 扩展帧仲裁场: 11 位基本 ID + SRR + IDE + 18 位扩展 ID + RTR
    * 控制场(经典): IDE + r0 + DLC(4)
    * 控制场(CAN FD): IDE + FDF + res + BRS + ESI + DLC(4)
    * CRC 场: CRC 序列(15/17/21 位) + CRC 界定符(1 位隐性)
    * ACK 场: ACK 槽(1) + ACK 界定符(1)
    * EOF: 7 位隐性；IFS: 3 位隐性

位填充只作用于 "SOF ... CRC 序列" 这一段；CRC 界定符、ACK 场、EOF、IFS 以及
错误帧 / 过载帧中的固定形式位不做填充。

**已知简化**：本实现把 CAN FD 的仲裁段同样按经典 CAN 的位填充规则处理，
不建模 CAN FD 在 BRS 位之后切换到更高的数据段波特率、也不建模
FDF/BRS 附近位填充规则的差异。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 显性位电平值（逻辑 0）
DOMINANT = 0
#: 隐性位电平值（逻辑 1）
RECESSIVE = 1

#: 标准帧（CAN 2.0A）标识符位数
STD_ID_BITS = 11
#: 扩展帧（CAN 2.0B）标识符位数
EXT_ID_BITS = 29
#: 标准帧标识符上限
STD_ID_MAX = 0x7FF
#: 扩展帧标识符上限
EXT_ID_MAX = 0x1FFFFFFF

#: 位填充阈值：连续多少个同极性位之后插入一个反极性位
STUFF_THRESHOLD = 5

#: 经典 CAN 的 CRC15 生成多项式（含隐含的 x^15 项），0x4599
CRC15_POLY = 0x4599
#: CAN FD 数据场 <= 16 字节时使用的 CRC17 生成多项式
CRC17_POLY = 0x1685B
#: CAN FD 数据场 > 16 字节时使用的 CRC21 生成多项式
CRC21_POLY = 0x102899

#: 4 位 DLC 到实际数据字节长度的映射（经典 CAN 与 CAN FD 共用 0-8）
DLC_TO_LENGTH: Dict[int, int] = {
    0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8,
    9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64,
}

#: 经典 CAN 允许的最大数据字节数
CLASSIC_MAX_LENGTH = 8
#: CAN FD 允许的最大数据字节数
FD_MAX_LENGTH = 64
#: 合法数据长度集合（CAN FD DLC 编码可表示的全部长度）
LEGAL_LENGTHS = frozenset(DLC_TO_LENGTH.values())


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class FrameError(ValueError):
    """帧结构非法（未知字段、长度不匹配等）。"""


class BitStreamError(ValueError):
    """位流非法（长度不足、填充错误、CRC 校验失败等）。"""


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------


def crc_polynomial(bits: Sequence[int], poly: int, crc_bits: int) -> int:
    """通用 CRC 计算（MSB-first、移位寄存器初值 0、不做输出反转）。

    实现方式与标准中描述的软件参考实现一致：把待校验位流后面补 ``crc_bits``
    个 0，对移位寄存器做模 2 除法，最后寄存器的值即为 CRC。

    :param bits: 待校验的位序列，元素为 0/1。
    :param poly: 生成多项式（已去掉最高位，例如 CRC15 为 0x4599）。
    :param crc_bits: CRC 位数（15 / 17 / 21）。
    :return: ``crc_bits`` 位整数形式的 CRC。
    """
    if crc_bits <= 0:
        raise ValueError("crc_bits must be positive")
    mask = (1 << crc_bits) - 1
    reg = 0
    for bit in list(bits) + [0] * crc_bits:
        if bit not in (0, 1):
            raise ValueError("bit stream must contain only 0/1")
        # 取寄存器最高位与当前输入位的异或作为反馈
        fb = ((reg >> (crc_bits - 1)) & 1) ^ bit
        reg = (reg << 1) & mask
        if fb:
            reg ^= poly & mask
    return reg & mask


def crc15(bits: Sequence[int]) -> int:
    """经典 CAN 数据帧 / 远程帧的 CRC15 计算。"""
    return crc_polynomial(bits, CRC15_POLY, 15)


def crc17(bits: Sequence[int]) -> int:
    """CAN FD 数据场 <= 16 字节时使用的 CRC17。"""
    return crc_polynomial(bits, CRC17_POLY, 17)


def crc21(bits: Sequence[int]) -> int:
    """CAN FD 数据场 > 16 字节时使用的 CRC21。"""
    return crc_polynomial(bits, CRC21_POLY, 21)


def crc_select(data_length: int, is_fd: bool) -> Tuple[int, str]:
    """根据帧类型与数据长度选择 CRC。

    :return: ``(crc_bits, name)``，例如 ``(15, "CRC15")``。
    """
    if not is_fd:
        return 15, "CRC15"
    if data_length <= 16:
        return 17, "CRC17"
    return 21, "CRC21"


def crc_compute(bits: Sequence[int], crc_bits: int) -> int:
    """按位数分派到对应的 CRC 实现。"""
    if crc_bits == 15:
        return crc15(bits)
    if crc_bits == 17:
        return crc17(bits)
    if crc_bits == 21:
        return crc21(bits)
    raise ValueError("unsupported CRC width: %d" % crc_bits)


def int_to_bits(value: int, width: int) -> List[int]:
    """把整数按 MSB-first 展开为固定宽度的位序列。"""
    if width <= 0:
        raise ValueError("width must be positive")
    if value < 0 or value >= (1 << width):
        raise ValueError("value 0x%X does not fit in %d bits" % (value, width))
    return [(value >> shift) & 1 for shift in range(width - 1, -1, -1)]


def bits_to_int(bits: Sequence[int]) -> int:
    """把 MSB-first 的位序列折叠成整数。"""
    value = 0
    for bit in bits:
        if bit not in (0, 1):
            raise ValueError("bit stream must contain only 0/1")
        value = (value << 1) | bit
    return value


# ---------------------------------------------------------------------------
# DLC 映射
# ---------------------------------------------------------------------------


def dlc_to_length(dlc: int) -> int:
    """把 4 位 DLC 编码展开为实际数据字节长度（经典 CAN 与 CAN FD 通用表）。"""
    if dlc not in DLC_TO_LENGTH:
        raise FrameError("illegal DLC value: %r" % (dlc,))
    return DLC_TO_LENGTH[dlc]


def length_to_dlc(length: int) -> int:
    """把实际数据字节长度编码为 DLC。

    只接受 DLC 表能够精确表示的长度；9/10/11 等长度在 CAN FD 中没有对应
    的 DLC 编码，必须报错而不是静默取整（真实控制器同样不会产生这种帧）。
    """
    for dlc, mapped in DLC_TO_LENGTH.items():
        if mapped == length:
            return dlc
    raise FrameError("no DLC encoding for data length %r" % (length,))


def max_dlc(is_fd: bool) -> int:
    """返回该帧格式下允许的最大 DLC。"""
    return 15 if is_fd else 8


def max_length(is_fd: bool) -> int:
    """返回该帧格式下允许的最大数据字节数。"""
    return FD_MAX_LENGTH if is_fd else CLASSIC_MAX_LENGTH


# ---------------------------------------------------------------------------
# 帧类型
# ---------------------------------------------------------------------------


class FrameKind(object):
    """帧类型枚举（用字符串常量而不是 Enum，便于日志与脚本层直接比较）。"""

    DATA = "data"
    REMOTE = "remote"
    ERROR = "error"
    OVERLOAD = "overload"


# ---------------------------------------------------------------------------
# 位填充
# ---------------------------------------------------------------------------


def stuff_bits(bits: Sequence[int], threshold: int = STUFF_THRESHOLD) -> List[int]:
    """对位序列做位填充：连续 ``threshold`` 个同极性位后插入 1 个反极性位。"""
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    out: List[int] = []
    run = 0
    last: Optional[int] = None
    for bit in bits:
        if bit not in (0, 1):
            raise ValueError("bit stream must contain only 0/1")
        out.append(bit)
        if bit == last:
            run += 1
        else:
            run = 1
            last = bit
        if run == threshold:
            out.append(1 - bit)
            run = 0
            last = None
    return out


def unstuff_bits(bits: Sequence[int], threshold: int = STUFF_THRESHOLD) -> List[int]:
    """位填充的逆运算：检测并剔除插入的填充位。

    :raises BitStreamError: 出现超过阈值个连续同极性位（即填充错误）或位流
        在填充位位置被截断。
    """
    return _unstuff(bits, threshold, allow_trailing_truncation=False)[0]


def _unstuff(
    bits: Sequence[int],
    threshold: int = STUFF_THRESHOLD,
    allow_trailing_truncation: bool = False,
) -> Tuple[List[int], bool]:
    """位填充逆运算的内部实现。

    :param allow_trailing_truncation: 为 ``True`` 时，允许位流恰好以
        ``threshold`` 个同极性位结束而不需要后续填充位。这一情形出现在
        **只取** "SOF..数据场" 这一段（丢弃其后的填充位与 CRC）来做 CRC
        重算的时候：编码器可能恰好把填充位放在数据场之后。
    :return: ``(去填充后的位序列, 是否在尾部被截断)``。
    """
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    out: List[int] = []
    run = 0
    last: Optional[int] = None
    index = 0
    total = len(bits)
    while index < total:
        bit = bits[index]
        if bit not in (0, 1):
            raise ValueError("bit stream must contain only 0/1")
        out.append(bit)
        index += 1
        if bit == last:
            run += 1
        else:
            run = 1
            last = bit
        if run == threshold:
            if index >= total:
                if allow_trailing_truncation:
                    return out, True
                raise BitStreamError("bit stream ends where a stuff bit was expected")
            stuff = bits[index]
            if stuff == bit:
                if allow_trailing_truncation:
                    # 宽松模式：把这一位当作 "填充区之后的内容"，回退到它之前
                    out.pop()
                    return out, True
                raise BitStreamError(
                    "stuff error: expected complement of %d at bit index %d" % (bit, index)
                )
            index += 1
            run = 0
            last = None
    return out, False


def find_stuff_error(bits: Sequence[int], threshold: int = STUFF_THRESHOLD) -> Optional[int]:
    """返回位流中第一个填充错误的下标；没有错误则返回 ``None``。"""
    run = 0
    last: Optional[int] = None
    for index, bit in enumerate(bits):
        if bit == last:
            run += 1
        else:
            run = 1
            last = bit
        if run > threshold:
            return index
    return None


def _count_stuff_bits(bits: Sequence[int], threshold: int = STUFF_THRESHOLD) -> int:
    """统计一个**无填充**位序列在施加位填充后会插入多少个填充位。"""
    count = 0
    run = 0
    last: Optional[int] = None
    for bit in bits:
        if bit == last:
            run += 1
        else:
            run = 1
            last = bit
        if run == threshold:
            count += 1
            run = 0
            last = None
    return count


def _scan_stuffed_region(bits: List[int], expected_raw: int) -> Tuple[List[int], int]:
    """定位 "SOF..CRC 序列" 填充区，返回 ``(原始位序列, 填充位个数)``。

    关键不变量：对一段**合法**的位流而言，只有把窗口切到 "填充区的最后一个位"
    上时，严格去填充才会恰好成功并还原出 ``expected_raw`` 个原始位。

    * 窗口切在填充区中间：去填充要么抛填充错误，要么还原出的位数不等于
      ``expected_raw``（切在填充位之前会少还原出填充位后面那一位）。
    * 窗口切到填充位之后：多出来的位要么违反填充规则（紧接着的 CRC 界定符与
      填充位极性相同，会被判成填充错误），要么使还原长度超过 ``expected_raw``。

    因此从长到短枚举窗口并返回第一个命中点即可，不存在歧义。
    """
    limit = min(len(bits), expected_raw + expected_raw // 4 + 16)
    for window_len in range(limit, expected_raw - 1, -1):
        try:
            raw = unstuff_bits(bits[0:window_len])
        except BitStreamError:
            continue
        if len(raw) == expected_raw and _count_stuff_bits(raw) == window_len - expected_raw:
            return raw, window_len - expected_raw
    raise BitStreamError(
        "bit stream too short or malformed: no stuffed region of %d raw bits found "
        "in a %d-bit stream" % (expected_raw, len(bits))
    )


def unstuff_prefix(
    bits: Sequence[int],
    threshold: int = STUFF_THRESHOLD,
) -> Tuple[List[int], int]:
    """贪婪地去填充，返回 ``(原始位序列, 消费掉的已填充位个数)``。

    .. warning::

       这个函数**不能**用来定位填充区边界。填充区末尾可能正好是 5 个同极性位，
       此时紧随其后的填充位与 CRC 界定符极性相同（都是隐性），无法从位值上区分；
       而帧尾的 "CRC 界定符 + ACK 界定符 + EOF + IFS" 又是一长串连续隐性位，
       会被误当成更多填充位。位级解码请使用 :func:`decode`，它用
       "去填充结果长度必须等于帧结构推算出的原始长度" 这一不变量来定位边界。

    保留此函数是因为它在**测试**里便于表达 "能还原出的最长前缀" 这一概念。
    """
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    out: List[int] = []
    run = 0
    last: Optional[int] = None
    index = 0
    total = len(bits)
    while index < total:
        bit = bits[index]
        if bit not in (0, 1):
            raise ValueError("bit stream must contain only 0/1")
        out.append(bit)
        index += 1
        if bit == last:
            run += 1
        else:
            run = 1
            last = bit
        if run == threshold:
            if index >= total:
                break
            if bits[index] == bit:
                # 该插入填充位的位置上没有反极性位：这里就是填充区的结束
                out.pop()
                index -= 1
                break
            index += 1
            run = 0
            last = None
    return out, index


# ---------------------------------------------------------------------------
# 帧对象
# ---------------------------------------------------------------------------


@dataclass
class Frame(object):
    """一条 CAN / CAN FD 帧的逻辑表示。

    :param identifier: 11 位或 29 位标识符。
    :param data: 数据字节（远程帧为空）。
    :param extended: ``True`` 表示扩展帧（29 位 ID）。
    :param remote: ``True`` 表示远程帧（RTR 显性）。
    :param fd: ``True`` 表示 CAN FD 帧。
    :param brs: CAN FD 位速率切换标志。
    :param esi: CAN FD 错误状态指示（``True`` = 发送方 error-passive）。
    :param timestamp: 发送时刻（虚拟毫秒），由总线填充。
    :param source: 发送节点名，由总线填充。
    """

    identifier: int = 0
    data: bytes = b""
    extended: bool = False
    remote: bool = False
    fd: bool = False
    brs: bool = False
    esi: bool = False
    timestamp: float = 0.0
    source: str = ""

    # -- 基本属性 ---------------------------------------------------------

    @property
    def id_max(self) -> int:
        """该帧标识符位宽允许的最大值。"""
        return EXT_ID_MAX if self.extended else STD_ID_MAX

    @property
    def length(self) -> int:
        """数据字节长度。"""
        return len(self.data)

    @property
    def dlc(self) -> int:
        """把数据长度编码成 DLC。"""
        if self.remote:
            # 远程帧用 DLC 表达 "请求多少字节"，数据场为空
            return length_to_dlc(self._remote_dlc_length)
        return length_to_dlc(len(self.data))

    @property
    def _remote_dlc_length(self) -> int:
        return getattr(self, "_remote_length", 0)

    @property
    def priority_key(self) -> Tuple[int, int, int]:
        """仲裁优先级排序键，越小越优先。

        规则（ISO 11898-1 仲裁场逐位比较的等价形式）：

        1. 标识符数值小者优先；
        2. 标识符相同时 **数据帧优先于远程帧**（RTR 位显性胜出）；
        3. 基本 ID 相同时 **标准帧优先于扩展帧**（IDE 位显性胜出）。
        """
        return (
            self.identifier,
            1 if self.remote else 0,
            1 if self.extended else 0,
        )

    # -- 构造辅助 ---------------------------------------------------------

    @staticmethod
    def remote_frame(identifier: int, dlc: int, extended: bool = False) -> "Frame":
        """构造一条远程帧，``dlc`` 表示请求的数据字节数。"""
        length = dlc_to_length(dlc)
        frame = Frame(identifier=identifier, data=b"", extended=extended, remote=True)
        frame._remote_length = length
        return frame

    @property
    def requested_length(self) -> int:
        """远程帧请求的数据长度；数据帧返回自身长度。"""
        return self._remote_dlc_length if self.remote else len(self.data)

    # -- 校验 -------------------------------------------------------------

    def validate(self) -> None:
        """校验帧结构合法性。

        :raises FrameError: 标识符越界、数据长度非法、CAN FD 组合非法等。
        """
        if self.identifier < 0 or self.identifier > self.id_max:
            raise FrameError(
                "identifier 0x%X out of range for %s frame (max 0x%X)"
                % (self.identifier, "extended" if self.extended else "standard", self.id_max)
            )
        if self.extended and self.identifier <= STD_ID_MAX:
            # 合法但通常意味着调用方忘了设置 extended，这里只做提示性约束不做拒绝
            pass
        if self.remote:
            if self.data:
                raise FrameError("remote frame must not carry a data field")
            dlc = length_to_dlc(self._remote_dlc_length)
            if dlc > max_dlc(self.fd):
                raise FrameError("remote frame DLC %d exceeds limit" % dlc)
            if self.fd:
                raise FrameError("CAN FD does not define remote frames")
            return
        length = len(self.data)
        if self.fd and length not in LEGAL_LENGTHS:
            raise FrameError("invalid CAN FD data length %d" % length)
        if not self.fd and length > CLASSIC_MAX_LENGTH:
            raise FrameError("classic CAN data length %d exceeds 8" % length)
        if self.brs and not self.fd:
            raise FrameError("BRS requires CAN FD")
        if self.esi and not self.fd:
            raise FrameError("ESI requires CAN FD")

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        kind = "RTR" if self.remote else "DAT"
        proto = "FD" if self.fd else "CC"
        return "<Frame %s %s id=0x%X dlc=%d data=%s>" % (
            proto, kind, self.identifier, self.dlc, self.data.hex().upper()
        )


# ---------------------------------------------------------------------------
# 位级编码
# ---------------------------------------------------------------------------


def _arbitration_phase(frame: Frame) -> List[int]:
    """构造仲裁场位序列（不含 SOF）。

    * 标准帧：``ID(11) + RTR``，共 12 位。
    * 扩展帧：``基本 ID(11) + SRR(1,隐性) + IDE(1,显性) + 扩展 ID(18) + RTR``，共 32 位。

    注意：**IDE 位本身属于仲裁场**（它在 RTR 之前、决定后续仲裁场长度），
    因此标准帧的仲裁场里没有显式写出的 IDE 位——标准帧的 IDE 就是那个
    被标准帧用来放 RTR 之前的、恒为显性的位。这一位在标准帧里紧跟在
    11 位 ID 之后，见下：标准帧第 12 位即 IDE（显性），第 13 位是 RTR。
    """
    bits: List[int] = []
    if frame.extended:
        base = (frame.identifier >> 18) & STD_ID_MAX
        ext = frame.identifier & 0x3FFFF
        bits += int_to_bits(base, 11)
        bits.append(RECESSIVE)   # SRR：替代远程请求，固定隐性
        bits.append(DOMINANT)    # IDE：显性 = 扩展帧
        bits += int_to_bits(ext, 18)
    else:
        bits += int_to_bits(frame.identifier, 11)
        bits.append(DOMINANT)    # IDE：显性 = 标准帧
    bits.append(RECESSIVE if frame.remote else DOMINANT)  # RTR
    return bits


def _control_phase(frame: Frame, valid_length: int) -> List[int]:
    """构造控制场位序列（IDE 位已在仲裁场中发出，这里不再重复）。

    * 经典 CAN：``FDF(1,显性) + r0(1,显性) + DLC(4)``
    * CAN FD：``FDF(1,隐性) + res(1,显性) + BRS(1) + ESI(1) + DLC(4)``
    """
    bits: List[int] = []
    if frame.fd:
        bits.append(RECESSIVE)                              # FDF：隐性 = FD
        bits.append(DOMINANT)                               # res / r0
        bits.append(RECESSIVE if frame.brs else DOMINANT)   # BRS
        bits.append(RECESSIVE if frame.esi else DOMINANT)   # ESI
    else:
        bits.append(DOMINANT)   # FDF：显性 = 经典 CAN
        bits.append(DOMINANT)   # r0
    bits += int_to_bits(length_to_dlc(valid_length), 4)
    return bits


def _crc_region(frame: Frame) -> Tuple[List[int], List[int]]:
    """返回 ``(需要施加位填充的位序列, 其后的非填充尾部)``。

    按标准，位填充作用于 ``SOF ... CRC 序列`` 这一段。因此这里把 "SOF + 仲裁场 +
    控制场 + 数据场 + CRC 序列" 整体作为填充区，而 CRC 界定符、ACK 场、EOF、IFS
    属于非填充尾部。

    填充区的最后一个填充位是否插入，取决于 CRC 序列的第一位——所以填充必须
    在**整段**上完成，不能只对 "SOF..数据场" 单独填充。
    """
    valid_length = frame.requested_length
    header = [DOMINANT]                                  # SOF
    header += _arbitration_phase(frame)
    header += _control_phase(frame, valid_length)
    if not frame.remote:
        for byte in frame.data:
            header += int_to_bits(byte, 8)

    crc_width, _name = crc_select(valid_length, frame.fd)
    crc_value = crc_compute(header, crc_width)
    crc_bits = int_to_bits(crc_value, crc_width)
    return header + crc_bits, crc_bits


def encode(frame: Frame, apply_stuffing: bool = True) -> List[int]:
    """把一条帧编码成完整的总线位序列。

    返回的位序列依次为：SOF、仲裁场、控制场、数据场、CRC 序列（以上区域施加位
    填充）、CRC 界定符、ACK 场、EOF、IFS。
    ACK 槽按 "已被正确接收" 编码（显性），等价于单节点自测时的正常应答。

    :param frame: 待编码的帧。
    :param apply_stuffing: 是否施加位填充（默认 ``True``）。
    :raises FrameError: 帧结构非法。
    """
    frame.validate()
    stuffed_region, _crc_bits = _crc_region(frame)

    bits = stuff_bits(stuffed_region) if apply_stuffing else list(stuffed_region)
    bits.append(RECESSIVE)                    # CRC 界定符
    bits.append(DOMINANT)                     # ACK 槽（被接收节点拉显性）
    bits.append(RECESSIVE)                    # ACK 界定符
    bits += [RECESSIVE] * 7                   # EOF
    bits += [RECESSIVE] * 3                   # IFS
    return bits


def encode_error_frame() -> List[int]:
    """构造错误帧位序列：错误标志(6 显性) + 错误界定符(8 隐性)。

    错误标志的 6 个连续同极性位是**固定形式**，不做位填充。
    """
    return [DOMINANT] * 6 + [RECESSIVE] * 8


def encode_overload_frame() -> List[int]:
    """构造过载帧位序列：过载标志(6 显性) + 过载界定符(8 隐性)。"""
    return [DOMINANT] * 6 + [RECESSIVE] * 8


# ---------------------------------------------------------------------------
# 位级解码
# ---------------------------------------------------------------------------


@dataclass
class DecodeResult(object):
    """解码结果：帧本体 + 解码过程中的诊断信息。"""

    frame: Frame
    crc_ok: bool = True
    crc_received: int = 0
    crc_computed: int = 0
    crc_width: int = 15
    stuff_bits_count: int = 0
    consumed_bits: int = 0
    ack_slot: int = RECESSIVE
    notes: List[str] = field(default_factory=list)


def _peek(bits: Sequence[int], index: int, count: int) -> List[int]:
    end = index + count
    if end > len(bits):
        raise BitStreamError(
            "bit stream too short: need %d bits at index %d, have %d"
            % (count, index, len(bits))
        )
    return list(bits[index:end])


def decode(bits: Sequence[int], check_crc: bool = True) -> Frame:
    """从位序列解码出一条帧。

    :param bits: 完整位序列（包含 SOF 与尾部的 EOF/IFS）。
    :param check_crc: 是否校验 CRC，校验失败抛 :class:`BitStreamError`。
    :raises BitStreamError: 位流非法、填充错误或 CRC 校验失败。
    :raises FrameError: 解出的字段组合非法。
    """
    return decode_detailed(bits, check_crc=check_crc).frame


def _parse_raw_frame(raw_bits: List[int]) -> Dict[str, object]:
    """在**已去填充**的原始位流上解析定长字段。

    只解析到 DLC 为止（标识符、帧格式、DLC、数据长度、RTR），
    因为后续的 CRC 宽度依赖 DLC、而填充区长度又依赖 CRC 宽度，需要外层迭代。
    """
    if len(raw_bits) < 1 + 12 + 6:
        raise BitStreamError("raw bit stream too short to contain a header")
    if raw_bits[0] != DOMINANT:
        raise BitStreamError("SOF must be dominant")
    base = bits_to_int(raw_bits[1:12])
    twelfth = raw_bits[12]
    pos = 13
    if twelfth == RECESSIVE:
        # 扩展帧：SRR(1) + IDE(1) + 扩展 ID(18)
        if raw_bits[pos] != DOMINANT:
            raise BitStreamError("IDE must be dominant in extended frame")
        pos += 1
        identifier = (base << 18) | bits_to_int(raw_bits[pos:pos + 18])
        pos += 18
        extended = True
    else:
        identifier = base
        extended = False
    if pos >= len(raw_bits):
        raise BitStreamError("raw bit stream ends before RTR")
    remote = raw_bits[pos] == RECESSIVE
    pos += 1

    if pos + 6 > len(raw_bits):
        raise BitStreamError("raw bit stream ends inside the control field")
    fd = raw_bits[pos] == RECESSIVE
    pos += 2                                   # FDF + r0/res
    if fd:
        brs = raw_bits[pos] == RECESSIVE
        pos += 1
        esi = raw_bits[pos] == RECESSIVE
        pos += 1
    else:
        brs = False
        esi = False
    dlc = bits_to_int(raw_bits[pos:pos + 4])
    pos += 4

    if dlc > max_dlc(fd):
        raise FrameError(
            "DLC %d is not legal for %s frame" % (dlc, "CAN FD" if fd else "classic CAN")
        )
    length = dlc_to_length(dlc)
    if not fd and length > CLASSIC_MAX_LENGTH:
        raise FrameError("classic CAN DLC %d maps to %d bytes" % (dlc, length))

    return {
        "identifier": identifier,
        "extended": extended,
        "remote": remote,
        "fd": fd,
        "brs": brs,
        "esi": esi,
        "dlc": dlc,
        "length": length,
        "crc_width": crc_select(length, fd)[0],
        "header_raw_len": pos,
    }


def _expected_raw_len(info: Dict[str, object]) -> int:
    """原始位流中 "SOF..CRC 序列" 的总长度。"""
    header_len = int(info["header_raw_len"])          # type: ignore[arg-type]
    if not info["remote"]:
        header_len += int(info["length"]) * 8         # type: ignore[arg-type]
    return header_len + int(info["crc_width"])        # type: ignore[arg-type]


def decode_detailed(
    bits: Sequence[int],
    check_crc: bool = True,
    allow_truncated_tail: bool = False,
) -> DecodeResult:
    """与 :func:`decode` 相同，但额外返回 CRC / 填充位等诊断信息。

    实现要点：CRC 宽度取决于 DLC，而 DLC 在**已填充**的位流里位置会随前面的
    填充位浮动，因此解码分为两步迭代：

    1. 先对整段做去填充（填充区 = SOF..CRC 序列），得到原始位流；
    2. 在原始位流上解析标识符 / 帧格式 / DLC，算出 "SOF..CRC 序列" 应有的原始
       长度，再据此把去填充结果**裁剪**到正确长度（去填充窗口可能多含了 CRC
       界定符等几个非填充位）。

    :param allow_truncated_tail: 允许位流在填充区尾部被截断（例如抓包工具只
        保存了帧头）。
    """
    bits = list(bits)
    if not bits:
        raise BitStreamError("empty bit stream")
    if bits[0] != DOMINANT:
        raise BitStreamError("SOF must be dominant, got %r" % (bits[0],))

    notes: List[str] = []
    # ---- 第 1 步：贪婪去填充，拿到原始位流的头部 -------------------------
    raw_probe, _consumed = unstuff_prefix(bits)
    if len(raw_probe) < 1 + 12 + 6:
        # 前缀太短通常意味着位流被截断，改用宽松去填充再试一次
        raw_probe, _ = _unstuff(bits, allow_trailing_truncation=True)
    info = _parse_raw_frame(raw_probe)
    expected_raw = _expected_raw_len(info)

    # ---- 第 2 步：搜索填充区在已填充位流中的确切边界 ---------------------
    raw_bits, stuff_count = _scan_stuffed_region(bits, expected_raw)

    # ---- 第 3 步：在原始位流上拆出数据场与 CRC ---------------------------
    pos = int(info["header_raw_len"])                 # type: ignore[arg-type]
    length = int(info["length"])                      # type: ignore[arg-type]
    crc_width = int(info["crc_width"])                # type: ignore[arg-type]
    remote = bool(info["remote"])

    data = bytearray()
    if not remote:
        raw_data = raw_bits[pos:pos + length * 8]
        pos += length * 8
        for offset in range(0, len(raw_data), 8):
            data.append(bits_to_int(raw_data[offset:offset + 8]) & 0xFF)

    crc_received = bits_to_int(raw_bits[pos:pos + crc_width])
    crc_computed = crc_compute(raw_bits[0:pos], crc_width)
    if check_crc and crc_received != crc_computed:
        raise BitStreamError(
            "CRC%d mismatch: received 0x%X, computed 0x%X"
            % (crc_width, crc_received, crc_computed)
        )

    # ---- 第 4 步：非填充尾部（CRC 界定符 / ACK / EOF / IFS）-------------
    index = expected_raw + stuff_count
    delimiter = _peek(bits, index, 1)[0]
    index += 1
    if delimiter != RECESSIVE:
        raise BitStreamError("CRC delimiter must be recessive")
    ack_slot = _peek(bits, index, 1)[0]
    index += 1
    ack_delim = _peek(bits, index, 1)[0]
    index += 1
    if ack_delim != RECESSIVE:
        raise BitStreamError("ACK delimiter must be recessive")
    eof = _peek(bits, index, 7)
    index += 7
    if any(bit != RECESSIVE for bit in eof):
        raise BitStreamError("EOF must be 7 recessive bits")
    if index + 3 <= len(bits):
        ifs = _peek(bits, index, 3)
        index += 3
        if any(bit != RECESSIVE for bit in ifs):
            raise BitStreamError("IFS must be 3 recessive bits")

    frame = Frame(
        identifier=int(info["identifier"]),           # type: ignore[arg-type]
        data=bytes(data),
        extended=bool(info["extended"]),
        remote=remote,
        fd=bool(info["fd"]),
        brs=bool(info["brs"]),
        esi=bool(info["esi"]),
    )
    if remote:
        frame._remote_length = length
    return DecodeResult(
        frame=frame,
        crc_ok=crc_received == crc_computed,
        crc_received=crc_received,
        crc_computed=crc_computed,
        crc_width=crc_width,
        stuff_bits_count=stuff_count,
        consumed_bits=index,
        ack_slot=ack_slot,
        notes=notes,
    )


def roundtrip(frame: Frame) -> Frame:
    """编码再解码，用于自检；解码结果不含时间戳与来源。"""
    return decode(encode(frame))
