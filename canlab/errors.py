# -*- coding: utf-8 -*-
"""CAN 错误处理：五类错误检测、TEC/REC 计数与错误状态机。

依据 ISO 11898-1 中关于错误检测、错误计数与错误限制（fault confinement）的
规定自行实现。**本模块是纯软件模型**，不涉及真实收发器、位定时与采样点。

五类错误检测
------------
================  ==========================================================
位错误(bit)        发送方在仲裁场之外回读到的位与它发出的位不同；或
                   接收方在仲裁场/ACK 槽阶段发出隐性位却读到显性位。
填充错误(stuff)    在需要位填充的区间里出现 6 个连续同极性位。
CRC 错误(crc)      接收方算出的 CRC 与帧内 CRC 序列不一致。
格式错误(form)     固定形式位段出现非法位（CRC 界定符、ACK 界定符、EOF、
                   IFS 等）。
ACK 错误(ack)      发送方在 ACK 槽读到隐性位，即没有任何节点应答。
================  ==========================================================

TEC/REC 计数口径（本项目的实现口径，ISO 11898-1 的软件参考行为）
-----------------------------------------------------------------
发送错误计数器 TEC：

* 发送方检测到错误（发送错误帧）: ``TEC += 8``
* 发送成功: ``TEC -= 1``（下限 0）
* 处于 error-passive 的节点检测到错误时，按标准的错误被动规则加 8
  **不再**为其增加 "额外 +8"（本项目不建模该附加规则，见 README 的
  "已知简化与未实现"）
* 发送方在仲裁中失败或被干扰而重发、但没有检测到错误: 不加不减

接收错误计数器 REC：

* 接收方检测到错误: ``REC += 1``
* 接收成功: ``REC -= 1``（下限 0）
* 接收方在发出错误标志后**第一个**检测到错误的节点: ``REC += 8``
  （ISO 11898-1 中的例外：接收方检测到错误后先 +8，若之后又检测到错误则
  ``REC += 8`` 规则仅适用一次，后续按 +1 计）
* 节点处于 error-active 且 REC > 127 时，REC 不再增加、改为 ``REC = 127``
  （避免接收方单方面把总线推向 bus-off）

状态迁移（ISO 11898-1 fault confinement）：

* ``error-active``  → ``error-passive``：``TEC > 127`` 或 ``REC > 127``
* ``error-passive`` → ``bus-off``：``TEC > 255``
* ``bus-off`` → ``error-active``：检测到 128 次 "连续 11 个隐性位"，随后
  TEC/REC 清零
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .frame import DOMINANT, RECESSIVE

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 错误计数器上限（无符号 8 位，实际达到该值即进入 bus-off）
TEC_BUS_OFF_THRESHOLD = 255
#: 进入 error-passive 的错误计数器阈值（超过 127）
ERROR_PASSIVE_THRESHOLD = 127
#: 错误标志的固定形式长度（6 个同极性位）
ERROR_FLAG_BITS = 6
#: 错误界定符长度
ERROR_DELIMITER_BITS = 8
#: bus-off 恢复需要的 "连续 11 个隐性位" 次数
BUS_OFF_RECOVERY_SEQUENCES = 128
#: bus-off 恢复序列长度（11 个隐性位）
BUS_OFF_RECOVERY_SEQUENCE_BITS = 11


class ErrorKind(object):
    """五类错误检测的类型常量。"""

    BIT = "bit"
    STUFF = "stuff"
    CRC = "crc"
    FORM = "form"
    ACK = "ack"

    ALL = (BIT, STUFF, CRC, FORM, ACK)


class ErrorState(object):
    """错误限制状态（fault confinement state）。"""

    ERROR_ACTIVE = "error-active"
    ERROR_PASSIVE = "error-passive"
    BUS_OFF = "bus-off"

    ALL = (ERROR_ACTIVE, ERROR_PASSIVE, BUS_OFF)


# ---------------------------------------------------------------------------
# 错误事件
# ---------------------------------------------------------------------------


@dataclass
class CanError(object):
    """一次错误检测的记录。"""

    kind: str
    node: str = ""
    bit_position: int = -1
    detail: str = ""
    tec_delta: int = 0
    rec_delta: int = 0
    state_before: str = ErrorState.ERROR_ACTIVE
    state_after: str = ErrorState.ERROR_ACTIVE

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<CanError %s node=%s bit=%d %s>" % (
            self.kind, self.node, self.bit_position, self.detail
        )


# ---------------------------------------------------------------------------
# 五类错误的检测函数
# ---------------------------------------------------------------------------


def detect_bit_error(
    tx_bit: int,
    rx_bit: int,
    in_arbitration: bool = False,
    is_ack_slot: bool = False,
    is_receiver: bool = False,
) -> bool:
    """位错误检测。

    :param tx_bit: 本节点发出的位。
    :param rx_bit: 本节点在总线上回读到的位。
    :param in_arbitration: 是否处于仲裁场（含 SOF）。
    :param is_ack_slot: 是否处于 ACK 槽。
    :param is_receiver: 本节点是接收方还是发送方。
    :return: 是否构成位错误。

    规则：

    * 发送方：在仲裁场之外的任何位置，发出隐性位却回读到显性位 → 位错误
      （发出显性位回读到显性位属于正常的总线 "与" 语义，不算错误）。
    * 发送方：仲裁场内的回读差异是**仲裁失败**，不是错误。
    * 接收方：在仲裁场或 ACK 槽阶段主动发出隐性位却读到显性位 → 位错误。
    """
    if tx_bit == rx_bit:
        return False
    if is_receiver:
        # 接收方只在 "自己发隐性、总线为显性" 且处于仲裁场/ACK 槽时才算位错误
        return tx_bit == RECESSIVE and rx_bit == DOMINANT and (in_arbitration or is_ack_slot)
    if in_arbitration:
        return False
    return tx_bit == RECESSIVE and rx_bit == DOMINANT


def detect_stuff_error(bits: Sequence[int], threshold: int = 5) -> Optional[int]:
    """填充错误检测：返回第一个出现 6 个连续同极性位的位置下标。"""
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


def detect_crc_error(received_crc: int, computed_crc: int) -> bool:
    """CRC 错误检测：接收方算出的 CRC 与帧内序列不一致。"""
    return received_crc != computed_crc


class FormErrorKind(object):
    """格式错误的细分原因。"""

    CRC_DELIMITER = "crc-delimiter"
    ACK_DELIMITER = "ack-delimiter"
    EOF = "eof"
    IFS = "ifs"
    DLC = "dlc"
    RESERVED_BIT = "reserved-bit"
    SOF = "sof"
    OTHER = "other"


def detect_form_error(
    crc_delimiter: int = RECESSIVE,
    ack_delimiter: int = RECESSIVE,
    eof_bits: Sequence[int] = (),
    ifs_bits: Sequence[int] = (),
) -> Optional[str]:
    """格式错误检测：返回第一个违规的固定形式位段名，全部合法则返回 ``None``。"""
    if crc_delimiter != RECESSIVE:
        return FormErrorKind.CRC_DELIMITER
    if ack_delimiter != RECESSIVE:
        return FormErrorKind.ACK_DELIMITER
    if eof_bits and any(bit != RECESSIVE for bit in eof_bits):
        return FormErrorKind.EOF
    if ifs_bits and any(bit != RECESSIVE for bit in ifs_bits):
        return FormErrorKind.IFS
    return None


def detect_ack_error(ack_slot: int) -> bool:
    """ACK 错误检测：发送方在 ACK 槽读到隐性位（无人应答）。"""
    return ack_slot == RECESSIVE


# ---------------------------------------------------------------------------
# 错误计数器
# ---------------------------------------------------------------------------


def next_tec(tec: int, error: bool, success: bool) -> int:
    """发送错误计数器的下一状态（错误 +8、成功 -1、下限 0）。"""
    if error:
        return tec + 8
    if success:
        return max(0, tec - 1)
    return tec


def next_rec(rec: int, error: bool, success: bool, already_flagged: bool = False,
             error_active: bool = True) -> int:
    """接收错误计数器的下一状态。

    :param already_flagged: 本节点是否已经为当前这个错误发出过错误标志。
        第一个检测到错误的接收方按 ISO 11898-1 的规则加 8；如果是**发送方**
        首先检测到错误（此时 ``already_flagged`` 对外部调用者应为 ``True``），
        接收方只加 1。
    :param error_active: 节点是否处于 error-active；error-active 的接收方
        REC 不会增长到超过 127。
    """
    if error:
        if already_flagged:
            # 发送方已先发出错误标志，接收方只计 +1
            value = rec + 1
        else:
            value = rec + 8
        if error_active and value > ERROR_PASSIVE_THRESHOLD:
            return ERROR_PASSIVE_THRESHOLD
        return value
    if success:
        return max(0, rec - 1)
    return rec


def error_state_for(tec: int, rec: int) -> str:
    """根据 TEC/REC 计算错误限制状态（不含 bus-off，bus-off 由 TEC 触发后单独维护）。"""
    if tec > TEC_BUS_OFF_THRESHOLD:
        return ErrorState.BUS_OFF
    if tec > ERROR_PASSIVE_THRESHOLD or rec > ERROR_PASSIVE_THRESHOLD:
        return ErrorState.ERROR_PASSIVE
    return ErrorState.ERROR_ACTIVE


# ---------------------------------------------------------------------------
# 错误状态机（单节点）
# ---------------------------------------------------------------------------


@dataclass
class ErrorCounters(object):
    """一个 CAN 控制器的错误计数与错误限制状态机。"""

    node: str = ""
    tec: int = 0
    rec: int = 0
    state: str = ErrorState.ERROR_ACTIVE
    #: bus-off 恢复过程中已经累计的 "连续 11 个隐性位" 次数
    recovery_sequences: int = 0
    #: 恢复过程中当前 11 位序列已累计的隐性位个数
    idle_run: int = 0
    #: 总线空闲后是否允许自动恢复（标准行为为 True；快速恢复关闭该行为）
    auto_recovery: bool = True
    #: 历史峰值，便于测试与报告
    tec_peak: int = 0
    rec_peak: int = 0
    #: 各类错误累计次数
    error_counts: Dict[str, int] = field(default_factory=dict)

    def _bump(self, kind: str) -> None:
        self.error_counts[kind] = self.error_counts.get(kind, 0) + 1

    # -- 事件入口 ---------------------------------------------------------

    def on_transmit_error(self, kind: str, detail: str = "") -> CanError:
        """发送方检测到错误：TEC += 8，并可能迁移到 error-passive / bus-off。"""
        before = self.state
        self._bump(kind)
        self.tec = next_tec(self.tec, error=True, success=False)
        self.tec_peak = max(self.tec_peak, self.tec)
        self.state = error_state_for(self.tec, self.rec)
        if self.state == ErrorState.BUS_OFF:
            self.recovery_sequences = 0
        return CanError(
            kind=kind, node=self.node, detail=detail,
            tec_delta=8, state_before=before, state_after=self.state,
        )

    def on_receive_error(self, kind: str, already_flagged: bool = False,
                         detail: str = "") -> CanError:
        """接收方检测到错误：按 ISO 11898-1 的规则更新 REC。"""
        before = self.state
        self._bump(kind)
        delta = 1 if already_flagged else 8
        self.rec = next_rec(
            self.rec, error=True, success=False,
            already_flagged=already_flagged,
            error_active=(self.state == ErrorState.ERROR_ACTIVE),
        )
        self.rec_peak = max(self.rec_peak, self.rec)
        self.state = error_state_for(self.tec, self.rec)
        return CanError(
            kind=kind, node=self.node, detail=detail,
            rec_delta=delta, state_before=before, state_after=self.state,
        )

    def on_transmit_success(self) -> None:
        """发送成功：TEC -= 1（下限 0）。bus-off 状态下不计数。"""
        if self.state == ErrorState.BUS_OFF:
            return
        self.tec = next_tec(self.tec, error=False, success=True)
        self.state = error_state_for(self.tec, self.rec)

    def on_receive_success(self) -> None:
        """接收成功：REC -= 1（下限 0）。"""
        if self.state == ErrorState.BUS_OFF:
            return
        self.rec = next_rec(self.rec, error=False, success=True)
        self.state = error_state_for(self.tec, self.rec)

    # -- bus-off ----------------------------------------------------------

    @property
    def is_bus_off(self) -> bool:
        return self.state == ErrorState.BUS_OFF

    def observe_bus_idle_bit(self, bit: int) -> bool:
        """向状态机送入一个总线位，用于 bus-off 恢复计数。

        :return: 本次调用是否完成了恢复（状态回到 error-active）。

        只有处于 bus-off 的节点才关心总线位。恢复条件是连续 128 次
        "11 个隐性位" 序列；任何一次读到显性位都会把计数清零重新开始。
        """
        if self.state != ErrorState.BUS_OFF:
            return False
        if not self.auto_recovery:
            return False
        if bit == DOMINANT:
            self.recovery_sequences = 0
            self.idle_run = 0
            return False
        self.idle_run += 1
        if self.idle_run >= BUS_OFF_RECOVERY_SEQUENCE_BITS:
            self.idle_run = 0
            self.recovery_sequences += 1
            if self.recovery_sequences >= BUS_OFF_RECOVERY_SEQUENCES:
                self.recover_from_bus_off()
                return True
        return False

    def reset_idle_run(self) -> None:
        """总线出现非空闲活动时调用，清空当前 11 位序列的进行中计数。"""
        self.idle_run = 0

    def recover_from_bus_off(self) -> None:
        """执行 bus-off 恢复：TEC/REC 归零，回到 error-active。"""
        self.tec = 0
        self.rec = 0
        self.recovery_sequences = 0
        self.idle_run = 0
        self.state = ErrorState.ERROR_ACTIVE

    def force_bus_off(self) -> None:
        """直接置为 bus-off（测试与错误注入使用）。"""
        self.tec = TEC_BUS_OFF_THRESHOLD + 1
        self.tec_peak = max(self.tec_peak, self.tec)
        self.state = ErrorState.BUS_OFF
        self.recovery_sequences = 0
        self.idle_run = 0

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<ErrorCounters %s tec=%d rec=%d state=%s>" % (
            self.node, self.tec, self.rec, self.state
        )
