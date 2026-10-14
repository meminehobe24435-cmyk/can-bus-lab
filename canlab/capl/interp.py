# -*- coding: utf-8 -*-
"""CAPL 风格脚本的解释器与**虚拟时间调度器**。

设计要点
--------
* 解释器用**生成器**实现：普通语句立即求值，只有 ``testWaitForMessage`` 一类
  会阻塞的测试原语才 ``yield`` 一个请求给调度器。
* 调度器维护虚拟毫秒时钟，按时间顺序派发定时器回调、报文事件与阻塞等待的
  恢复点。所有随机性都来自可指定种子的确定性 LCG，**同一脚本连跑两次结果
  逐字节一致**。
* 报文发送走 :class:`canlab.bus.CanBus`，由总线的逐位仲裁决定真正的发送顺序，
  因此``output()`` 的顺序不等于总线上的顺序——这正是 CAN 的真实行为。

脚本能力见 :mod:`canlab.capl.parser` 的模块文档。
"""

from __future__ import annotations

import io
import math
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..bus import ArbitrationRecord, CanBus
from ..frame import DOMINANT, RECESSIVE, Frame, dlc_to_length, length_to_dlc
from .parser import (
    AssignExpr, BinaryOp, Block, BreakStmt, CallExpr, CharLiteral, ContinueStmt,
    DoWhileStmt, EventHandler, ExprStmt, ForStmt, FunctionDecl, Identifier,
    IfStmt, IncDecExpr, IndexAccess, IntLiteral, MemberAccess, Node, ParseError,
    ReturnStmt, Script, StringLiteral, TernaryOp, ThisExpr, TYPE_INFO, UnaryOp,
    VarDecl, WhileStmt, count_nodes, parse,
)

#: 整数类型的位宽掩码与是否有符号
INT_MASK = {name: (1 << bits) - 1 for name, (bits, _signed) in TYPE_INFO.items()
            if bits is not None and name not in ("float", "double")}
INT_BITS = {name: bits for name, (bits, _signed) in TYPE_INFO.items()
            if bits is not None and name not in ("float", "double")}
INT_SIGNED = {name: signed for name, (bits, signed) in TYPE_INFO.items()
              if bits is not None and name not in ("float", "double")}


def wrap_integer(value: int, type_name: str) -> int:
    """按声明类型做定宽截断；有符号类型按补码解释回负数。

    CAPL 的 ``int`` 是 16 位**有符号**类型，与 C 一致：``-5`` 存进去再读出来
    仍然是 ``-5``，而不是 ``65531``。这一点直接影响 ``setTimer(t, 10 + jitter)``
    这类表达式，所以必须建模。
    """
    mask = INT_MASK.get(type_name)
    if mask is None:
        return value
    value = int(value) & mask
    if INT_SIGNED.get(type_name, False):
        bits = INT_BITS[type_name]
        sign_bit = 1 << (bits - 1)
        if value & sign_bit:
            value -= (1 << bits)
    return value


class CaplRuntimeError(Exception):
    """脚本运行期错误（未定义变量、类型不匹配、除零等）。"""

    def __init__(self, message: str, line: int = 0, column: int = 0) -> None:
        prefix = "line %d:%d: " % (line, column) if line else ""
        super(CaplRuntimeError, self).__init__(prefix + message)
        self.line = line
        self.column = column


# ---------------------------------------------------------------------------
# 值类型
# ---------------------------------------------------------------------------


class MsgValue(object):
    """脚本里的 ``message`` 对象。"""

    def __init__(self, can_id: int = 0, dlc: int = 8, extended: bool = False,
                 data: Optional[bytes] = None, fd: bool = False) -> None:
        self.can_id = can_id
        self.extended = extended
        self.fd = fd
        self._dlc = dlc
        self._data = bytearray(data if data is not None else bytes(dlc))
        self.dir = "TX"
        self.can = 1
        self.brs = False
        #: 该报文是否已被 ``output()`` 送出（避免重复发送）
        self.sent = False

    # -- 数据访问 ---------------------------------------------------------

    def get_byte(self, index: int) -> int:
        if index < 0 or index >= len(self._data):
            raise CaplRuntimeError("message byte index %d out of range (dlc=%d)"
                                   % (index, len(self._data)))
        return self._data[index]

    def set_byte(self, index: int, value: int) -> None:
        if index < 0 or index >= len(self._data):
            raise CaplRuntimeError("message byte index %d out of range (dlc=%d)"
                                   % (index, len(self._data)))
        self._data[index] = value & 0xFF

    @property
    def dlc(self) -> int:
        return self._dlc

    @dlc.setter
    def dlc(self, value: int) -> None:
        value = int(value)
        if value < 0 or value > 15:
            raise CaplRuntimeError("illegal DLC %d" % value)
        self._dlc = value
        length = min(dlc_to_length(value), 64)
        if len(self._data) < length:
            self._data.extend(b"\x00" * (length - len(self._data)))
        else:
            del self._data[length:]

    @property
    def length(self) -> int:
        return len(self._data)

    @property
    def data(self) -> bytes:
        return bytes(self._data)

    def set_data(self, data: bytes) -> None:
        self._data = bytearray(data)
        self._dlc = length_to_dlc(len(data)) if len(data) <= 64 else 8

    def to_frame(self) -> Frame:
        """转换成总线帧对象。``dlc`` 决定实际上线的字节数。"""
        length = min(dlc_to_length(self._dlc), self.fd and 64 or 8)
        payload = self.data[:length]
        if len(payload) < length:
            payload = payload + b"\x00" * (length - len(payload))
        return Frame(identifier=self.can_id, data=payload,
                     extended=self.extended, fd=self.fd, brs=self.brs)

    def copy(self) -> "MsgValue":
        clone = MsgValue(can_id=self.can_id, dlc=self._dlc,
                         extended=self.extended, data=self.data, fd=self.fd)
        clone.brs = self.brs
        clone.dir = self.dir
        clone.can = self.can
        return clone

    def hex(self) -> str:
        return " ".join("%02X" % b for b in self._data)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<msg 0x%X dlc=%d [%s]>" % (self.can_id, self._dlc, self.hex())


class TimerValue(object):
    """脚本里的 ``msTimer`` 对象。"""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.period_ms: float = 0.0
        self.running = False
        self.fires = 0
        self.deadline_ms: float = 0.0

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<timer %s %s %.1fms>" % (
            self.name, "running" if self.running else "idle", self.period_ms)


@dataclass
class CallFrame(object):
    """函数调用的作用域。"""

    scopes: List[Dict[str, object]] = field(default_factory=list)
    function: str = ""


class ReturnSignal(Exception):
    """``return`` 语句的控制流信号。"""

    def __init__(self, value: object = None) -> None:
        super(ReturnSignal, self).__init__("return")
        self.value = value


class BreakSignal(Exception):
    """``break`` 语句的控制流信号。"""


class ContinueSignal(Exception):
    """``continue`` 语句的控制流信号。"""


# ---------------------------------------------------------------------------
# 等待请求（生成器 yield 出去的对象）
# ---------------------------------------------------------------------------


@dataclass
class WaitRequest(object):
    """解释器向调度器发出的等待请求。"""

    kind: str                                # message / time
    can_id: Optional[int] = None
    timeout_ms: Optional[float] = None
    deadline_ms: float = 0.0
    wildcard: bool = False
    matched_frame: Optional[Frame] = None


@dataclass
class SuspendedHandler(object):
    """一个因为等待请求而挂起的处理器。

    挂起时必须把**调用栈帧、``this`` 绑定、生成器与剩余语句**一起保存下来，
    否则恢复后就丢失了局部变量作用域（例如 ``int r; r = testWaitForMessage(...)``
    里的 ``r``），也无法从挂起点继续往下执行。
    """

    handler: EventHandler
    frame: CallFrame
    gen: object
    remaining: List[Node]
    previous_this: Optional["MsgValue"] = None
    pending: Optional[WaitRequest] = None


@dataclass
class TestCaseResult(object):
    """一个测试用例的聚合结果。"""

    name: str
    start_ms: float = 0.0
    end_ms: float = 0.0
    assertions: List["AssertionResult"] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(a.passed for a in self.assertions)

    @property
    def failures(self) -> List["AssertionResult"]:
        return [a for a in self.assertions if not a.passed]

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


@dataclass
class AssertionResult(object):
    """一条断言的记录。"""

    test_case: str
    kind: str
    passed: bool
    message: str = ""
    virtual_ms: float = 0.0
    line: int = 0


