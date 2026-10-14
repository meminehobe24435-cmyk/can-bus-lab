# -*- coding: utf-8 -*-
"""自研的类 CAPL 脚本引擎。

本子包只借鉴 CAPL 的**书写习惯**（``variables {}`` 块、``on message 0x123``
事件、``msTimer`` 定时器、``output()`` 发送报文、``write()`` 打印等），
词法分析器、递归下降语法分析器、解释器与虚拟时间调度器全部自行实现。

模块构成
--------
:mod:`canlab.capl.lexer`
    词法分析：源码 -> 记号流。
:mod:`canlab.capl.parser`
    语法分析：记号流 -> AST（``Script`` / ``EventHandler`` / 语句 / 表达式）。
:mod:`canlab.capl.interp`
    解释执行 + 虚拟时间调度器 + 测试原语 + 内建函数。

典型用法::

    from canlab.bus import CanBus
    from canlab.capl.interp import CaplEngine

    bus = CanBus()
    engine = CaplEngine(bus=bus, node_name="TESTER", seed=1)
    engine.load_file("demo/scripts/01_periodic_jitter.capl")
    engine.run(max_virtual_ms=2000)
    print(engine.stats_snapshot())
"""

from .interp import (
    AssertionResult, CaplEngine, CaplRuntimeError, CallFrame, MsgValue,
    ReportEntry, TestCaseResult, TimerValue, WaitRequest, extract_signal,
    frame_to_message, insert_signal,
)
from .lexer import LexError, Token, format_tokens, tokenize
from .parser import (
    EventHandler, FunctionDecl, ParseError, Script, count_nodes, parse,
)

__all__ = [
    "AssertionResult", "CaplEngine", "CaplRuntimeError", "CallFrame",
    "MsgValue", "ReportEntry", "TestCaseResult", "TimerValue", "WaitRequest",
    "extract_signal", "frame_to_message", "insert_signal",
    "LexError", "Token", "format_tokens", "tokenize",
    "EventHandler", "FunctionDecl", "ParseError", "Script", "count_nodes", "parse",
]
