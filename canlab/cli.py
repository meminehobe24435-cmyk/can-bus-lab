# -*- coding: utf-8 -*-
"""命令行入口。

用法::

    python -m canlab run demo/scripts/01_periodic_jitter.capl [--trace] [--report out.xml]
    python -m canlab selftest
    python -m canlab replay trace.json

设计原则：所有输出都由**虚拟时间**决定，同一脚本两次运行（同 seed）产生逐字节
一致的文本与报告。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from . import __version__
from .bus import CanBus
from .capl.interp import CaplEngine, CaplRuntimeError
from .capl.lexer import LexError
from .capl.parser import ParseError
from .frame import Frame, encode, decode, crc15
from .isotp import IsoTpConfig, IsoTpConnection, split_message
from .report import RunSummary, summarize, to_junit_xml, to_text, write_report
from .uds import Session, UdsClient, UdsServer

#: 演示脚本目录
DEFAULT_SCRIPT_DIR = os.path.join("demo", "scripts")


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def _format_trace_entry(index: int, frame: Frame) -> str:
    """把一帧格式化成 trace 行。"""
    kind = "RTR" if frame.remote else "DAT"
    proto = "FD " if frame.fd else "CC "
    return "%4d  %10.3f ms  %s%s  0x%0*X  dlc=%2d  [%s]  src=%s" % (
        index,
        frame.timestamp,
        proto,
        kind,
        len("%X" % frame.identifier),
        frame.identifier,
        frame.dlc,
        " ".join("%02X" % b for b in frame.data),
        frame.source or "-",
    )


def cmd_run(args: argparse.Namespace) -> int:
    """执行一个 CAPL 脚本并输出报告与 trace。"""
    path = args.script
    if not os.path.isfile(path):
        sys.stderr.write("找不到脚本文件: %s\n" % path)
        return 2
    # 报告里只写脚本文件名（不含绝对/相对目录），这样报告内容与运行位置无关，
    # 换台机器跑同样的脚本能得到逐字节一致的产物
    script_name = os.path.basename(path)

    bus = CanBus(bitrate=args.bitrate, min_gap_bits=args.gap)
    engine = CaplEngine(bus=bus, node_name=args.node, seed=args.seed,
                        bitrate=args.bitrate)
    try:
        engine.load_file(path)
    except (LexError, ParseError) as error:
        sys.stderr.write("脚本解析失败: %s\n" % error)
        return 3

    # 总线回调：把每一帧都送进解释器的事件派发
    bus.on_frame = engine._on_bus_frame

    engine.run(max_virtual_ms=args.max_ms)

    # 等待期间的定时器交错：run() 已经在推进虚拟时间时派发了它们
    summary = summarize(engine, script_name=script_name)
    trace_lines = [
        _format_trace_entry(index, frame)
        for index, frame in enumerate(engine.transmitted)
        if args.trace or index < 0
    ]

    text = to_text(engine, summary, trace_lines if args.trace else None)
    if args.quiet:
        header = "脚本 %s: %s (断言 %d/%d 通过, 虚拟时间 %.3f ms)" % (
            os.path.basename(path),
            "通过" if summary.passed else "失败",
            summary.assertions_passed, summary.assertions,
            summary.virtual_ms,
        )
        sys.stdout.write(header + "\n")
    else:
        sys.stdout.write(text)

    if args.report:
        write_report(args.report, to_junit_xml(engine, summary,
                                               suite_name=script_name))
        sys.stdout.write("JUnit XML 报告已写入: %s\n" % args.report)
    if args.text_report:
        write_report(args.text_report, text)
        sys.stdout.write("文本报告已写入: %s\n" % args.text_report)
    if args.dump_trace:
        write_report(args.dump_trace,
                     json.dumps(bus.export_trace(), indent=2, ensure_ascii=False) + "\n")
        sys.stdout.write("trace 已写入: %s\n" % args.dump_trace)

    return 0 if summary.passed else 1


def cmd_selftest(args: argparse.Namespace) -> int:
    """内置自检：不需要 pytest，直接跑一组核心行为断言。"""
    failures: List[str] = []
    checks = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal checks
        checks += 1
        status = "ok  " if condition else "FAIL"
        sys.stdout.write("  [%s] %s%s\n" % (status, name, ("  -- " + detail) if detail else ""))
        if not condition:
            failures.append(name)

    sys.stdout.write("can-bus-lab 自检 (v%s)\n" % __version__)
    sys.stdout.write("[1] 帧层\n")
    frame = Frame(identifier=0x123, data=bytes([0x11, 0x22, 0x33, 0x44]))
    bits = encode(frame)
    decoded = decode(bits)
    check("标准帧 编码/解码 往返一致",
          decoded.identifier == 0x123 and decoded.data == frame.data,
          "长度 %d 位" % len(bits))
    check("CRC15 空位流为 0", crc15([]) == 0)
    ext = Frame(identifier=0x1ABCDEF, data=bytes(range(8)), extended=True)
    check("扩展帧 往返一致",
          decode(encode(ext)).identifier == 0x1ABCDEF)
    fd = Frame(identifier=0x100, data=bytes(range(64)), fd=True)
    check("CAN FD 64 字节 往返一致", decode(encode(fd)).data == fd.data)

    sys.stdout.write("[2] 总线仲裁\n")
    bus = CanBus()
    bus.add_node("A")
    bus.add_node("B")
    results = bus.contend([("A", Frame(0x100, bytes([1]))),
                           ("B", Frame(0x200, bytes([2])))])
    winner = [r.node for r in results if r.arbitration_won]
    check("ID 小的节点赢得逐位仲裁", winner == ["A"], "赢家=%s" % winner)
    check("ID 大的节点发生重发",
          bus.node("B").retransmissions == 1,
          "重发 %d 次" % bus.node("B").retransmissions)

    sys.stdout.write("[3] 错误状态机\n")
    bus2 = CanBus()
    node = bus2.add_node("X")
    injections = 0
    while not node.counters.is_bus_off and injections < 64:
        bus2.inject_bit_error("X", at_bit=20)
        bus2.send("X", Frame(0x300, bytes([injections & 0xFF])))
        injections += 1
    check("注入位错误可使节点进入 bus-off",
          node.counters.is_bus_off, "%d 次注入, TEC=%d" % (injections, node.tec))
    idle_bits = 0
    while node.counters.is_bus_off and idle_bits < 20000:
        bus2.idle(1)
        idle_bits += 1
    check("128 x 11 个隐性位后恢复",
          not node.counters.is_bus_off and idle_bits == 128 * 11,
          "%d 位" % idle_bits)

    sys.stdout.write("[4] ISO-TP 多帧\n")
    payload = bytes([0x00, 0x00, 0x00, 0x00]) + bytes([0xCC]) * 56
    pdus = split_message(payload)
    received: List[bytes] = []
    conn = IsoTpConnection(lambda p: None, IsoTpConfig(), lambda: 0.0)
    for pdu in pdus:
        got = conn.on_pdu(pdu)
        if got is not None:
            received.append(got)
    check("60 字节报文分帧后能完整拼回",
          received and received[0] == payload,
          "%d 帧" % len(pdus))

    sys.stdout.write("[5] UDS 诊断\n")
    server = UdsServer(clock=lambda: 0.0)
    server.add_did(0xF190, b"VIN0123456789ABCD")
    server.add_dtc(0x00A001)
    client = UdsClient(server, clock=lambda: 0.0)
    response = client.session_control(Session.EXTENDED)
    check("0x10 进入扩展会话", response.data[:2] == b"\x50\x03")
    check("0x22 读数据", client.read_data(0xF190) == b"VIN0123456789ABCD")
    check("0x19 读 DTC 数量", client.read_dtc_count() == 1)

    sys.stdout.write("[6] 脚本引擎\n")
    engine = CaplEngine(bus=CanBus(), node_name="SELFTEST", seed=7)
    engine.load_source(
        "variables { int n; msTimer t; }\n"
        "on start { testCaseBegin(\"tick\"); setTimer(t, 10); }\n"
        "on timer t { n = n + 1; if (n < 3) { setTimer(t, 10); } }\n"
        "on stopMeasurement { testAssertEquals(n, 3, \"三个定时器回调\"); testCaseEnd(); }\n"
    )
    engine.run(max_virtual_ms=100)
    check("定时器回调与虚拟时间推进",
          engine.timer_callback_count == 3 and abs(engine.now_ms - 30.0) < 1e-6,
          "回调 %d 次, %.3f ms" % (engine.timer_callback_count, engine.now_ms))
    check("断言全部通过",
          all(a.passed for a in engine.assertions) and len(engine.assertions) == 1)

    sys.stdout.write("-" * 60 + "\n")
    sys.stdout.write("自检结果: %d 项检查, %d 项失败\n" % (checks, len(failures)))
    return 0 if not failures else 1


def cmd_replay(args: argparse.Namespace) -> int:
    """把 JSON trace 重放成总线激励（自研的极简 trace 重放器）。"""
    if not os.path.isfile(args.trace_file):
        sys.stderr.write("找不到 trace 文件: %s\n" % args.trace_file)
        return 2
    with io.open(args.trace_file, "r", encoding="utf-8") as handle:
        entries = json.load(handle)
    frames = CanBus.load_trace(entries)
    sys.stdout.write("载入 %d 条 trace 记录\n" % len(frames))
    bus = CanBus(bitrate=args.bitrate)
    bus.add_node("REPLAY")
    for frame in frames:
        # 保持原始时间间隔：把总线空闲推进到该帧的原始时间戳
        bus.advance_to_ms(frame.timestamp)
        clone = Frame(identifier=frame.identifier, data=frame.data,
                      extended=frame.extended, remote=frame.remote, fd=frame.fd)
        bus.send("REPLAY", clone)
    sys.stdout.write("重放完成：发送 %d 帧，总线时间 %.3f ms，负载率 %.2f%%\n"
                     % (bus.stats.frames_sent, bus.now_ms, bus.stats.load_percent))
    if args.json_out:
        write_report(args.json_out,
                     json.dumps(bus.node_stats(), indent=2, ensure_ascii=False) + "\n")
        sys.stdout.write("统计已写入: %s\n" % args.json_out)
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="canlab",
        description="CAN 总线仿真与类 CAPL 脚本化测试框架（纯标准库实现）",
    )
    parser.add_argument("--version", action="version",
                        version="can-bus-lab %s" % __version__)
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="运行一个 .capl 脚本")
    run.add_argument("script", help="脚本路径")
    run.add_argument("--trace", action="store_true", help="打印总线 trace")
    run.add_argument("--report", default="", help="输出 JUnit XML 报告路径")
    run.add_argument("--text-report", default="", help="输出文本报告路径")
    run.add_argument("--dump-trace", default="", help="导出 JSON trace 路径")
    run.add_argument("--seed", type=int, default=1, help="确定性随机种子")
    run.add_argument("--node", default="TESTER", help="本脚本使用的总线节点名")
    run.add_argument("--bitrate", type=int, default=500000, help="位速率 bit/s")
    run.add_argument("--gap", type=int, default=0,
                     help="相邻发送之间强制插入的最小空闲位数（用于负载率统计）")
    run.add_argument("--max-ms", type=float, default=60000.0,
                     help="虚拟时间上限（毫秒）")
    run.add_argument("--quiet", action="store_true", help="只输出一行结论")
    run.set_defaults(func=cmd_run)

    selftest = sub.add_parser("selftest", help="运行内置自检")
    selftest.set_defaults(func=cmd_selftest)

    replay = sub.add_parser("replay", help="重放 JSON trace 作为总线激励")
    replay.add_argument("trace_file", help="JSON trace 路径")
    replay.add_argument("--bitrate", type=int, default=500000)
    replay.add_argument("--json-out", default="", help="把统计写入 JSON 文件")
    replay.set_defaults(func=cmd_replay)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行主入口。"""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except CaplRuntimeError as error:
        sys.stderr.write("脚本运行错误: %s\n" % error)
        return 4
    except KeyboardInterrupt:                 # pragma: no cover - 交互式中断
        sys.stderr.write("已中断\n")
        return 130


if __name__ == "__main__":                    # pragma: no cover
    sys.exit(main())