@dataclass
class ReportEntry(object):
    """测试报告里的一条记录（文本与 JUnit XML 共用）。"""

    name: str
    passed: bool
    virtual_ms: float = 0.0
    detail: str = ""


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class CaplEngine(object):
    """一个脚本引擎实例：解析 + 执行 + 统计。

    :param bus: 底层总线（多节点仲裁由它负责）。
    :param node_name: 本脚本在总线上使用的节点名。
    :param seed: 确定性伪随机种子。
    :param bitrate: 位速率（用于把帧长换算成虚拟毫秒）。
    """

    def __init__(
        self,
        bus: Optional[CanBus] = None,
        node_name: str = "SCRIPT",
        seed: int = 1,
        bitrate: int = 500000,
        output_stream: Optional[io.TextIOBase] = None,
    ) -> None:
        self.bus = bus if bus is not None else CanBus(bitrate=bitrate, min_gap_bits=0)
        self.node_name = node_name
        if node_name not in [n.name for n in self.bus.nodes]:
            self.bus.add_node(node_name)
        self.output_stream = output_stream if output_stream is not None else io.StringIO()
        self.seed = seed
        self._rng_state = (seed & 0x7FFFFFFF) or 1

        #: 调用栈
        self._frame_stack: List[CallFrame] = []
        #: 局部变量名 -> 声明类型（用于定宽整型截断）
        self._local_types: Dict[str, str] = {}
        #: 当前 ``this`` 报文（仅消息处理器内有效）
        self._current_this: Optional[MsgValue] = None

        #: 虚拟时钟（毫秒）
        self.now_ms: float = 0.0
        #: 已解析的脚本
        self.script: Optional[Script] = None
        #: 全局变量
        self.globals: Dict[str, object] = {}
        #: 全局作用域（变量声明也放在这里）
        self.messages: Dict[str, MsgValue] = {}
        self.timers: Dict[str, TimerValue] = {}
        self.functions: Dict[str, FunctionDecl] = {}
        self.handlers: List[EventHandler] = []
        #: 定时器 -> 处理器
        self._timer_handlers: Dict[str, EventHandler] = {}
        #: 周期定时器（setInterval 注册），到期自动重装
        self._intervals: Dict[str, float] = {}
        #: 报文处理器
        self._message_handlers: List[EventHandler] = []

        #: 待发送报文 (时间, 节点, MsgValue)
        self._pending_tx: List[Tuple[float, str, MsgValue]] = []
        #: 定时器到期队列
        self._timer_events: List[Tuple[float, str]] = []
        #: 阻塞等待
        self._waiters: List[Tuple[float, object, WaitRequest]] = []
        #: 记录收到的报文（供 testWaitForMessage 与统计）
        self.received: List[Frame] = []
        #: 输出报文（本脚本节点发出的）
        self.transmitted: List[Frame] = []
        #: 总线发送日志（总线上所有节点成功发出的帧，按时间顺序）
        self.tx_log: List[Frame] = []
        #: 已同步到 tx_log 的 bus.trace 下标（水位线）
        self._tx_watermark: int = len(self.bus.trace)
        #: 本次 output() 正在上线的帧对象（用于识别"自发帧"）
        self._self_tx_frames: List[Frame] = []

        # -- 统计 ---------------------------------------------------------
        self.event_count = 0
        self.timer_callback_count = 0
        self.message_event_count = 0
        self.start_count = 0
        self.stop_count = 0
        self.stats: Dict[str, int] = {
            "handlers_start": 0,
            "handlers_message": 0,
            "handlers_timer": 0,
            "handlers_key": 0,
            "handlers_stop": 0,
            "functions_called": 0,
        }
        #: 测试用例与断言
        self.test_cases: List[TestCaseResult] = []
        self.assertions: List[AssertionResult] = []
        self._current_case: Optional[TestCaseResult] = None
        self._case_counter = 0
        #: 脚本输出日志行
        self.log_lines: List[str] = []
        #: 运行期错误
        self.errors: List[str] = []
        #: 待派发的按键事件
        self._key_queue: List[str] = []
        self.finished = False
        self.max_virtual_ms = 0.0

    # -- 载入 -------------------------------------------------------------

    def load_source(self, source: str, filename: str = "<script>") -> None:
        """解析源码并准备执行环境。"""
        self.script = parse(source)
        self._prepare()

    def load_file(self, path: str) -> None:
        """解析脚本文件。"""
        with open(path, "r", encoding="utf-8") as handle:
            self.load_source(handle.read(), path)

    def _prepare(self) -> None:
        assert self.script is not None
        self.globals = {}
        self.messages = {}
        self.timers = {}
        self.functions = {f.name: f for f in self.script.functions}
        self.handlers = list(self.script.handlers)
        self._timer_handlers = {}
        self._message_handlers = []
        self._pending_tx = []
        self._timer_events = []
        self._waiters = []

        for decl in self.script.variables:
            if decl.message_id is not None or decl.is_timer:
                # 报文与定时器分别由 self.messages / self.timers 管理，
                # 不能同时塞进 globals，否则会遮蔽掉对象本身
                continue
            self.globals[decl.name] = self._default_value(decl)
        for decl in self.script.messages:
            self.messages[decl.name] = self._make_message(decl)
        for decl in self.script.timers:
            self.timers[decl.name] = TimerValue(name=decl.name)

        for handler in self.handlers:
            if handler.kind == "timer":
                if handler.timer_name not in self.timers:
                    # 处理器引用了未声明的定时器：自动补一个，方便写脚本
                    self.timers[handler.timer_name] = TimerValue(name=handler.timer_name)
                self._timer_handlers[handler.timer_name] = handler
            elif handler.kind == "message":
                self._message_handlers.append(handler)

    def _default_value(self, decl: VarDecl) -> object:
        """变量的初值（数组返回列表、定时器返回定时器对象）。"""
        if decl.is_timer:
            if decl.name in self.timers:
                return self.timers[decl.name]
            timer = TimerValue(name=decl.name)
            self.timers[decl.name] = timer
            return timer
        if decl.array_size is not None:
            size = self._const_int(decl.array_size)
            return [0] * size
        if decl.init is not None:
            literal = decl.init
            if isinstance(literal, IntLiteral):
                return wrap_integer(literal.value, decl.type_name)
            if isinstance(literal, CharLiteral):
                return ord(literal.value) & 0xFF
            if isinstance(literal, StringLiteral):
                return literal.value
        return 0

    def _const_int(self, node: Node) -> int:
        if isinstance(node, IntLiteral):
            return node.value
        raise CaplRuntimeError("array size must be an integer literal",
                               node.line, node.column)

    def _make_message(self, decl: VarDecl) -> MsgValue:
        can_id = 0
        if decl.message_id is not None and isinstance(decl.message_id, IntLiteral):
            can_id = decl.message_id.value
        return MsgValue(can_id=can_id, dlc=8, extended=decl.extended)

    # -- 作用域 -----------------------------------------------------------

    def lookup(self, name: str) -> object:
        """按作用域链查找变量。"""
        frame = self._frame_stack[-1] if self._frame_stack else None
        if frame is not None:
            for scope in reversed(frame.scopes):
                if name in scope:
                    return scope[name]
        if name in self.globals:
            return self.globals[name]
        if name in self.messages:
            return self.messages[name]
        if name in self.timers:
            return self.timers[name]
        raise CaplRuntimeError("undefined variable %r" % name)

    def assign(self, name: str, value: object) -> None:
        """给变量赋值。"""
        frame = self._frame_stack[-1] if self._frame_stack else None
        if frame is not None:
            for scope in reversed(frame.scopes):
                if name in scope:
                    scope[name] = self._coerce(self._declared_type(name), value)
                    return
        if name in self.globals:
            self.globals[name] = self._coerce(self._declared_type(name), value)
            return
        if name in self.messages:
            if isinstance(value, MsgValue):
                self.messages[name] = value
                return
            raise CaplRuntimeError("cannot assign %r to message %s" % (value, name))
        if name in self.timers:
            if isinstance(value, TimerValue):
                self.timers[name] = value
                return
            raise CaplRuntimeError("cannot assign %r to timer %s" % (value, name))
        # 未声明变量：直接报错，避免静默产生难以排查的行为
        raise CaplRuntimeError("undefined variable %r" % name)

    def _declared_type(self, name: str) -> str:
        """查变量的声明类型（局部变量也能查到）。"""
        if name in self._local_types:
            return self._local_types[name]
        for decl in (self.script.variables if self.script else []):
            if decl.name == name:
                return decl.type_name
        return "int"

    def _coerce(self, type_name: str, value: object) -> object:
        """按声明类型截断整数值（模拟 C 的定宽整型语义）。"""
        value = _unwrap_generator(value)
        if isinstance(value, (MsgValue, TimerValue, str, list)):
            return value
        return wrap_integer(int(value), type_name)

    # -- 生成器驱动 -------------------------------------------------------

    def _drive(self, gen: object) -> object:
        """同步驱动一个不阻塞的生成器；遇到 yield 返回 WaitRequest。"""
        try:
            value = next(gen)  # type: ignore[arg-type]
        except StopIteration as stop:
            return stop.value
        return value

    # -- 发送调度 ---------------------------------------------------------

    def schedule_send(self, msg: MsgValue, delay_ms: float = 0.0) -> None:
        """把报文排入发送队列（由调度器在对应时刻交给总线仲裁）。"""
        self._pending_tx.append((self.now_ms + max(0.0, delay_ms),
                                 self.node_name, msg))

    def _flush_transmissions(self) -> None:
        """把当前时刻之前排队的报文交给总线仲裁并发送。"""
        if not self._pending_tx:
            return
        due = [item for item in self._pending_tx if item[0] <= self.now_ms]
        if not due:
            return
        self._pending_tx = [item for item in self._pending_tx if item[0] > self.now_ms]
        requests = [(node, msg.to_frame()) for _t, node, msg in due]
        # 记录本次发出的帧对象：总线回调会对它们也触发一次，
        # 这一小段"在途"窗口内到达的同对象帧会被识别为本节点的自发帧
        self._self_tx_frames = [frame for _node, frame in requests]
        try:
            results = self.bus.contend(requests, max_retries=4)
        finally:
            # 先把总线上真正发出去的帧同步进发送日志，再清空"自发帧"标记
            self._sync_tx_log()
            self._self_tx_frames = []
        for index, result in enumerate(results):
            if result.sent:
                self.transmitted.append(result.frame)
                due[index][2].sent = True

    # -- 报文接收 ---------------------------------------------------------

    def _on_bus_frame(self, frame: Frame) -> None:
        """总线回调：记录接收、派发报文事件处理器、唤醒等待者。

        **顺序很重要**：先派发 ``on message`` 处理器，再唤醒 ``testWaitForMessage``
        的等待者。否则等待者会在一帧的观测点还没更新时就被恢复，脚本里
        "等到响应 -> 立刻断言响应内容" 就会读到上一帧的数据。
        """
        self.received.append(frame)
        self._sync_tx_log()
        for handler in self._message_handlers:
            if handler.wildcard or handler.message_id == frame.identifier:
                self.stats["handlers_message"] += 1
                self.message_event_count += 1
                self.event_count += 1
                self._run_handler(handler, this_frame=frame)
        self._wake_waiters(frame)

    def _is_self_transmission(self, frame: Frame) -> bool:
        """判断这一帧是否就是本节点此刻正在上线的帧。

        只用于 :meth:`_wake_waiters` 与 :meth:`_builtin_test_wait_for_message`
        的即时命中检查：等待"某 ID 的报文"不应该被自己刚发出去的同一 ID 帧满足。
        ``on message`` 事件处理器**不受**影响，仍然会收到本节点发出的帧
        （本框架允许一个脚本同时扮演被测 ECU 与测试仪，见 demo 02/03）。
        """
        for pending in self._self_tx_frames:
            if pending is frame:
                return True
        return False

    def _wake_waiters(self, frame: Frame) -> None:
        """把等待本帧的挂起处理器唤醒（本节点自发帧不唤醒）。"""
        if self._is_self_transmission(frame):
            return
        for entry in list(self._waiters):
            request = entry[2]
            if request.kind != "message" or request.matched_frame is not None:
                continue
            if request.wildcard or request.can_id == frame.identifier:
                request.matched_frame = frame
                self._waiters.remove(entry)
                self._resume_suspended(entry[3], frame)

    def _resume_suspended(self, suspended: SuspendedHandler,
                          value: object) -> None:
        """从挂起点恢复一个处理器，并继续执行它剩下的语句。"""
        self._frame_stack.append(suspended.frame)
        previous_this = self._current_this
        if suspended.previous_this is not None:
            self._current_this = suspended.previous_this
        try:
            try:
                yielded = suspended.gen.send(value)  # type: ignore[attr-defined]
            except StopIteration:
                yielded = None
            else:
                if isinstance(yielded, WaitRequest):
                    # 又挂起了：保存状态再入队
                    self._suspend(suspended, yielded)
                    return
            for index, statement in enumerate(suspended.remaining):
                gen = self.exec_statement(statement)
                yielded = self._drive(gen)
                if yielded is not None and isinstance(yielded, WaitRequest):
                    self._suspend(suspended, yielded,
                                  remaining=list(suspended.remaining[index + 1:]),
                                  gen=gen)
                    return
                self._flush_transmissions()
        except ReturnSignal:
            pass
        except CaplRuntimeError as error:
            self.errors.append(str(error))
        finally:
            if self._frame_stack and self._frame_stack[-1] is suspended.frame:
                self._frame_stack.pop()
            self._current_this = previous_this
        self._flush_transmissions()

    def _suspend(self, suspended: SuspendedHandler, request: WaitRequest,
                 remaining: Optional[List[Node]] = None,
                 gen: Optional[object] = None) -> None:
        """保存挂起状态并登记到等待队列。"""
        if remaining is not None:
            suspended.remaining = remaining
        if gen is not None:
            suspended.gen = gen
        suspended.pending = request
        self._waiters.append((request.deadline_ms, gen or suspended.gen, request,
                              suspended))

    def _handle_yield_and_continue(self, gen: object, value: object) -> bool:
        """继续推进一个生成器，直到它结束或抛出等待请求。

        :return: ``True`` 表示遇到了 :class:`WaitRequest`（调用方需要保存状态）。
        """
        while True:
            if isinstance(value, WaitRequest):
                return True
            try:
                value = next(gen)  # type: ignore[arg-type]
            except StopIteration:
                return False

    # -- 事件派发 ---------------------------------------------------------

    def _run_handler(self, handler: EventHandler, this_frame: Optional[Frame] = None,
                     timer: Optional[TimerValue] = None) -> None:
        """执行一个事件处理器。

        处理器内部的语句同步执行；一旦某条语句调用了会阻塞的测试原语
        （``testWaitForMessage`` / ``testWaitForTimeout``），整个处理器就挂起，
        **后续语句必须等到等待被满足后才继续**——这是 CAPL 测试脚本里
        "先发请求、再等响应、最后断言" 这种线性写法能够成立的前提。
        """
        frame = CallFrame(scopes=[{}], function="on %s" % handler.kind)
        previous_this = self._current_this
        if this_frame is not None:
            self._current_this = frame_to_message(this_frame)
        self._frame_stack.append(frame)
        suspended = SuspendedHandler(handler=handler, frame=frame, gen=None,
                                     remaining=[], previous_this=self._current_this)
        try:
            for index, statement in enumerate(handler.body.statements):
                gen = self.exec_statement(statement)
                value = self._drive(gen)
                if value is not None and isinstance(value, WaitRequest):
                    suspended.gen = gen
                    suspended.remaining = list(handler.body.statements[index + 1:])
                    self._suspend(suspended, value, gen=gen)
                    return
                # 该语句可能调用过 output()，立刻推上总线，让本轮事件里
                # 后续的 testWaitForMessage 能等到真实的总线应答
                self._flush_transmissions()
        except ReturnSignal:
            pass
        except CaplRuntimeError as error:
            self.errors.append(str(error))
        finally:
            if self._frame_stack and self._frame_stack[-1] is frame:
                self._frame_stack.pop()
            self._current_this = previous_this

    def _fire_timer(self, name: str) -> None:
        """触发定时器回调。"""
        timer = self.timers.get(name)
        handler = self._timer_handlers.get(name)
        if timer is not None:
            timer.fires += 1
            timer.running = False
        if handler is None:
            return
        self.stats["handlers_timer"] += 1
        self.timer_callback_count += 1
        self.event_count += 1
        self._run_handler(handler, timer=timer)
        # 周期定时器（setInterval）：回调执行完之后重新装上
        period = self._intervals.get(name)
        if period is not None and period > 0:
            if timer is not None:
                timer.running = True
                timer.period_ms = period
                timer.deadline_ms = self.now_ms + period
            self._timer_events.append((self.now_ms + period, name))

    def _dispatch_key(self, key: str) -> None:
        """派发按键事件。"""
        for handler in self.handlers:
            if handler.kind == "key" and handler.key == key:
                self.stats["handlers_key"] += 1
                self.event_count += 1
                self._run_handler(handler)

    # -- 主循环 -----------------------------------------------------------

    def next_deadline(self) -> Optional[float]:
        """返回下一个事件时刻（含定时器、挂起等待与待发送报文）。"""
        candidates: List[float] = []
        for deadline, _name in self._timer_events:
            candidates.append(deadline)
        for entry in self._waiters:
            candidates.append(entry[0])
        for time_ms, _node, _msg in self._pending_tx:
            candidates.append(time_ms)
        return min(candidates) if candidates else None

    def run(self, max_virtual_ms: float = 60000.0,
            wait_until_ms: Optional[float] = None) -> float:
        """跑完脚本，返回结束时的虚拟时间（毫秒）。

        :param max_virtual_ms: 虚拟时间上限，防止脚本里出现无限循环。
        :param wait_until_ms: 交错的 ``on timer`` 事件在等待期间同步派发时的
            最大跨越时间（默认 250 ms，避免长时间等待把性能拖垮）。
        """
        self.max_virtual_ms = max_virtual_ms
        # on start
        for handler in self.handlers:
            if handler.kind == "start":
                self.stats["handlers_start"] += 1
                self.start_count += 1
                self.event_count += 1
                self._run_handler(handler)
                self._flush_transmissions()

        guard = 0
        deadline_limit = guard_limit = 200000
        while not self.finished and guard < guard_limit:
            guard += 1
            target = self.next_deadline()
            if target is None:
                break
            if target > max_virtual_ms:
                self.now_ms = max_virtual_ms
                break
            self.now_ms = target
            self._advance_bus_to(self.now_ms)

            # 触发到期定时器
            due_timers = [name for deadline, name in self._timer_events
                          if deadline <= self.now_ms]
            self._timer_events = [(d, n) for d, n in self._timer_events
                                  if d > self.now_ms]
            for name in due_timers:
                self._fire_timer(name)

            # 完成阻塞等待（消息等待若已由总线回调解决则不在队列里）
            due_waiters = [entry for entry in self._waiters
                           if entry[0] <= self.now_ms]
            self._waiters = [entry for entry in self._waiters
                             if entry[0] > self.now_ms]
            for _deadline, _gen, request, suspended in due_waiters:
                # 超时之前做最后一次检查，避免应答与超时同刻到达时误判为超时
                if request.kind == "message" and request.matched_frame is None:
                    for frame in self.received:
                        if request.wildcard or frame.identifier == request.can_id:
                            request.matched_frame = frame
                            break
                if request.kind == "time":
                    self._resume_suspended(suspended, None)
                else:
                    self._resume_suspended(suspended, request.matched_frame)

            self._flush_transmissions()

            if due_timers or due_waiters:
                continue

        # on stopMeasurement
        for handler in self.handlers:
            if handler.kind == "stopMeasurement":
                self.stats["handlers_stop"] += 1
                self.stop_count += 1
                self.event_count += 1
                self._run_handler(handler)
        self.finished = True
        return self.now_ms

    def _advance_bus_to(self, target_ms: float) -> None:
        """把总线虚拟时间推进到 ``target_ms``（把这段时间计为空闲）。"""
        self.bus.advance_to_ms(target_ms)

    def run_steps(self, steps: int = 1) -> None:
        """推进有限步（调试用）。"""
        for _ in range(steps):
            target = self.next_deadline()
            if target is None:
                break
            self.now_ms = target
            self._advance_bus_to(self.now_ms)
            due = [name for deadline, name in self._timer_events if deadline <= self.now_ms]
            self._timer_events = [(d, n) for d, n in self._timer_events if d > self.now_ms]
            for name in due:
                self._fire_timer(name)
            due_waiters = [entry for entry in self._waiters if entry[0] <= self.now_ms]
            self._waiters = [entry for entry in self._waiters if entry[0] > self.now_ms]
            for _d, _gen, request, suspended in due_waiters:
                if request.kind == "time":
                    self._resume_suspended(suspended, None)
                else:
                    self._resume_suspended(suspended, request.matched_frame)
            self._flush_transmissions()

    # ------------------------------------------------------------------
    # 语句执行
    # ------------------------------------------------------------------

    def exec_statement(self, node: Node) -> object:
        """执行一条语句，返回**生成器**（可能 yield 等待请求）。"""
        if isinstance(node, VarDecl):
            return _as_generator(self._exec_declaration(node))
        if isinstance(node, Block):
            return _as_generator(self._exec_block(node))
        if isinstance(node, ExprStmt):
            return _as_generator(self._exec_expr_stmt(node))
        if isinstance(node, IfStmt):
            return _as_generator(self._exec_if(node))
        if isinstance(node, WhileStmt):
            return _as_generator(self._exec_while(node))
        if isinstance(node, DoWhileStmt):
            return _as_generator(self._exec_do_while(node))
        if isinstance(node, ForStmt):
            return _as_generator(self._exec_for(node))
        if isinstance(node, BreakStmt):
            return _as_generator(self._exec_break(node))
        if isinstance(node, ContinueStmt):
            return _as_generator(self._exec_continue(node))
        if isinstance(node, ReturnStmt):
            return _as_generator(self._exec_return(node))
        raise CaplRuntimeError("unsupported statement %s" % type(node).__name__,
                               node.line, node.column)

    def _exec_declaration(self, node: VarDecl) -> object:
        frame = self._frame_stack[-1]
        name = node.name
        self._local_types[name] = node.type_name
        if node.is_timer and name not in self.timers:
            self.timers[name] = TimerValue(name=name)
            frame.scopes[-1][name] = self.timers[name]
            return None
        value = self._default_value(node)
        frame.scopes[-1][name] = value
        if node.init is not None and not isinstance(node.init, IntLiteral):
            resolved = yield from self.eval(node.init)
            frame.scopes[-1][name] = self._coerce(node.type_name, resolved)
        return None
        return None
        yield None

    def _exec_block(self, node: Block) -> object:
        frame = self._frame_stack[-1]
        frame.scopes.append({})
        try:
            for statement in node.statements:
                yield from self.exec_statement(statement)
        finally:
            frame.scopes.pop()
        return None
        return None
        yield None

    def _exec_expr_stmt(self, node: ExprStmt) -> object:
        yield from self.eval(node.expr)
        return None
        return None
        yield None

    def _exec_if(self, node: IfStmt) -> object:
        condition = yield from self.eval(node.condition)
        if self.truthy(condition):
            yield from self.exec_statement(node.then_branch)
        elif node.else_branch is not None:
            yield from self.exec_statement(node.else_branch)
        return None
        return None
        yield None

    def _exec_while(self, node: WhileStmt) -> object:
        iterations = 0
        while True:
            iterations += 1
            if iterations > 1000000:
                raise CaplRuntimeError("while loop exceeded 1000000 iterations",
                                       node.line, node.column)
            condition = yield from self.eval(node.condition)
            if not self.truthy(condition):
                break
            try:
                yield from self.exec_statement(node.body)
            except BreakSignal:
                break
            except ContinueSignal:
                continue
        return None
        return None
        yield None

    def _exec_do_while(self, node: DoWhileStmt) -> object:
        iterations = 0
        while True:
            iterations += 1
            if iterations > 1000000:
                raise CaplRuntimeError("do-while exceeded 1000000 iterations",
                                       node.line, node.column)
            try:
                yield from self.exec_statement(node.body)
            except BreakSignal:
                break
            except ContinueSignal:
                pass
            condition = yield from self.eval(node.condition)
            if not self.truthy(condition):
                break
        return None
        return None
        yield None

    def _exec_for(self, node: ForStmt) -> object:
        self._frame_stack[-1].scopes.append({})
        try:
            if node.init is not None:
                if isinstance(node.init, (VarDecl, Block)):
                    yield from self.exec_statement(node.init)
                else:
                    yield from self.eval(node.init)
            iterations = 0
            while True:
                iterations += 1
                if iterations > 1000000:
                    raise CaplRuntimeError("for loop exceeded 1000000 iterations",
                                           node.line, node.column)
                if node.condition is not None:
                    condition = yield from self.eval(node.condition)
                    if not self.truthy(condition):
                        break
                try:
                    yield from self.exec_statement(node.body)
                except BreakSignal:
                    break
                except ContinueSignal:
                    pass
                if node.update is not None:
                    yield from self.eval(node.update)
        finally:
            self._frame_stack[-1].scopes.pop()
        return None
        return None
        yield None

    def _exec_break(self, node: BreakStmt) -> object:
        raise BreakSignal()
        yield  # pragma: no cover - 让函数成为生成器

    def _exec_continue(self, node: ContinueStmt) -> object:
        raise ContinueSignal()
        yield  # pragma: no cover

    def _exec_return(self, node: ReturnStmt) -> object:
        value = None
        if node.value is not None:
            value = yield from self.eval(node.value)
        raise ReturnSignal(value)
        yield  # pragma: no cover

    # ------------------------------------------------------------------
    # 表达式求值
    # ------------------------------------------------------------------

    def eval(self, node: Node) -> object:
        """求值一个表达式，返回**生成器**（可能 yield 等待请求）。

        生成的生成器通过 ``return`` 传出求值结果，因此调用方统一写
        ``value = yield from self.eval(node)``。
        """
        if isinstance(node, IntLiteral):
            return _as_generator(self._literal(node.value))
        if isinstance(node, StringLiteral):
            return _as_generator(self._literal_string(node.value))
        if isinstance(node, CharLiteral):
            return _as_generator(self._literal(ord(node.value) if node.value else 0))
        if isinstance(node, Identifier):
            return _as_generator(self._eval_identifier(node))
        if isinstance(node, ThisExpr):
            return _as_generator(self._eval_this(node))
        if isinstance(node, MemberAccess):
            return _as_generator(self._eval_member(node))
        if isinstance(node, IndexAccess):
            return _as_generator(self._eval_index(node))
        if isinstance(node, CallExpr):
            return _as_generator(self._eval_call(node))
        if isinstance(node, UnaryOp):
            return _as_generator(self._eval_unary(node))
        if isinstance(node, BinaryOp):
            return _as_generator(self._eval_binary(node))
        if isinstance(node, TernaryOp):
            return _as_generator(self._eval_ternary(node))
        if isinstance(node, AssignExpr):
            return _as_generator(self._eval_assign(node))
        if isinstance(node, IncDecExpr):
            return _as_generator(self._eval_incdec(node))
        raise CaplRuntimeError("unsupported expression %s" % type(node).__name__,
                               node.line, node.column)

    # -- 常量 / 字面量 ----------------------------------------------------

    #: 内置常量
    CONSTANTS: Dict[str, int] = {
        "DOMINANT": DOMINANT,
        "RECESSIVE": RECESSIVE,
        "TX": 0,
        "RX": 1,
        "TRUE": 1,
        "FALSE": 0,
        "PI": 3,
        "CAN": 1,
    }

    def _literal(self, value: int) -> object:
        return value

    def _literal_string(self, value: str) -> object:
        return value

    def _eval_identifier(self, node: Identifier) -> object:
        name = node.name
        if name in self.CONSTANTS:
            return self.CONSTANTS[name]
            yield self.CONSTANTS[name]
        try:
            return self.lookup(name)
            yield self.lookup(name)
        except CaplRuntimeError:
            if name in self.functions:
                raise CaplRuntimeError(
                    "function %r used as a value" % name, node.line, node.column)
            raise

    def _eval_this(self, node: ThisExpr) -> object:
        current = self._current_this
        if current is None:
            raise CaplRuntimeError("'this' is only valid inside a message handler",
                                   node.line, node.column)
        return current
        yield current

    # -- 成员 / 下标 ------------------------------------------------------

    def _eval_member(self, node: MemberAccess) -> object:
        obj = yield from self.eval(node.obj)
        return self.get_member(obj, node.member, node)
        return self.get_member(obj, node.member, node)
        yield self.get_member(obj, node.member, node)

    def get_member(self, obj: object, member: str, node: Optional[Node] = None) -> object:
        """读取成员（消息字段、定时器字段）。"""
        line = node.line if node else 0
        column = node.column if node else 0
        if isinstance(obj, MsgValue):
            if member == "id":
                return obj.can_id
            if member == "dlc":
                return obj.dlc
            if member == "dir":
                return 0 if obj.dir == "TX" else 1
            if member == "can":
                return obj.can
            if member == "extended":
                return 1 if obj.extended else 0
            if member == "fd":
                return 1 if obj.fd else 0
            if member == "brs":
                return 1 if obj.brs else 0
            if member == "length":
                return obj.length
            if member == "data":
                return obj.data
            raise CaplRuntimeError("message has no member %r" % member, line, column)
        if isinstance(obj, TimerValue):
            if member == "period":
                return int(obj.period_ms)
            if member == "running":
                return 1 if obj.running else 0
            if member == "count":
                return obj.fires
            raise CaplRuntimeError("timer has no member %r" % member, line, column)
        if isinstance(obj, str):
            raise CaplRuntimeError("cannot access member %r of a string" % member,
                                   line, column)
        raise CaplRuntimeError("value of type %s has no members"
                               % type(obj).__name__, line, column)

    def set_member(self, obj: object, member: str, value: object,
                   node: Optional[Node] = None) -> None:
        """写入成员。"""
        obj = _unwrap_generator(obj)
        value = _unwrap_generator(value)
        line = node.line if node else 0
        column = node.column if node else 0
        if isinstance(obj, MsgValue):
            if member == "id":
                obj.can_id = int(value)
                return
            if member == "dlc":
                obj.dlc = int(value)
                return
            if member == "dir":
                obj.dir = "TX" if int(value) == 0 else "RX"
                return
            if member == "can":
                obj.can = int(value)
                return
            if member == "extended":
                obj.extended = bool(value)
                return
            if member == "fd":
                obj.fd = bool(value)
                return
            if member == "brs":
                obj.brs = bool(value)
                return
            raise CaplRuntimeError("message has no member %r" % member, line, column)
        if isinstance(obj, TimerValue):
            if member == "period":
                obj.period_ms = float(value)
                return
            raise CaplRuntimeError("timer member %r is read-only" % member, line, column)
        raise CaplRuntimeError("value of type %s has no writable members"
                               % type(obj).__name__, line, column)

    def _eval_index(self, node: IndexAccess) -> object:
        obj = yield from self.eval(node.obj)
        index = yield from self.eval(node.index)
        index = int(index)
        if isinstance(obj, MsgValue):
            return obj.get_byte(index)
        if isinstance(obj, str):
            if index < 0 or index >= len(obj):
                raise CaplRuntimeError("string index %d out of range" % index,
                                       node.line, node.column)
            return ord(obj[index])
        if isinstance(obj, list):
            if index < 0 or index >= len(obj):
                raise CaplRuntimeError("array index %d out of range (size %d)"
                                       % (index, len(obj)), node.line, node.column)
            return obj[index]
        raise CaplRuntimeError("value is not indexable", node.line, node.column)

    def set_index(self, obj: object, index: int, value: object,
                  node: Optional[Node] = None) -> None:
        """写入下标。"""
        obj = _unwrap_generator(obj)
        value = _unwrap_generator(value)
        line = node.line if node else 0
        column = node.column if node else 0
        if isinstance(obj, MsgValue):
            obj.set_byte(index, int(value))
            return
        if isinstance(obj, list):
            if index < 0 or index >= len(obj):
                raise CaplRuntimeError("array index %d out of range (size %d)"
                                       % (index, len(obj)), line, column)
            obj[index] = int(value) & 0xFF
            return
        raise CaplRuntimeError("value is not index-assignable", line, column)

    # -- 运算 -------------------------------------------------------------

    def _eval_unary(self, node: UnaryOp) -> object:
        value = yield from self.eval(node.operand)
        if node.op == "-":
            return -self.to_int(value)
        if node.op == "+":
            return self.to_int(value)
        if node.op == "!":
            return 0 if self.truthy(value) else 1
        if node.op == "~":
            return ~self.to_int(value)
        raise CaplRuntimeError("unsupported unary operator %r" % node.op,
                               node.line, node.column)

    def _eval_binary(self, node: BinaryOp) -> object:
        """求值二元运算（含 ``&&`` / ``||`` 的短路语义）。"""
        op = node.op
        if op == ",":
            yield from self.eval(node.left)
            return (yield from self.eval(node.right))
            yield None
        left = yield from self.eval(node.left)
        # 短路求值
        if op == "&&":
            if not self.truthy(left):
                return 0
                yield 0
            right = yield from self.eval(node.right)
            return 1 if self.truthy(right) else 0
            yield 0
        if op == "||":
            if self.truthy(left):
                return 1
                yield 1
            right = yield from self.eval(node.right)
            return 1 if self.truthy(right) else 0
            yield 0
        right = yield from self.eval(node.right)
        return self.binary_op(op, left, right, node)
        yield None

    def binary_op(self, op: str, left: object, right: object,
                  node: Optional[Node] = None) -> object:
        """执行一次二元运算（也供测试直接调用）。"""
        line = node.line if node else 0
        column = node.column if node else 0
        if op == "+" and (isinstance(left, str) or isinstance(right, str)):
            return self.to_string(left) + self.to_string(right)
        if op in ("==", "!=") and (isinstance(left, str) or isinstance(right, str)):
            equal = self.to_string(left) == self.to_string(right)
            return (1 if equal else 0) if op == "==" else (0 if equal else 1)
        a = self.to_int(left)
        b = self.to_int(right)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:
                raise CaplRuntimeError("division by zero", line, column)
            # C 语义的整数除法（向零截断）
            quotient = abs(a) // abs(b)
            return quotient if (a >= 0) == (b >= 0) else -quotient
        if op == "%":
            if b == 0:
                raise CaplRuntimeError("modulo by zero", line, column)
            remainder = abs(a) % abs(b)
            return remainder if a >= 0 else -remainder
        if op == "&":
            return a & b
        if op == "|":
            return a | b
        if op == "^":
            return a ^ b
        if op == "<<":
            return a << (b & 63)
        if op == ">>":
            return a >> (b & 63)
        if op == "==":
            return 1 if a == b else 0
        if op == "!=":
            return 1 if a != b else 0
        if op == "<":
            return 1 if a < b else 0
        if op == ">":
            return 1 if a > b else 0
        if op == "<=":
            return 1 if a <= b else 0
        if op == ">=":
            return 1 if a >= b else 0
        raise CaplRuntimeError("unsupported binary operator %r" % op, line, column)

    def _eval_ternary(self, node: TernaryOp) -> object:
        condition = yield from self.eval(node.condition)
        if self.truthy(condition):
            return (yield from self.eval(node.if_true))
        return (yield from self.eval(node.if_false))

    def _eval_assign(self, node: AssignExpr) -> object:
        target = node.target
        if node.op == "=":
            if isinstance(target, CallExpr) and target.receiver is not None:
                # CAPL 习惯写法：``msg.byte(0) = value``
                # 解释为对接收者调用 setByte / 对应的写方法
                receiver = yield from self.eval(target.receiver)
                args = []
                for arg in target.args:
                    args.append((yield from self.eval(arg)))
                value = yield from self.eval(node.value)
                setter = {"byte": "setByte"}.get(target.callee,
                                                 "set" + target.callee[:1].upper()
                                                 + target.callee[1:])
                yield from _as_generator(self.call_method(receiver, setter,
                                                         args + [value], target))
                return value
            if isinstance(target, Identifier):
                value = yield from self.eval(node.value)
                self.assign(target.name, value)
                return value
            if isinstance(target, MemberAccess):
                obj = yield from self.eval(target.obj)
                value = yield from self.eval(node.value)
                self.set_member(obj, target.member, value, target)
                return value
            if isinstance(target, IndexAccess):
                obj = yield from self.eval(target.obj)
                index = yield from self.eval(target.index)
                value = yield from self.eval(node.value)
                self.set_index(obj, int(index), value, target)
                return value
            raise CaplRuntimeError("invalid assignment target", node.line,
                                   node.column)
        # 复合赋值
        current = yield from self.read_target(target)
        operand = yield from self.eval(node.value)
        result = self.binary_op(node.op[:-1], current, operand, node)
        yield from self.write_target(target, result)
        return result
        return result
        yield result

    def read_target(self, target: Node) -> object:
        if isinstance(target, Identifier):
            return self.lookup(target.name)
        if isinstance(target, MemberAccess):
            obj = yield from self.eval(target.obj)
            return self.get_member(obj, target.member, target)
        if isinstance(target, IndexAccess):
            obj = yield from self.eval(target.obj)
            index = yield from self.eval(target.index)
            if isinstance(obj, MsgValue):
                return obj.get_byte(int(index))
            if isinstance(obj, list):
                return obj[int(index)]
            raise CaplRuntimeError("value is not indexable", target.line,
                                   target.column)
        raise CaplRuntimeError("invalid assignment target", target.line,
                               target.column)

    def write_target(self, target: Node, value: object) -> object:
        if isinstance(target, Identifier):
            self.assign(target.name, value)
            return value
        if isinstance(target, MemberAccess):
            obj = yield from self.eval(target.obj)
            self.set_member(obj, target.member, value, target)
            return value
        if isinstance(target, IndexAccess):
            obj = yield from self.eval(target.obj)
            index = yield from self.eval(target.index)
            self.set_index(obj, int(index), value, target)
            return value
        raise CaplRuntimeError("invalid assignment target", target.line,
                               target.column)

    def _eval_incdec(self, node: IncDecExpr) -> object:
        current = yield from self.read_target(node.target)
        if isinstance(current, str) or isinstance(current, MsgValue):
            raise CaplRuntimeError("%s requires a numeric target" % node.op,
                                   node.line, node.column)
        before = self.to_int(current)
        after = before + 1 if node.op == "++" else before - 1
        yield from self.write_target(node.target, after)
        return after if node.prefix else before
        return after if node.prefix else before
        yield after if node.prefix else before

    # -- 函数调用 ---------------------------------------------------------

    def _eval_call(self, node: CallExpr) -> object:
        name = node.callee
        # 方法调用形式：msg.byte(i) 等
        if node.receiver is not None:
            receiver = yield from self.eval(node.receiver)
            args = []
            for arg in node.args:
                args.append((yield from self.eval(arg)))
            return _as_generator(self.call_method(receiver, name, args, node))
        if name in self.functions:
            args = []
            for arg in node.args:
                args.append((yield from self.eval(arg)))
            return (yield from self.call_function(name, args, node))
        handler = self.BUILTINS.get(name)
        if handler is None:
            raise CaplRuntimeError("unknown function %r" % name, node.line,
                                   node.column)
        args = []
        for arg in node.args:
            args.append((yield from self.eval(arg)))
        return (yield from _as_generator(handler(self, args, node)))

    def call_method(self, receiver: object, name: str, args: List[object],
                    node: Optional[Node] = None) -> object:
        """调用对象方法（目前只有 message / 字符串上的方法），返回生成器。

        这里刻意包一层 ``_value_generator``：解释器约定 ``_eval_call`` 可以对
        本函数的返回值直接 ``yield from``，所以返回值必须是生成器。
        """
        def gen() -> Iterable[object]:
            line = node.line if node else 0
            column = node.column if node else 0
            if isinstance(receiver, MsgValue):
                if name == "byte":
                    index = self.to_int(args[0]) if args else 0
                    return receiver.get_byte(index)
                    yield None
                if name == "setByte":
                    receiver.set_byte(self.to_int(args[0]), self.to_int(args[1]))
                    return 1
                    yield 1
                if name == "id":
                    return receiver.can_id
                    yield 0
                raise CaplRuntimeError("message has no method %r" % name, line, column)
            if isinstance(receiver, str):
                if name == "length":
                    return len(receiver)
                    yield 0
                if name == "at":
                    index = self.to_int(args[0]) if args else 0
                    return ord(receiver[index])
                    yield 0
                raise CaplRuntimeError("string has no method %r" % name, line, column)
            raise CaplRuntimeError(
                "value of type %s has no method %r" % (type(receiver).__name__, name),
                line, column)
        return gen()

    def call_function(self, name: str, args: List[object],
                      node: Optional[Node] = None) -> object:
        """调用用户自定义函数，返回生成器。"""
        decl = self.functions[name]
        if len(args) != len(decl.params):
            raise CaplRuntimeError(
                "function %s expects %d argument(s), got %d"
                % (name, len(decl.params), len(args)),
                node.line if node else 0, node.column if node else 0)
        scope: Dict[str, object] = {}
        for param, value in zip(decl.params, args):
            scope[param.name] = self._coerce(param.type_name, value)
        frame = CallFrame(scopes=[scope], function=name)
        self.stats["functions_called"] += 1

        def gen() -> Iterable[object]:
            self._frame_stack.append(frame)
            try:
                yield from self.exec_statement(decl.body)
            except ReturnSignal as signal:
                return signal.value
                return signal.value
                yield signal.value
                yield signal.value
            finally:
                if self._frame_stack and self._frame_stack[-1] is frame:
                    self._frame_stack.pop()
            return 0
            return 0
            yield 0
            yield 0
        return gen()

    # ------------------------------------------------------------------
    # 内建函数
    # ------------------------------------------------------------------

    @staticmethod
    def to_int(value: object) -> int:
        """把值转成整数（消息对象返回其 ID，字符串按 CAPL 习惯取字符和）。"""
        value = _unwrap_generator(value)
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            total = 0
            for char in value:
                total = (total << 8) | ord(char)
            return total
        if isinstance(value, MsgValue):
            return value.can_id
        if isinstance(value, TimerValue):
            return int(value.period_ms)
        if value is None:
            return 0
        raise CaplRuntimeError("cannot convert %r to an integer" % (value,))

    @staticmethod
    def to_string(value: object) -> str:
        """把值转成字符串。"""
        value = _unwrap_generator(value)
        if isinstance(value, str):
            return value
        if isinstance(value, MsgValue):
            return "msg(0x%X, dlc=%d, %s)" % (value.can_id, value.dlc, value.hex())
        if isinstance(value, TimerValue):
            return "timer(%s)" % value.name
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            return str(value)
        return str(value)

    @staticmethod
    def truthy(value: object) -> bool:
        """CAPL 的真值规则：非零即为真。"""
        value = _unwrap_generator(value)
        if isinstance(value, (MsgValue, TimerValue)):
            return True
        return _safe_int(value) != 0

    # -- 内建实现 ---------------------------------------------------------

    def _builtin_output(self, args: List[object], node: Node) -> object:
        if not args:
            raise CaplRuntimeError("output() requires a message argument",
                                   node.line, node.column)
        msg = args[0]
        if not isinstance(msg, MsgValue):
            raise CaplRuntimeError("output() expects a message, got %s"
                                   % type(msg).__name__, node.line, node.column)
        delay = 0.0
        if len(args) > 1:
            delay = float(self.to_int(args[1]))
        self.schedule_send(msg, delay)
        # 立刻推上总线。CAPL 的 output() 语义就是马上发；而且脚本里常见的
        # 「先 output(req) 再 testWaitForMessage(resp)」也依赖这一点。
        self._flush_transmissions()
        return 1

    def _builtin_write(self, args: List[object], node: Node) -> object:
        text = "".join(self.to_string(a) for a in args)
        self.output_stream.write(text)
        self.log_lines.append(text)
        return 0

    def _builtin_write_line(self, args: List[object], node: Node) -> object:
        text = "".join(self.to_string(a) for a in args)
        self.output_stream.write(text + "\n")
        self.log_lines.append(text)
        return 0

    def _builtin_set_timer(self, args: List[object], node: Node) -> object:
        if len(args) < 2:
            raise CaplRuntimeError("setTimer(timer, ms) requires 2 arguments",
                                   node.line, node.column)
        timer = args[0]
        if not isinstance(timer, TimerValue):
            raise CaplRuntimeError("setTimer() expects a timer as first argument",
                                   node.line, node.column)
        period = float(self.to_int(args[1]))
        timer.period_ms = period
        timer.running = True
        timer.deadline_ms = self.now_ms + period
        self._timer_events.append((timer.deadline_ms, timer.name))
        return 1

    def _builtin_cancel_timer(self, args: List[object], node: Node) -> object:
        if not args or not isinstance(args[0], TimerValue):
            raise CaplRuntimeError("cancelTimer() expects a timer argument",
                                   node.line, node.column)
        timer = args[0]
        timer.running = False
        self._timer_events = [(d, n) for d, n in self._timer_events
                              if n != timer.name]
        return 1

    def _builtin_is_timer_active(self, args: List[object], node: Node) -> object:
        if not args or not isinstance(args[0], TimerValue):
            return 0
        return 1 if args[0].running else 0

    def _builtin_get_timer(self, args: List[object], node: Node) -> object:
        if not args or not isinstance(args[0], TimerValue):
            return 0
        timer = args[0]
        if not timer.running:
            return 0
        return int(max(0.0, timer.deadline_ms - self.now_ms))

    # -- 消息字段访问 -----------------------------------------------------

    def _builtin_msg_byte(self, args: List[object], node: Node) -> object:
        if not args:
            raise CaplRuntimeError("msg.byte(index) requires an index",
                                   node.line, node.column)
        raise CaplRuntimeError("msg.byte() must be called on a message", 0, 0)

    # -- 测试原语 ---------------------------------------------------------

    def _builtin_test_wait_for_message(self, args: List[object], node: Node) -> object:
        """``testWaitForMessage(id, timeoutMs)``：等待报文，0 = 等到，非 0 = 超时。

        两个细节，都是为了让"先 output 请求、再等应答"这种线性脚本能稳定工作：

        * 注册等待之前先检查**本次等待开始之后**已经到达的报文。由于 ECU 的应答
          可能就在请求帧的传输过程里被同步产生（本仿真器里接收和发送是同步的），
          先发后等的脚本可能"错过"那次唤醒。
        * 超时后做最后一次检查再判定失败，避免应答与超时同刻到达时误判。
        """
        if not args:
            raise CaplRuntimeError("testWaitForMessage(id, timeout) requires an id",
                                   node.line, node.column)
        can_id = self.to_int(args[0])
        timeout = float(self.to_int(args[1])) if len(args) > 1 else 1000.0
        wildcard = can_id < 0
        request = WaitRequest(
            kind="message",
            can_id=None if wildcard else can_id,
            timeout_ms=timeout,
            deadline_ms=self.now_ms + timeout,
            wildcard=wildcard,
        )
        # 即时命中检查：先把"发送本请求期间已经到达的应答"找出来。
        # 本仿真器的发送与接收是同步的（一帧刚送上总线，接收方的事件处理器
        # 就会执行），所以应答可能在 output(req) 返回之前就已经到达，必须补一次
        # 检查，否则「先发请求、再等应答」的线性脚本会漏掉这次唤醒。
        for frame in self.received:
            if self._is_self_transmission(frame):
                continue
            if wildcard or frame.identifier == request.can_id:
                request.matched_frame = frame
                break
        frame = yield request
        if frame is None:
            # 超时：返回 1（非 0）
            value = 1
        else:
            value = 0
        return value
        yield value

    def _builtin_test_wait_for_timeout(self, args: List[object], node: Node) -> object:
        duration = float(self.to_int(args[0])) if args else 0.0
        if duration <= 0:
            return 0
        request = WaitRequest(kind="time", timeout_ms=duration,
                              deadline_ms=self.now_ms + duration)
        yield request
        return 0
        yield 0

    def _current_case_name(self) -> str:
        if self._current_case is not None:
            return self._current_case.name
        return "(no test case)"

    def _record_assertion(self, kind: str, passed: bool, message: str,
                          node: Optional[Node]) -> None:
        result = AssertionResult(
            test_case=self._current_case_name(),
            kind=kind,
            passed=passed,
            message=message,
            virtual_ms=self.now_ms,
            line=node.line if node else 0,
        )
        self.assertions.append(result)
        if self._current_case is not None:
            self._current_case.assertions.append(result)

    def _builtin_test_assert_true(self, args: List[object], node: Node) -> object:
        value = args[0] if args else 0
        text = self.to_string(args[1]) if len(args) > 1 else ""
        passed = self.truthy(value)
        self._record_assertion("assertTrue", passed,
                               text or "value=%s" % self.to_string(value), node)
        return 0 if passed else 1

    def _builtin_test_assert_false(self, args: List[object], node: Node) -> object:
        value = args[0] if args else 0
        text = self.to_string(args[1]) if len(args) > 1 else ""
        passed = not self.truthy(value)
        self._record_assertion("assertFalse", passed,
                               text or "value=%s" % self.to_string(value), node)
        return 0 if passed else 1

    def _builtin_test_assert_equals(self, args: List[object], node: Node) -> object:
        if len(args) < 2:
            raise CaplRuntimeError("testAssertEquals(actual, expected) needs 2 args",
                                   node.line, node.column)
        actual, expected = args[0], args[1]
        text = self.to_string(args[2]) if len(args) > 2 else ""
        passed = self.to_string(actual) == self.to_string(expected)
        detail = text or "actual=%s expected=%s" % (self.to_string(actual),
                                                    self.to_string(expected))
        self._record_assertion("assertEquals", passed, detail, node)
        return 0 if passed else 1

    def _builtin_test_assert_not_equals(self, args: List[object], node: Node) -> object:
        actual, expected = args[0], args[1]
        text = self.to_string(args[2]) if len(args) > 2 else ""
        passed = self.to_string(actual) != self.to_string(expected)
        detail = text or "actual=%s forbidden=%s" % (self.to_string(actual),
                                                      self.to_string(expected))
        self._record_assertion("assertNotEquals", passed, detail, node)
        return 0 if passed else 1

    def _builtin_test_assert_in_range(self, args: List[object], node: Node) -> object:
        if len(args) < 3:
            raise CaplRuntimeError(
                "testAssertInRange(value, low, high) needs 3 args",
                node.line, node.column)
        value = self.to_int(args[0])
        low = self.to_int(args[1])
        high = self.to_int(args[2])
        text = self.to_string(args[3]) if len(args) > 3 else ""
        passed = low <= value <= high
        detail = text or "value=%d range=[%d, %d]" % (value, low, high)
        self._record_assertion("assertInRange", passed, detail, node)
        return 0 if passed else 1

    def _builtin_test_case_begin(self, args: List[object], node: Node) -> object:
        name = self.to_string(args[0]) if args else "case%d" % self._case_counter
        self._case_counter += 1
        if self._current_case is not None:
            self._current_case.end_ms = self.now_ms
        self._current_case = TestCaseResult(name=name, start_ms=self.now_ms)
        self.test_cases.append(self._current_case)
        return 1

    def _builtin_test_case_end(self, args: List[object], node: Node) -> object:
        if self._current_case is not None:
            self._current_case.end_ms = self.now_ms
            self._current_case = None
        return 1

    def _builtin_test_add_comment(self, args: List[object], node: Node) -> object:
        if args:
            self.log_lines.append("# " + self.to_string(args[0]))
        return 0

    # -- 工具函数 ---------------------------------------------------------

    def _builtin_abs(self, args: List[object], node: Node) -> object:
        return abs(self.to_int(args[0])) if args else 0

    def _builtin_min(self, args: List[object], node: Node) -> object:
        if not args:
            return 0
        return min(self.to_int(a) for a in args)

    def _builtin_max(self, args: List[object], node: Node) -> object:
        if not args:
            return 0
        return max(self.to_int(a) for a in args)

    def _builtin_get_tick_count(self, args: List[object], node: Node) -> object:
        return int(self.now_ms)

    def _builtin_random(self, args: List[object], node: Node) -> object:
        limit = self.to_int(args[0]) if args else 0x7FFF
        return self.next_random() % max(1, limit)

    def _builtin_jitter_ms(self, args: List[object], node: Node) -> object:
        """``jitterMs(seq, amplitude)``：确定性伪随机抖动，单位 0.1 ms。

        用序号作为种子的一部分，保证同一脚本重复运行得到完全相同的数值。
        """
        def gen() -> Iterable[object]:
            seq = self.to_int(args[0]) if args else 0
            amplitude = self.to_int(args[1]) if len(args) > 1 else 5
            state = (self.seed * 2654435761 + seq * 40503 + 12345) & 0xFFFFFFFF
            state = (state ^ (state >> 13)) * 1274126177 & 0xFFFFFFFF
            value = (state >> 8) % (2 * amplitude + 1) - amplitude
            return int(value)
        return gen()

    def next_random(self) -> int:
        """确定性线性同余随机数（Numerical Recipes 参数）。"""
        self._rng_state = (self._rng_state * 1664525 + 1013904223) & 0xFFFFFFFF
        return self._rng_state >> 16

    def _builtin_get_signal_value(self, args: List[object], node: Node) -> object:
        """``getSignalValue(message, startBit, bitLength)``：从报文字节里取位段。

        这是**极简的信号映射**：不做 DBC 解析，只按给定起始位与长度从数据场
        提取 Little-Endian 位段。
        """
        def gen() -> Iterable[object]:
            if len(args) < 3:
                raise CaplRuntimeError(
                    "getSignalValue(msg, startBit, bitLength) needs 3 args",
                    node.line, node.column)
            msg = args[0]
            if not isinstance(msg, MsgValue):
                raise CaplRuntimeError("getSignalValue expects a message",
                                       node.line, node.column)
            start = self.to_int(args[1])
            bits = self.to_int(args[2])
            return extract_signal(msg.data, start, bits)
        return gen()

    def _builtin_set_signal_value(self, args: List[object], node: Node) -> object:
        if len(args) < 4:
            raise CaplRuntimeError(
                "setSignalValue(msg, startBit, bitLength, value) needs 4 args",
                node.line, node.column)
        msg = args[0]
        if not isinstance(msg, MsgValue):
            raise CaplRuntimeError("setSignalValue expects a message",
                                   node.line, node.column)
        start = self.to_int(args[1])
        bits = self.to_int(args[2])
        value = self.to_int(args[3])
        for index, byte in enumerate(insert_signal(msg.data, start, bits, value)):
            msg.set_byte(index, byte)
        return 1

    def _builtin_msg_byte_method(self, args: List[object], node: Node) -> object:
        return 0

    #: 内建函数表（在类定义之后填充，见文件末尾）
    BUILTINS: Dict[str, Callable[["CaplEngine", List[object], Node], object]] = {}


    # ------------------------------------------------------------------
    # 总线实验内建函数（多节点同时发送 + 查询仿真结果）
    # ------------------------------------------------------------------

    def _sync_tx_log(self) -> None:
        """把 :attr:`canbus.CanBus.trace` 中新增的帧同步到脚本可见的发送日志。

        这样"总线上一共发了几帧""第 i 帧是什么 ID"这类查询就不依赖调用方是否
        挂了 ``bus.on_frame`` 回调，纯粹的引擎测试也能拿到完整日志。
        """
        trace = self.bus.trace
        if len(trace) > self._tx_watermark:
            self.tx_log.extend(trace[self._tx_watermark:])
            self._tx_watermark = len(trace)

    def _ensure_node(self, name: str) -> str:
        """确保总线上存在该节点名，返回规范化后的名字。"""
        if name not in [node.name for node in self.bus.nodes]:
            self.bus.add_node(name)
        return name

    def _make_frame(self, can_id: int, byte0: int = 0, remote: bool = False,
                    dlc: int = 8, extended: bool = False) -> Frame:
        """按参数构造一帧。"""
        if remote:
            return Frame.remote_frame(can_id, dlc, extended=extended)
        data = bytes([byte0 & 0xFF] + [0] * (max(1, dlc) - 1))
        return Frame(identifier=can_id, data=data, extended=extended)

    def _builtin_bus_send2(self, args: List[object], node: Node) -> object:
        """``busSend2(nameA, idA, b0A, nameB, idB, b0B)``：两节点同一时刻开始发送。

        :return: 本次实验中总线成功发送的帧数。
        """
        if len(args) < 6:
            raise CaplRuntimeError("busSend2 需要 6 个参数", node.line, node.column)
        name_a = self._ensure_node(str(args[0]))
        name_b = self._ensure_node(str(args[3]))
        id_a = self.to_int(args[1])
        id_b = self.to_int(args[4])
        frm_a = self._make_frame(id_a, self.to_int(args[2]))
        frm_b = self._make_frame(id_b, self.to_int(args[5]))
        before = len(self.tx_log)
        self.bus.contend([(name_a, frm_a), (name_b, frm_b)], max_retries=4)
        self._sync_tx_log()
        value = len(self.tx_log) - before
        return value
        yield value

    def _builtin_bus_send3(self, args: List[object], node: Node) -> object:
        """``busSend3(nA,idA,bA, nB,idB,bB, nC,idC,bC)``：三节点同时开始发送。"""
        if len(args) < 9:
            raise CaplRuntimeError("busSend3 需要 9 个参数", node.line, node.column)
        requests = []
        for index in range(3):
            base = index * 3
            name = self._ensure_node(str(args[base]))
            can_id = self.to_int(args[base + 1])
            requests.append((name, self._make_frame(can_id, self.to_int(args[base + 2]))))
        before = len(self.tx_log)
        self.bus.contend(requests, max_retries=4)
        self._sync_tx_log()
        value = len(self.tx_log) - before
        return value
        yield value

    def _builtin_bus_send_data_rtr(self, args: List[object], node: Node) -> object:
        """``busSendDataRtr(dName, dId, dB0, rName, rId, rDlc)``：数据帧与远程帧同时发送。"""
        if len(args) < 6:
            raise CaplRuntimeError("busSendDataRtr 需要 6 个参数", node.line, node.column)
        data_name = self._ensure_node(str(args[0]))
        rtr_name = self._ensure_node(str(args[3]))
        data_frame = self._make_frame(self.to_int(args[1]), self.to_int(args[2]))
        rtr_frame = self._make_frame(self.to_int(args[4]), 0, remote=True,
                                     dlc=self.to_int(args[5]))
        before = len(self.tx_log)
        self.bus.contend([(data_name, data_frame), (rtr_name, rtr_frame)],
                         max_retries=4)
        self._sync_tx_log()
        value = len(self.tx_log) - before
        return value
        yield value

    def _builtin_arbitration_count(self, args: List[object], node: Node) -> object:
        """``arbitrationCount()``：已记录的仲裁次数。"""
        return len(self.bus.arbitrations)
        yield len(self.bus.arbitrations)

    def _arbitration(self, index: int) -> ArbitrationRecord:
        records = self.bus.arbitrations
        if index < 0 or index >= len(records):
            raise CaplRuntimeError("arbitration 记录下标 %d 越界（共 %d 条）"
                                   % (index, len(records)))
        return records[index]

    def _builtin_arbitration_winner_id(self, args: List[object], node: Node) -> object:
        """``arbitrationWinnerId(i)``：第 i 次仲裁的赢家 ID。"""
        index = self.to_int(args[0]) if args else 0
        return self._arbitration(index).winner_id
        yield self._arbitration(index).winner_id

    def _builtin_arbitration_loser_count(self, args: List[object], node: Node) -> object:
        """``arbitrationLoserCount(i)``：第 i 次仲裁中失败的节点数。"""
        index = self.to_int(args[0]) if args else 0
        return len(self._arbitration(index).losers)
        yield len(self._arbitration(index).losers)

    def _builtin_arbitration_loser_bit(self, args: List[object], node: Node) -> object:
        """``arbitrationLoserBit(i, k)``：第 i 次仲裁里第 k 个失败者退出时的位序号。"""
        index = self.to_int(args[0]) if args else 0
        which = self.to_int(args[1]) if len(args) > 1 else 0
        losers = self._arbitration(index).losers
        if which < 0 or which >= len(losers):
            raise CaplRuntimeError("loser 下标 %d 越界" % which)
        return losers[which][1]
        yield losers[which][1]

    def _builtin_arbitration_loser_name(self, args: List[object], node: Node) -> object:
        """``arbitrationLoserName(i, k)``：失败节点的名字。"""
        index = self.to_int(args[0]) if args else 0
        which = self.to_int(args[1]) if len(args) > 1 else 0
        losers = self._arbitration(index).losers
        if which < 0 or which >= len(losers):
            raise CaplRuntimeError("loser 下标 %d 越界" % which)
        return losers[which][0]
        yield losers[which][0]

    def _builtin_arbitration_loser_id(self, args: List[object], node: Node) -> object:
        """``arbitrationLoserId(i, k)``：失败节点当时想发的报文 ID。"""
        index = self.to_int(args[0]) if args else 0
        which = self.to_int(args[1]) if len(args) > 1 else 0
        record = self._arbitration(index)
        losers = record.losers
        if which < 0 or which >= len(losers):
            raise CaplRuntimeError("loser 下标 %d 越界" % which)
        name = losers[which][0]
        # identifiers 与 participants 一一对应
        if name in record.participants and len(record.identifiers) == len(record.participants):
            return record.identifiers[record.participants.index(name)]
            yield record.identifiers[record.participants.index(name)]
        return 0
        yield 0

    def _builtin_arbitration_loser_retries(self, args: List[object], node: Node) -> object:
        """``arbitrationLoserRetries(i, k)``：失败节点最终的自动重发次数。"""
        index = self.to_int(args[0]) if args else 0
        which = self.to_int(args[1]) if len(args) > 1 else 0
        record = self._arbitration(index)
        losers = record.losers
        if which < 0 or which >= len(losers):
            raise CaplRuntimeError("loser 下标 %d 越界" % which)
        name = losers[which][0]
        try:
            bus_node = self.bus.node(name)
        except KeyError:
            return 0
            yield 0
        return bus_node.retransmissions
        yield bus_node.retransmissions

    def _builtin_total_frames_sent(self, args: List[object], node: Node) -> object:
        """``totalFramesSent()``：总线上成功发送过的帧数（含其它节点）。"""
        self._sync_tx_log()
        value = len(self.tx_log)
        return value
        yield value

    def _builtin_frame_id_at(self, args: List[object], node: Node) -> object:
        """``frameIdAt(i)``：总线发送日志里第 i 帧的 ID。"""
        self._sync_tx_log()
        index = self.to_int(args[0]) if args else 0
        if index < 0 or index >= len(self.tx_log):
            raise CaplRuntimeError("帧日志下标 %d 越界（共 %d 条）"
                                   % (index, len(self.tx_log)))
        value = self.tx_log[index].identifier
        return value
        yield value

    def _builtin_frame_source_at(self, args: List[object], node: Node) -> object:
        """``frameSourceAt(i)``：总线发送日志里第 i 帧的发送节点名（字符串）。"""
        self._sync_tx_log()
        index = self.to_int(args[0]) if args else 0
        if index < 0 or index >= len(self.tx_log):
            raise CaplRuntimeError("帧日志下标 %d 越界" % index)
        value = self.tx_log[index].source
        return value
        yield value

    def _builtin_bus_bitrate(self, args: List[object], node: Node) -> object:
        """``busBitrateKbps()``：总线位速率，单位 **kbit/s**。

        脚本里的 ``int`` 是 16 位有符号，直接返回 500000 bit/s 会被截断成负数，
        因此这里刻意用 kbit/s（车载 CAN 常见 500 kbit/s、250 kbit/s）。
        """
        value = int(round(self.bus.bitrate / 1000.0))
        return value
        yield value

    def _builtin_bus_load_x100(self, args: List[object], node: Node) -> object:
        """``busLoadX100()``：总线负载率 x 100（整数，避免浮点）。"""
        return int(round(self.bus.stats.load_percent * 100))
        yield int(round(self.bus.stats.load_percent * 100))

    def _builtin_bus_time_ms(self, args: List[object], node: Node) -> object:
        """``busTimeMs()``：总线虚拟时间（毫秒，取整）。"""
        return int(self.bus.now_ms)
        yield int(self.bus.now_ms)


    # ------------------------------------------------------------------
    # 错误注入与错误状态机内建函数
    # ------------------------------------------------------------------

    def _ensure_node_obj(self, name: str):
        """取得（必要时创建）总线节点对象。"""
        if name not in [node.name for node in self.bus.nodes]:
            return self.bus.add_node(name)
        return self.bus.node(name)

    def _builtin_inject_bit_error(self, args: List[object], node: Node) -> object:
        """``injectBitError(nodeName, atBit)``：让该节点的下一次发送发生位错误。"""
        if not args:
            raise CaplRuntimeError("injectBitError 需要节点名", node.line, node.column)
        name = self._ensure_node(str(args[0]))
        at_bit = self.to_int(args[1]) if len(args) > 1 else 20
        self.bus.inject_bit_error(name, at_bit=at_bit)
        return 1
        yield 1

    def _builtin_inject_crc_error(self, args: List[object], node: Node) -> object:
        """``injectCrcError(nodeName)``：让该节点的下一次发送携带错误 CRC。"""
        if not args:
            raise CaplRuntimeError("injectCrcError 需要节点名", node.line, node.column)
        self.bus.inject_crc_error(self._ensure_node(str(args[0])))
        return 1
        yield 1

    def _builtin_inject_stuff_error(self, args: List[object], node: Node) -> object:
        """``injectStuffError(nodeName, atBit)``：让该节点的下一次发送出现填充错误。"""
        if not args:
            raise CaplRuntimeError("injectStuffError 需要节点名", node.line, node.column)
        name = self._ensure_node(str(args[0]))
        at_bit = self.to_int(args[1]) if len(args) > 1 else 0
        self.bus.inject_stuff_error(name, at_bit=at_bit)
        return 1
        yield 1

    def _builtin_node_send(self, args: List[object], node: Node) -> object:
        """``nodeSend(nodeName, canId, byte0)``：让某节点发一帧，返回是否成功。"""
        if len(args) < 2:
            raise CaplRuntimeError("nodeSend 至少需要节点名与 ID", node.line, node.column)
        name = self._ensure_node(str(args[0]))
        can_id = self.to_int(args[1])
        byte0 = self.to_int(args[2]) if len(args) > 2 else 0
        result = self.bus.send(name, self._make_frame(can_id, byte0))
        return (1 if result.sent else 0)
        yield (1 if result.sent else 0)

    def _builtin_node_tec(self, args: List[object], node: Node) -> object:
        """``nodeTec(nodeName)``：该节点的发送错误计数（TEC）。"""
        name = self._ensure_node(str(args[0]))
        return self.bus.node(name).tec
        yield self.bus.node(name).tec

    def _builtin_node_rec(self, args: List[object], node: Node) -> object:
        """``nodeRec(nodeName)``：该节点的接收错误计数（REC）。"""
        name = self._ensure_node(str(args[0]))
        return self.bus.node(name).rec
        yield self.bus.node(name).rec

    def _builtin_node_state(self, args: List[object], node: Node) -> object:
        """``nodeErrorState(nodeName)``：错误限制状态（字符串）。

        取值：``"error-active"`` / ``"error-passive"`` / ``"bus-off"``。
        """
        name = self._ensure_node(str(args[0]))
        return self.bus.node(name).error_state
        yield self.bus.node(name).error_state

    def _builtin_node_is_bus_off(self, args: List[object], node: Node) -> object:
        """``nodeIsBusOff(nodeName)``：该节点是否处于 bus-off。"""
        name = self._ensure_node(str(args[0]))
        return (1 if self.bus.node(name).counters.is_bus_off else 0)
        yield (1 if self.bus.node(name).counters.is_bus_off else 0)

    def _builtin_node_recovery_sequences(self, args: List[object], node: Node) -> object:
        """``nodeRecoverySequences(nodeName)``：bus-off 恢复已累计的 "11 个隐性位" 次数。"""
        name = self._ensure_node(str(args[0]))
        return self.bus.node(name).counters.recovery_sequences
        yield self.bus.node(name).counters.recovery_sequences

    def _builtin_node_tec_peak(self, args: List[object], node: Node) -> object:
        """``nodeTecPeak(nodeName)``：TEC 历史峰值。"""
        name = self._ensure_node(str(args[0]))
        return self.bus.node(name).counters.tec_peak
        yield self.bus.node(name).counters.tec_peak

    def _builtin_node_tx_success(self, args: List[object], node: Node) -> object:
        """``nodeTxSuccess(nodeName)``：该节点成功发送的帧数。"""
        name = self._ensure_node(str(args[0]))
        return self.bus.node(name).tx_success
        yield self.bus.node(name).tx_success

    def _builtin_node_error_frames(self, args: List[object], node: Node) -> object:
        """``nodeErrorFrames(nodeName)``：该节点发出错误帧的次数。"""
        name = self._ensure_node(str(args[0]))
        return self.bus.node(name).error_frames_sent
        yield self.bus.node(name).error_frames_sent

    def _builtin_bus_idle_ms(self, args: List[object], node: Node) -> object:
        """``busIdleMs(ms)``：在总线上推进一段空闲虚拟时间（bus-off 恢复需要）。

        :return: 推进的位时间个数。
        """
        milliseconds = self.to_int(args[0]) if args else 0
        if milliseconds <= 0:
            return 0
            yield 0
        bits = self.bus.idle_bits_for(float(milliseconds))
        self.bus.idle(bits)
        self.now_ms += float(milliseconds)
        return bits
        yield bits

    def _builtin_error_count(self, args: List[object], node: Node) -> object:
        """``errorCount(nodeName, kind)``：该节点某类错误的累计次数。"""
        if len(args) < 2:
            raise CaplRuntimeError("errorCount 需要节点名与错误类型",
                                   node.line, node.column)
        name = self._ensure_node(str(args[0]))
        kind = self.to_string(args[1])
        return self.bus.node(name).counters.error_counts.get(kind, 0)
        yield self.bus.node(name).counters.error_counts.get(kind, 0)


    def _builtin_set_interval(self, args: List[object], node: Node) -> object:
        """``setInterval(timer, ms)``：注册周期性定时器（到期自动重装）。"""
        if len(args) < 2:
            raise CaplRuntimeError("setInterval(timer, ms) 需要 2 个参数",
                                   node.line, node.column)
        timer = args[0]
        if not isinstance(timer, TimerValue):
            raise CaplRuntimeError("setInterval 的第一个参数必须是定时器",
                                   node.line, node.column)
        period = float(self.to_int(args[1]))
        if period <= 0:
            raise CaplRuntimeError("setInterval 的周期必须大于 0",
                                   node.line, node.column)
        self._intervals[timer.name] = period
        timer.period_ms = period
        timer.running = True
        timer.deadline_ms = self.now_ms + period
        self._timer_events.append((timer.deadline_ms, timer.name))
        return 1
        yield 1

    def _builtin_cancel_interval(self, args: List[object], node: Node) -> object:
        """``cancelInterval(timer)``：取消周期定时器。"""
        if not args or not isinstance(args[0], TimerValue):
            raise CaplRuntimeError("cancelInterval 需要定时器参数",
                                   node.line, node.column)
        timer = args[0]
        self._intervals.pop(timer.name, None)
        timer.running = False
        self._timer_events = [(d, n) for d, n in self._timer_events
                              if n != timer.name]
        return 1
        yield 1

    # ------------------------------------------------------------------
    # 报文 / 定时器辅助
    # ------------------------------------------------------------------

    def send_message(self, name: str) -> bool:
        """按变量名发送一条报文（测试与 CLI 用）。"""
        msg = self.messages.get(name)
        if msg is None:
            raise CaplRuntimeError("no message named %r" % name)
        self.schedule_send(msg)
        self._flush_transmissions()
        return True

    def window_open(self, key: str) -> None:
        """注入一次按键事件（CAPL 里对应 "按下按键"）。"""
        self._dispatch_key(key)

    #: :meth:`window_open` 的别名，语义更直观
    inject_key = window_open

    @property
    def file_scope(self) -> Dict[str, object]:
        """脚本的全局变量字典（供测试与调试查看）。"""
        return self.globals

    def variable(self, name: str) -> object:
        """读取一个脚本变量的当前值。"""
        value = self.globals.get(name)
        if value is None and name in self.messages:
            return self.messages[name]
        if value is None and name in self.timers:
            return self.timers[name]
        return value

    def bus_advance_ms(self, milliseconds: float) -> float:
        """在总线上推进一段**空闲**虚拟时间（同时推进脚本时钟）。

        bus-off 恢复、总线负载率统计等需要"总线空闲一段时间"的场景会用到。
        恢复计数由 :class:`canlab.errors.ErrorCounters` 在每一段空闲位里累加。

        :return: 推进后的虚拟毫秒。
        """
        if milliseconds <= 0:
            return self.now_ms
        target = self.now_ms + milliseconds
        self._advance_bus_to(target)
        self.now_ms = target
        return self.now_ms

    def stats_snapshot(self) -> Dict[str, object]:
        """返回运行统计（供报告使用）。"""
        return {
            "virtual_ms": self.now_ms,
            "events": self.event_count,
            "start_handlers": self.start_count,
            "message_events": self.message_event_count,
            "timer_callbacks": self.timer_callback_count,
            "stop_handlers": self.stop_count,
            "functions_called": self.stats["functions_called"],
            "frames_transmitted": len(self.transmitted),
            "frames_received": len(self.received),
            "assertions": len(self.assertions),
            "assertions_passed": sum(1 for a in self.assertions if a.passed),
            "assertions_failed": sum(1 for a in self.assertions if not a.passed),
            "test_cases": len(self.test_cases),
            "errors": len(self.errors),
        }


