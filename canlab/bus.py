# -*- coding: utf-8 -*-
"""CAN 总线与**逐位非破坏性仲裁**仿真。

本模块把一条总线建模成"线与"（wired-AND）介质：显性位(0) 会覆盖隐性位(1)。
所有挂在同一时刻开始发送的节点**逐位**把自己的位写到总线上并回读总线电平，
一旦某节点发出隐性位却读到显性位，它就仲裁失败、立刻转为接收方，并在总线
再次空闲后自动重发。这正是 CAN "非破坏性仲裁" 的核心机制。

时间模型
--------
虚拟时间以**位时间**（bit time）为单位推进，默认位速率 500 kbit/s，即
1 位时间 = 2 µs = 0.002 ms。总线对外提供毫秒级的 ``now_ms`` 供上层脚本使用。
一帧占用的位时间 = 该帧编码后的位个数；帧间固定间隔（IFS）包含在编码位流里。

**只建模数据链路层的行为**：不建模收发器、终端电阻、线束反射、位定时与
采样点、晶振容差、EMI。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import frame as frame_mod
from .errors import (
    BUS_OFF_RECOVERY_SEQUENCE_BITS,
    CanError,
    ErrorCounters,
    ErrorKind,
    ErrorState,
    detect_ack_error,
    detect_bit_error,
    detect_form_error,
    detect_stuff_error,
)
from .frame import DOMINANT, RECESSIVE, Frame

#: 默认位速率（bit/s）
DEFAULT_BITRATE = 500000
#: 总线空闲（隐性）状态
BUS_IDLE = RECESSIVE

#: 帧编码位流中标出的字段边界（用于位错误的场景判断）
#: 处于这些位置之前的位算 "仲裁场"，之后的算 "数据场"
ARBITRATION_BITS_STANDARD = 13
ARBITRATION_BITS_EXTENDED = 32


def bit_us(bitrate: int = DEFAULT_BITRATE) -> float:
    """返回一个位时间对应的微秒数。"""
    if bitrate <= 0:
        raise ValueError("bitrate must be positive")
    return 1e6 / bitrate


def bits_to_ms(bits: int, bitrate: int = DEFAULT_BITRATE) -> float:
    """把位个数换算成毫秒。"""
    return bits * 1000.0 / bitrate


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------


@dataclass
class Node(object):
    """挂在总线上的一个 CAN 节点（ECU 模型）。"""

    name: str
    #: 该节点的错误计数与状态机
    counters: ErrorCounters = field(default_factory=ErrorCounters)
    #: 成功发送的帧数（不含重发）
    tx_success: int = 0
    #: 发送尝试次数（含重发）
    tx_attempts: int = 0
    #: 仲裁失败次数
    arbitration_lost: int = 0
    #: 因仲裁失败导致的重发次数
    retransmissions: int = 0
    #: 成功接收的帧数
    rx_success: int = 0
    #: 发送错误帧的次数
    error_frames_sent: int = 0
    #: 该节点发送/接收过的帧（用于 trace）
    trace: List[Frame] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.counters.node = self.name

    # -- 便捷属性 ---------------------------------------------------------

    @property
    def tec(self) -> int:
        return self.counters.tec

    @property
    def rec(self) -> int:
        return self.counters.rec

    @property
    def error_state(self) -> str:
        return self.counters.state

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<Node %s tec=%d rec=%d state=%s tx=%d>" % (
            self.name, self.tec, self.rec, self.error_state, self.tx_success
        )


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


@dataclass
class ArbitrationRecord(object):
    """一次逐位仲裁的记录。"""

    winner: str
    #: 赢得仲裁的帧标识符
    winner_id: int
    #: 参与本次仲裁的节点名（按注册顺序）
    participants: List[str] = field(default_factory=list)
    #: 各失败节点及其最早失败的位序号
    losers: List[Tuple[str, int]] = field(default_factory=list)
    #: 参与仲裁的帧标识符
    identifiers: List[int] = field(default_factory=list)
    #: 本次仲裁消耗的位时间（等于仲裁场长度）
    arbitration_bits: int = 0
    #: 仲裁开始时的总线虚拟时间（毫秒）
    start_ms: float = 0.0

    @property
    def losers_names(self) -> List[str]:
        return [name for name, _bit in self.losers]


@dataclass
class SendResult(object):
    """一次发送请求的结果。"""

    node: str
    frame: Frame
    sent: bool = False
    #: 仲裁是否获胜
    arbitration_won: bool = True
    lost_at_bit: int = -1
    winner: str = ""
    errors: List[CanError] = field(default_factory=list)
    #: 发送该帧消耗的位时间（含重发与错误帧）
    bits_used: int = 0
    #: 完成时刻（虚拟毫秒）
    completed_ms: float = 0.0


@dataclass
class BusStats(object):
    """总线统计。"""

    total_bits: int = 0
    busy_bits: int = 0
    frames_sent: int = 0
    error_frames: int = 0
    arbitration_events: int = 0
    arbitration_losses: int = 0
    retransmissions: int = 0
    idle_bits: int = 0

    @property
    def load_percent(self) -> float:
        """总线负载率（%）：忙位时间 / 总位时间。"""
        if self.total_bits <= 0:
            return 0.0
        return 100.0 * self.busy_bits / self.total_bits


# ---------------------------------------------------------------------------
# 总线
# ---------------------------------------------------------------------------


class CanBus(object):
    """一条 CAN 总线（逐位仿真的多节点仲裁域）。"""

    def __init__(
        self,
        bitrate: int = DEFAULT_BITRATE,
        name: str = "CAN1",
        min_gap_bits: int = 0,
    ) -> None:
        if bitrate <= 0:
            raise ValueError("bitrate must be positive")
        self.name = name
        self.bitrate = bitrate
        self.bit_time_ms = 1000.0 / bitrate
        #: 相邻两次发送之间强制插入的最小空闲位（0 = 背靠背发送，负载率可达 100%）
        self.min_gap_bits = max(0, min_gap_bits)
        self.nodes: List[Node] = []
        self._nodes_by_name: Dict[str, Node] = {}
        #: 总线虚拟时间（位）
        self.bit_clock = 0
        self.stats = BusStats()
        #: 仲裁记录
        self.arbitrations: List[ArbitrationRecord] = []
        #: 由错误注入产生的、下一次发送时要施加的故障
        self._injections: Dict[str, Dict[str, object]] = {}
        #: 每节点待重发的帧队列
        self._pending: Dict[str, List[Frame]] = {}
        #: 所有已成功发送的帧（按时间顺序），供 trace 导出
        self.trace: List[Frame] = []
        #: 可选：发送成功后的回调（用于 CAPL 层的事件派发）
        self.on_frame: Optional[Callable[[Frame], None]] = None
        #: 可选：位级跟踪回调 (bit_index, bus_level)
        self.on_bit: Optional[Callable[[int, int], None]] = None
        #: bus-off 恢复：是否自动恢复
        self.auto_bus_off_recovery = True
        #: 累计的隐性位（用于 bus-off 恢复）
        self._idle_bits = 0
        #: 事件日志（人类可读）
        self.log: List[str] = []
        #: 确定性伪随机源（错误注入用），LCG 保证可复现
        self._rng_state = 1

    # -- 节点管理 ---------------------------------------------------------

    def add_node(self, name: str) -> Node:
        """向总线注册一个节点。重名会报错。"""
        if name in self._nodes_by_name:
            raise ValueError("node %r already exists" % (name,))
        node = Node(name=name)
        self.nodes.append(node)
        self._nodes_by_name[name] = node
        self._pending[name] = []
        return node

    def node(self, name: str) -> Node:
        """按名字取节点。"""
        try:
            return self._nodes_by_name[name]
        except KeyError:
            raise KeyError("unknown node %r" % (name,))

    @property
    def now_ms(self) -> float:
        """当前虚拟时间（毫秒）。"""
        return self.bit_clock * self.bit_time_ms

    def _advance(self, bits: int, busy: bool = True) -> None:
        """推进虚拟时间并累计负载统计。"""
        if bits <= 0:
            return
        self.bit_clock += bits
        self.stats.total_bits += bits
        if busy:
            self.stats.busy_bits += bits
            self._idle_bits = 0
        else:
            self.stats.idle_bits += bits

    def idle(self, bits: int) -> None:
        """显式在总线上产生一段空闲（隐性）位，用于 bus-off 恢复与负载统计。"""
        for _ in range(bits):
            self._advance(1, busy=False)
            if self.auto_bus_off_recovery:
                for node in self.nodes:
                    node.counters.observe_bus_idle_bit(RECESSIVE)

    def idle_ms(self, milliseconds: float) -> int:
        """按虚拟毫秒产生一段总线空闲，返回对应的位个数（向上取整）。"""
        if milliseconds < 0:
            raise ValueError("milliseconds must be non-negative")
        bits = int(milliseconds / self.bit_time_ms + 0.999999)
        self.idle(bits)
        return bits

    def idle_bits_for(self, milliseconds: float) -> int:
        """把毫秒换算成位个数（向上取整），不推进时间。"""
        if milliseconds < 0:
            raise ValueError("milliseconds must be non-negative")
        return int(milliseconds / self.bit_time_ms + 0.999999)

    def advance_to_ms(self, target_ms: float) -> int:
        """把虚拟时间推进到 ``target_ms``，中途的总线计为空闲。

        :return: 实际推进的位个数（目标时刻已过则返回 0）。
        """
        target_bits = int(target_ms / self.bit_time_ms + 0.999999)
        delta = target_bits - self.bit_clock
        if delta <= 0:
            return 0
        self.idle(delta)
        return delta

    def advance_ms(self, milliseconds: float) -> int:
        """在空闲总线上推进 ``milliseconds`` 毫秒。"""
        return self.idle_ms(milliseconds)

    # -- 错误注入 ---------------------------------------------------------

    def inject_bit_error(self, node_name: str, at_bit: int = 0) -> None:
        """让指定节点的下一次发送在 ``at_bit`` 位置发生位错误。"""
        self._injections[node_name] = {"kind": ErrorKind.BIT, "at_bit": at_bit}

    def inject_stuff_error(self, node_name: str, at_bit: int = 0) -> None:
        """让指定节点的下一次发送出现填充错误。"""
        self._injections[node_name] = {"kind": ErrorKind.STUFF, "at_bit": at_bit}

    def inject_crc_error(self, node_name: str) -> None:
        """让指定节点的下一次发送携带错误的 CRC。"""
        self._injections[node_name] = {"kind": ErrorKind.CRC, "at_bit": 0}

    def inject_form_error(self, node_name: str) -> None:
        """让指定节点的下一次发送出现格式错误（ACK 界定符非法）。"""
        self._injections[node_name] = {"kind": ErrorKind.FORM, "at_bit": 0}

    def inject_ack_error(self, node_name: str) -> None:
        """让指定节点的下一次发送无人应答（ACK 槽为隐性）。"""
        self._injections[node_name] = {"kind": ErrorKind.ACK, "at_bit": 0}

    def clear_injections(self) -> None:
        """清空所有待注入故障。"""
        self._injections.clear()

    # -- 发送 -------------------------------------------------------------

    def _encoded(self, frm: Frame) -> List[int]:
        """把帧编码为**已填充**的完整位流（含 CRC、ACK 槽、EOF、IFS）。"""
        return frame_mod.encode(frm)

    def _logic_bits(self, frm: Frame) -> List[int]:
        """把帧编码为**未填充**的逻辑位流。

        仲裁必须比较逻辑位，而不是已填充位流：位填充是各发送方**各自**根据自己
        的数据独立施加的，两个节点在同一个位序号上可能位于不同的字段，直接比较
        填充后的位流会得出错误的仲裁结果（例如标准帧 0x123 会被误判为输给
        扩展帧 0x123）。
        """
        return frame_mod.encode(frm, apply_stuffing=False)

    def _arbitrate(
        self,
        contending: List[Tuple[Optional[Node], List[int]]],
        start_bit: int,
    ) -> Tuple[List[Tuple[Optional[Node], List[int]]],
               List[Tuple[Optional[Node], int]],
               ArbitrationRecord]:
        """**逐位**非破坏性仲裁。

        每一轮把仍处于发送状态的节点写出的逻辑位做"线与"（显性 0 覆盖隐性 1），
        写出隐性位却读到显性位的节点立即退出（转为接收方），直到只剩一个节点。

        :param contending: ``[(节点, 逻辑位流)]``，均在同一时刻开始发送。
            节点可以为 ``None``（纯位流层面的仲裁实验，不需要注册节点）。
        :param start_bit: 仲裁开始时的总线位时钟。
        :return: ``(获胜者列表, 失败者列表[(节点, 失败位序号)], 仲裁记录)``
        """
        active = list(contending)
        losers: List[Tuple[Optional[Node], int]] = []
        arb_len = max(len(bits) for _n, bits in active)

        def name_of(node: Optional[Node]) -> str:
            return node.name if node is not None else "<anonymous>"

        bit_index = 0
        while bit_index < arb_len and len(active) > 1:
            bus_level = RECESSIVE
            for _node, bits in active:
                if bit_index < len(bits) and bits[bit_index] == DOMINANT:
                    bus_level = DOMINANT
                    break
            survivors: List[Tuple[Optional[Node], List[int]]] = []
            for node, bits in active:
                own = bits[bit_index] if bit_index < len(bits) else RECESSIVE
                if own == bus_level:
                    survivors.append((node, bits))
                else:
                    losers.append((node, bit_index))
            active = survivors
            bit_index += 1

        record = ArbitrationRecord(
            winner=name_of(active[0][0]) if active else "",
            winner_id=0,
            participants=[name_of(node) for node, _b in contending],
            losers=[(name_of(node), pos) for node, pos in losers],
            identifiers=[],
            arbitration_bits=bit_index,
            start_ms=start_bit * self.bit_time_ms,
        )
        return active, losers, record

    def send(
        self,
        node_name: str,
        frm: Frame,
        max_retries: int = 8,
        allow_retransmission: bool = True,
    ) -> SendResult:
        """请求某个节点发送一帧。

        当总线空闲且只有一个节点请求发送时，直接进行仲裁（此时仲裁必然获胜，
        但仍逐位走完全过程以模拟真实时序）。

        更常见的用法是 :func:`contend`：同一时刻多个节点一起发送，由总线完成
        逐位仲裁并自动重发失败者。

        :param node_name: 发送节点名。
        :param frm: 待发送的帧。
        :param max_retries: 仲裁失败后最多重发次数。
        :param allow_retransmission: 是否允许仲裁失败自动重发。
        :raises KeyError: 节点不存在。
        """
        node = self.node(node_name)
        result = SendResult(node=node_name, frame=frm)
        if node.counters.is_bus_off:
            result.errors.append(
                CanError(kind="bus-off", node=node_name, detail="节点处于 bus-off，发送被拒绝")
            )
            return result

        self._pending[node_name].append(frm)
        winners = self._resolve_pending(max_retries=max_retries,
                                        allow_retransmission=allow_retransmission)
        for wnode, wframe in winners:
            if wnode.name == node_name and wframe is frm:
                result.sent = True
                result.winner = wnode.name
                result.completed_ms = self.now_ms
                return result
        # 没轮到自己：说明仲裁失败（或帧被丢弃）
        for record in reversed(self.arbitrations):
            if node_name in record.losers_names:
                result.arbitration_won = False
                result.lost_at_bit = dict(record.losers)[node_name]
                result.winner = record.winner
                break
        result.completed_ms = self.now_ms
        return result

    def contend(
        self,
        requests: Sequence[Tuple[str, Frame]],
        max_retries: int = 8,
    ) -> List[SendResult]:
        """多个节点在同一时刻开始发送，逐位仲裁并自动重发失败者。

        :param requests: ``[(节点名, 帧)]``。
        :param max_retries: 每个节点的最大重发次数；超过后该帧被丢弃。
        :return: 与输入顺序一一对应的 :class:`SendResult` 列表。
        """
        results: Dict[int, SendResult] = {}
        retries: Dict[str, int] = {}
        first_record = len(self.arbitrations)
        for index, (name, frm) in enumerate(requests):
            results[index] = SendResult(node=name, frame=frm)
            retries[name] = 0
            if not self.node(name).counters.is_bus_off:
                self._pending[name].append(frm)

        guard = (max_retries + 2) * (len(requests) + 1)
        rounds = 0
        while any(self._pending.values()) and rounds <= guard:
            rounds += 1
            sent = self._resolve_pending(max_retries=max_retries,
                                         allow_retransmission=True)
            for wnode, wframe in sent:
                for index, (name, frm) in enumerate(requests):
                    if name == wnode.name and frm is wframe:
                        results[index].sent = True
                        results[index].winner = wnode.name
            # 超过重发上限的帧被丢弃，避免无限循环
            for node in self.nodes:
                if self._pending[node.name]:
                    if retries[node.name] >= max_retries:
                        self._pending[node.name].clear()
                        self.log.append(
                            "drop: node=%s exceeded max retries (%d)"
                            % (node.name, max_retries)
                        )
                    else:
                        retries[node.name] += 1
            if not sent and not any(self._pending.values()):
                break

        related = self.arbitrations[first_record:]
        for index, (name, frm) in enumerate(requests):
            res = results[index]
            res.completed_ms = self.now_ms
            # 即使最终通过重发送成功了，也要如实记录它曾经输掉过仲裁
            for record in reversed(related):
                if name in record.losers_names:
                    res.arbitration_won = False
                    res.lost_at_bit = dict(record.losers)[name]
                    res.winner = record.winner
                    break
        return [results[i] for i in range(len(requests))]

    def _apply_min_gap(self) -> None:
        """按 ``min_gap_bits`` 在两次发送之间插入空闲位（保证总线不是永远 100% 忙）。"""
        if self.min_gap_bits > 0:
            self.idle(self.min_gap_bits)

    def _resolve_pending(
        self,
        max_retries: int,
        allow_retransmission: bool,
    ) -> List[Tuple[Node, Frame]]:
        """处理一轮：所有当前有待发帧的节点同时参与一次逐位仲裁。

        :return: 本轮成功发送的 ``(节点, 帧)`` 列表。
        """
        contending_nodes = [node for node in self.nodes if self._pending[node.name]]
        if not contending_nodes:
            return []

        if len(contending_nodes) > 1:
            self.stats.arbitration_events += 1

        # 上一帧与本帧之间的最小空闲（模拟真实的帧间间隔，让负载率不会恒为 100%）
        self._apply_min_gap()

        contending: List[Tuple[Node, List[int], Frame]] = []
        for node in contending_nodes:
            frm = self._pending[node.name][0]
            contending.append((node, self._logic_bits(frm), frm))

        active, losers, record = self._arbitrate(
            [(node, bits) for node, bits, _f in contending], self.bit_clock
        )
        record.identifiers = [frm.identifier for _n, _b, frm in contending]
        if active:
            record.winner_id = next(
                frm.identifier for node, _b, frm in contending if node.name == record.winner
            )

        # 仲裁期只有 "失败节点最早退出的那一位" 之前是共享的
        shared_bits = max((pos for _n, pos in losers), default=0)
        if losers:
            self.arbitrations.append(record)
            self.log.append(
                "arbitration: winner=%s(0x%X) losers=%s"
                % (record.winner, record.winner_id,
                   ", ".join("%s@bit%d" % (n, b) for n, b in record.losers))
            )
            for node, _pos in losers:
                node.arbitration_lost += 1
                node.tx_attempts += 1
                node.retransmissions += 1
                self.stats.arbitration_losses += 1
                self.stats.retransmissions += 1
            # 失败者在 lost_at_bit 之后立刻转为接收方，只贡献共享的位时间
            self._advance(shared_bits, busy=True)

        sent: List[Tuple[Node, Frame]] = []
        for node, _logic in active:
            frm = self._pending[node.name][0]
            self._pending[node.name].pop(0)
            # 用已填充位流发送；前 shared_bits 个逻辑位已经在仲裁阶段计时，
            # 这里把它们对应的已填充位一并跳过，避免重复计入总线负载
            full_bits = self._encoded(frm)
            skip = self._stuffed_index_for(frm, shared_bits)
            self._transmit_bits(node, frm, full_bits, skip_bits=skip)
            sent.append((node, frm))
        return sent

    @staticmethod
    def _stuffed_index_for(frm: Frame, logic_index: int) -> int:
        """返回逻辑位序号 ``logic_index`` 在已填充位流中对应的下标。"""
        logic = frame_mod.encode(frm, apply_stuffing=False)[0:logic_index]
        return len(frame_mod.stuff_bits(logic))

    def _transmit_bits(
        self,
        node: Node,
        frm: Frame,
        bits: List[int],
        skip_bits: int = 0,
    ) -> bool:
        """把一帧的位流送上线，处理错误注入与错误帧。

        :param skip_bits: 该帧前若干位已经在仲裁阶段计时（由所有竞争节点共享），
            这里不再重复计入总线负载。
        :return: 帧是否成功发送（未被错误破坏）。
        """
        injection = self._injections.pop(node.name, None)
        node.tx_attempts += 1
        remaining = max(0, len(bits) - skip_bits)

        if injection is not None:
            kind = str(injection["kind"])
            at_bit = int(injection["at_bit"])  # type: ignore[arg-type]
            return self._handle_injected_error(node, frm, remaining, kind, at_bit)

        self._advance(remaining, busy=True)
        if self.on_bit is not None:
            for index in range(skip_bits, len(bits)):
                self.on_bit(index, bits[index])
        frm.timestamp = self.now_ms
        frm.source = node.name
        node.tx_success += 1
        node.counters.on_transmit_success()
        node.trace.append(frm)
        self.trace.append(frm)
        self.stats.frames_sent += 1
        for other in self.nodes:
            if other.name != node.name:
                other.rx_success += 1
                other.counters.on_receive_success()
        if self.on_frame is not None:
            self.on_frame(frm)
        return True

    def _handle_injected_error(
        self,
        node: Node,
        frm: Frame,
        remaining_bits: int,
        kind: str,
        at_bit: int,
    ) -> bool:
        """施加一次注入故障并产生错误帧，更新 TEC/REC。"""
        self._advance(max(0, remaining_bits), busy=True)
        node.error_frames_sent += 1
        self.stats.error_frames += 1
        detail = "%s error injected at bit %d" % (kind, at_bit)
        node.counters.on_transmit_error(kind, detail)
        self.log.append("error: node=%s kind=%s %s" % (node.name, kind, detail))

        # 错误标志是固定形式（6 个显性位）紧接 8 位错误界定符，会破坏当前帧，
        # 总线上所有节点都丢弃这一帧
        error_frame_bits = 6 + 8
        self._advance(error_frame_bits, busy=True)
        for other in self.nodes:
            if other.name == node.name:
                continue
            other.counters.on_receive_error(
                kind, already_flagged=True, detail="observed error frame"
            )
        return False

    # -- 统计 -------------------------------------------------------------

    def node_stats(self) -> Dict[str, Dict[str, object]]:
        """返回每节点的统计快照。"""
        out: Dict[str, Dict[str, object]] = {}
        for node in self.nodes:
            out[node.name] = {
                "tx_success": node.tx_success,
                "tx_attempts": node.tx_attempts,
                "arbitration_lost": node.arbitration_lost,
                "retransmissions": node.retransmissions,
                "rx_success": node.rx_success,
                "tec": node.tec,
                "rec": node.rec,
                "state": node.error_state,
                "error_frames_sent": node.error_frames_sent,
            }
        return out

    def worst_case_response_ms(self, node_name: str) -> float:
        """节点最长帧间隔（最坏响应延迟的近似：相邻两次成功发送的最大间隔）。

        真实的最坏响应时间需要按优先级与流量模型推导；这里给出的是**实测**的
        相邻成功发送间隔最大值，作为可复现的观测指标。
        """
        node = self.node(node_name)
        stamps = [frm.timestamp for frm in node.trace]
        if len(stamps) < 2:
            return 0.0
        return max(b - a for a, b in zip(stamps, stamps[1:]))

    def frame_gap_ms(self) -> List[float]:
        """总线层面相邻两帧之间的空闲时间（毫秒）。"""
        gaps: List[float] = []
        for prev, cur in zip(self.trace, self.trace[1:]):
            gaps.append(cur.timestamp - prev.timestamp)
        return gaps

    def export_trace(self) -> List[Dict[str, object]]:
        """把成功发送的帧导出为可 JSON 序列化的结构。"""
        return [
            {
                "t_ms": round(frm.timestamp, 6),
                "id": frm.identifier,
                "ext": frm.extended,
                "rtr": frm.remote,
                "fd": frm.fd,
                "dlc": frm.dlc,
                "data": list(frm.data),
                "src": frm.source,
            }
            for frm in self.trace
        ]

    @staticmethod
    def load_trace(entries: Sequence[Dict[str, object]]) -> List[Frame]:
        """从 :func:`export_trace` 的结构还原帧序列（trace 重放用）。"""
        frames: List[Frame] = []
        for entry in entries:
            raw = entry.get("data", [])
            data = bytes(int(b) & 0xFF for b in raw)  # type: ignore[union-attr]
            frm = Frame(
                identifier=int(entry.get("id", 0)),     # type: ignore[arg-type]
                data=data,
                extended=bool(entry.get("ext", False)),
                remote=bool(entry.get("rtr", False)),
                fd=bool(entry.get("fd", False)),
            )
            frm.timestamp = float(entry.get("t_ms", 0.0))  # type: ignore[arg-type]
            frm.source = str(entry.get("src", ""))
            frames.append(frm)
        return frames

    def reset(self) -> None:
        """清空统计与 trace（保留节点注册）。"""
        self.bit_clock = 0
        self.stats = BusStats()
        self.arbitrations = []
        self.trace = []
        self.log = []
        self._injections.clear()
        self._idle_bits = 0
        for node in self.nodes:
            node.tx_success = 0
            node.tx_attempts = 0
            node.arbitration_lost = 0
            node.retransmissions = 0
            node.rx_success = 0
            node.error_frames_sent = 0
            node.trace = []
            node.counters = ErrorCounters(node=node.name)
            self._pending[node.name] = []