def frame_to_message(frame: Frame) -> MsgValue:
    """把总线帧转换成脚本可用的 message 对象。"""
    msg = MsgValue(can_id=frame.identifier, dlc=frame.dlc, extended=frame.extended,
                   data=frame.data, fd=frame.fd)
    msg.brs = frame.brs
    msg.dir = "RX"
    msg.source = frame.source  # type: ignore[attr-defined]
    return msg


def _safe_int(value: object) -> int:
    """尽力把值转成整数（供真值判断使用，不做严格类型检查）。"""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return 1


def _unwrap_generator(value: object) -> object:
    """如果 ``value`` 是生成器，就地把它驱动完并返回其返回值。

    解释器正常路径不会走到这里；这个函数是防止"生成器对象被当成数值"的兜底。
    """
    if not (hasattr(value, "send") and hasattr(value, "throw")):
        return value
    result = None
    while True:
        try:
            next(value)          # type: ignore[arg-type]
        except StopIteration as stop:
            result = stop.value
            break
    return result


def _as_generator(result: object) -> object:
    """确保结果是生成器：已经是生成器就原样返回，否则包装成只返回该值的生成器。

    解释器内部统一约定 ``eval`` / ``exec_statement`` 返回生成器，这样调用方可以
    无差别地使用 ``yield from``；同步实现的节点由这里适配。
    """
    if hasattr(result, "send") and hasattr(result, "throw"):
        return result
    return _value_generator(result)


def _wrap(name: str) -> Callable[..., object]:
    """为内建函数生成模块级适配器。

    ``CaplEngine`` 上的 ``_builtin_*`` 实现是**同步**方法（直接返回值）；
    这里统一包装成生成器，使调用方可以一致地使用 ``yield from``。
    """
    def adapter(engine: "CaplEngine", args: List[object], node: Node) -> object:
        result = getattr(engine, name)(args, node)
        if hasattr(result, "send") and hasattr(result, "throw"):
            return result
        return _value_generator(result)
    adapter.__name__ = name
    adapter.__doc__ = getattr(CaplEngine, name).__doc__
    return adapter


#: 脚本里可以调用的内建函数名 -> 适配器
CaplEngine.BUILTINS = {
    "output": _wrap("_builtin_output"),
    "write": _wrap("_builtin_write"),
    "writeLine": _wrap("_builtin_write_line"),
    "setTimer": _wrap("_builtin_set_timer"),
    "cancelTimer": _wrap("_builtin_cancel_timer"),
    "isTimerActive": _wrap("_builtin_is_timer_active"),
    "getTimer": _wrap("_builtin_get_timer"),
    "testWaitForMessage": _wrap("_builtin_test_wait_for_message"),
    "testWaitForTimeout": _wrap("_builtin_test_wait_for_timeout"),
    "testAssertTrue": _wrap("_builtin_test_assert_true"),
    "testAssertFalse": _wrap("_builtin_test_assert_false"),
    "testAssertEquals": _wrap("_builtin_test_assert_equals"),
    "testAssertNotEquals": _wrap("_builtin_test_assert_not_equals"),
    "testAssertInRange": _wrap("_builtin_test_assert_in_range"),
    "testCaseBegin": _wrap("_builtin_test_case_begin"),
    "testCaseEnd": _wrap("_builtin_test_case_end"),
    "testAddComment": _wrap("_builtin_test_add_comment"),
    "abs": _wrap("_builtin_abs"),
    "min": _wrap("_builtin_min"),
    "max": _wrap("_builtin_max"),
    "getTickCount": _wrap("_builtin_get_tick_count"),
    "random": _wrap("_builtin_random"),
    "jitterMs": _wrap("_builtin_jitter_ms"),
    "getSignalValue": _wrap("_builtin_get_signal_value"),
    "setSignalValue": _wrap("_builtin_set_signal_value"),
    "busSend2": _wrap("_builtin_bus_send2"),
    "busSend3": _wrap("_builtin_bus_send3"),
    "busSendDataRtr": _wrap("_builtin_bus_send_data_rtr"),
    "arbitrationCount": _wrap("_builtin_arbitration_count"),
    "arbitrationWinnerId": _wrap("_builtin_arbitration_winner_id"),
    "arbitrationLoserCount": _wrap("_builtin_arbitration_loser_count"),
    "arbitrationLoserBit": _wrap("_builtin_arbitration_loser_bit"),
    "arbitrationLoserName": _wrap("_builtin_arbitration_loser_name"),
    "arbitrationLoserId": _wrap("_builtin_arbitration_loser_id"),
    "arbitrationLoserRetries": _wrap("_builtin_arbitration_loser_retries"),
    "totalFramesSent": _wrap("_builtin_total_frames_sent"),
    "frameIdAt": _wrap("_builtin_frame_id_at"),
    "frameSourceAt": _wrap("_builtin_frame_source_at"),
    "busBitrateKbps": _wrap("_builtin_bus_bitrate"),
    "busLoadX100": _wrap("_builtin_bus_load_x100"),
    "busTimeMs": _wrap("_builtin_bus_time_ms"),
    "injectBitError": _wrap("_builtin_inject_bit_error"),
    "injectCrcError": _wrap("_builtin_inject_crc_error"),
    "injectStuffError": _wrap("_builtin_inject_stuff_error"),
    "nodeSend": _wrap("_builtin_node_send"),
    "nodeTec": _wrap("_builtin_node_tec"),
    "nodeRec": _wrap("_builtin_node_rec"),
    "nodeErrorState": _wrap("_builtin_node_state"),
    "nodeIsBusOff": _wrap("_builtin_node_is_bus_off"),
    "nodeRecoverySequences": _wrap("_builtin_node_recovery_sequences"),
    "nodeTecPeak": _wrap("_builtin_node_tec_peak"),
    "nodeTxSuccess": _wrap("_builtin_node_tx_success"),
    "nodeErrorFrames": _wrap("_builtin_node_error_frames"),
    "errorCount": _wrap("_builtin_error_count"),
    "busIdleMs": _wrap("_builtin_bus_idle_ms"),
    "setInterval": _wrap("_builtin_set_interval"),
    "cancelInterval": _wrap("_builtin_cancel_interval"),
}


def _value_generator(value: object) -> object:
    """把一个普通值包装成只返回该值的生成器。"""
    def gen() -> Iterable[object]:
        return value
        yield value
    return gen()



def extract_signal(data: bytes, start_bit: int, bit_length: int) -> int:
    """按 Little-Endian 位序从数据场里取一个位段。

    :param start_bit: 起始位（0 = 第 0 字节的最低位）。
    :param bit_length: 位段长度（1-64）。
    """
    if bit_length <= 0 or bit_length > 64:
        raise CaplRuntimeError("signal bit length must be 1..64")
    value = 0
    for offset in range(bit_length):
        bit_index = start_bit + offset
        byte_index = bit_index // 8
        if byte_index >= len(data):
            break
        bit = (data[byte_index] >> (bit_index % 8)) & 1
        value |= bit << offset
    return value


def insert_signal(data: bytes, start_bit: int, bit_length: int,
                  value: int) -> bytes:
    """按 Little-Endian 位序把位段写回数据场，返回新的字节串。"""
    if bit_length <= 0 or bit_length > 64:
        raise CaplRuntimeError("signal bit length must be 1..64")
    out = bytearray(data)
    for offset in range(bit_length):
        bit_index = start_bit + offset
        byte_index = bit_index // 8
        if byte_index >= len(out):
            break
        bit = (value >> offset) & 1
        mask = 1 << (bit_index % 8)
        if bit:
            out[byte_index] |= mask
        else:
            out[byte_index] &= ~mask & 0xFF
    return bytes(out)
