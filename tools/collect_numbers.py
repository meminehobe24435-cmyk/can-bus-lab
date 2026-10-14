# -*- coding: utf-8 -*-
"""收集 README 所需的**真实**数字（全部来自本机实际运行）。"""
import hashlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 仓库根目录（本脚本位于 tools/ 下）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from canlab import frame as F
from canlab.bus import CanBus
from canlab.capl import CaplEngine
from canlab.report import summarize, to_text
from canlab.frame import Frame

print("=" * 70)
print("[1] CRC 已知向量（自算，用于回归锁定）")
print("=" * 70)
vecs = [
    ("crc15([])", F.crc15([])),
    ("crc15([1])", F.crc15([1])),
    ("crc15(0x123456,24bit)", F.crc15(F.int_to_bits(0x123456, 24))),
    ("crc15(0xDEADBEEF,32bit)", F.crc15(F.int_to_bits(0xDEADBEEF, 32))),
    ("crc17(0x123456,24bit)", F.crc17(F.int_to_bits(0x123456, 24))),
    ("crc21(0x123456,24bit)", F.crc21(F.int_to_bits(0x123456, 24))),
]
for name, value in vecs:
    print("  %-26s = 0x%X" % (name, value))

# 整帧的 CRC（黄金样例）
for label, fr in [
    ("std 0x123 dlc4 11 22 33 44", Frame(identifier=0x123, data=bytes([0x11, 0x22, 0x33, 0x44]))),
    ("std 0x000 dlc0", Frame(identifier=0x000, data=b"")),
    ("std 0x7FF dlc8 FF*8", Frame(identifier=0x7FF, data=b"\xFF" * 8)),
    ("ext 0x1ABCDEF dlc8 00..07", Frame(identifier=0x1ABCDEF, data=bytes(range(8)), extended=True)),
    ("rtr 0x321 dlc8", Frame.remote_frame(0x321, 8)),
    ("fd  0x100 dlc12", Frame(identifier=0x100, data=bytes(range(12)), fd=True)),
    ("fd  0x100 dlc64", Frame(identifier=0x100, data=bytes(range(64)), fd=True, brs=True)),
]:
    result = F.decode_detailed(F.encode(fr))
    print("  %-28s CRC%d = 0x%-6X 位流 %3d 位 填充位 %2d 帧内 %d 字节"
          % (label, result.crc_width, result.crc_received, len(F.encode(fr)),
             result.stuff_bits_count, len(fr.data)))

print()
print("=" * 70)
print("[2] 仲裁实验：两节点同时发送")
print("=" * 70)
bus = CanBus()
bus.add_node("ECU_A")
bus.add_node("ECU_B")
results = bus.contend([("ECU_A", Frame(identifier=0x100, data=b"\x01" * 4)),
                       ("ECU_B", Frame(identifier=0x200, data=b"\x02" * 4))])
for result in results:
    node = bus.node(result.node)
    print("  %s: sent=%s 仲裁获胜=%s 失败位=%s 重发=%d TEC=%d 状态=%s"
          % (result.node, result.sent, result.arbitration_won, result.lost_at_bit,
             node.retransmissions, node.tec, node.error_state))
print("  总线发送顺序: %s" % [hex(f.identifier) for f in bus.trace])
print("  总线负载率: %.2f%%（背靠背发送）" % bus.stats.load_percent)
record = bus.arbitrations[0]
print("  仲裁记录: 赢家=%s(0x%X) 失败者=%s 失败位=%d 仲裁场消耗=%d 位"
      % (record.winner, record.winner_id, record.losers, record.losers[0][1],
         record.arbitration_bits))

print()
print("=" * 70)
print("[3] bus-off 恢复")
print("=" * 70)
bus2 = CanBus()
node = bus2.add_node("ECU_X")
injections = 0
peak = 0
passive_at = None
while not node.counters.is_bus_off and injections < 100:
    bus2.inject_bit_error("ECU_X", at_bit=20)
    bus2.send("ECU_X", Frame(identifier=0x300, data=bytes([injections & 0xFF])))
    injections += 1
    peak = max(peak, node.tec)
    if passive_at is None and node.error_state == "error-passive":
        passive_at = (injections, node.tec)
print("  注入位错误次数: %d" % injections)
print("  首次进入 error-passive: 第 %d 次注入, TEC=%d" % passive_at)
print("  TEC 峰值: %d" % peak)
print("  最终状态: %s (TEC=%d)" % (node.error_state, node.tec))
idle = 0
while node.counters.is_bus_off and idle < 5000:
    bus2.idle(1)
    idle += 1
print("  恢复到 error-active 需要 %d 个隐性位 = 128 x 11" % idle)
print("  恢复后 TEC=%d REC=%d 状态=%s" % (node.tec, node.rec, node.error_state))
print("  折算虚拟时间: %.3f ms（500 kbit/s）" % (idle * bus2.bit_time_ms))

print()
print("=" * 70)
print("[4] 脚本引擎与 demo 汇总")
print("=" * 70)
script_dir = os.path.join(ROOT, "demo", "scripts")
total_assertions = 0
total_events = 0
for name in sorted(os.listdir(script_dir)):
    if not name.endswith(".capl"):
        continue
    path = os.path.join(script_dir, name)
    engine = CaplEngine(bus=CanBus(), node_name="TESTER", seed=1)
    engine.load_file(path)
    engine.bus.on_frame = engine._on_bus_frame
    engine.run(max_virtual_ms=60000.0)
    summary = summarize(engine, name)
    text = to_text(engine, summary)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    total_assertions += summary.assertions
    total_events += int(summary.stats["events"])
    print("  %-34s 断言 %2d/%2d  事件 %3d  定时器 %3d  报文 %3d/%3d  虚拟时间 %8.3f ms"
          % (name, summary.assertions_passed, summary.assertions,
             summary.stats["events"], summary.stats["timer_callbacks"],
             summary.stats["frames_transmitted"], summary.stats["frames_received"],
             summary.virtual_ms))
    print("      SHA256(文本报告) = %s" % digest)
print("  ---- 合计: 断言 %d 条, 事件回调 %d 次" % (total_assertions, total_events))

print()
print("=" * 70)
print("[5] ISO-TP 分帧与 STmin")
print("=" * 70)
from canlab import isotp as I
payload = bytes([0x00, 0x00, 0x00, 0x00]) + bytes([0xAA]) * 56
pdus = I.split_message(payload)
print("  payload %d 字节 -> %d 帧 (经典 CAN)" % (len(payload), len(pdus)))
print("  帧类型: %s" % [I.decode_pdu(p).kind_name for p in pdus])
print("  每帧长度: %s" % [len(p) for p in pdus])

now = [0.0]
conn = I.IsoTpConnection(lambda p: None, I.IsoTpConfig(), lambda: now[0])
conn.send(payload)
conn.on_pdu(I.encode_fc(I.FS_CTS, 0, 5))   # BS=0, STmin=5 ms
ticks = 0
start = now[0]
while not conn.tx.finished and ticks < 100:
    now[0] += 5.0
    ticks += 1
    if not conn.send_next_consecutive():
        break
print("  STmin=5 ms 时发送 8 个连续帧耗时 %.1f ms（%d 个 STmin 时隙）"
      % (now[0] - start, ticks))
print("  发送帧数 = %d，拼回字节数 = %d" % (conn.frames_sent, conn.tx.sent_length))

now2 = [0.0]
conn2 = I.IsoTpConnection(lambda p: None, I.IsoTpConfig(), lambda: now2[0])
conn2.send(payload)
conn2.on_pdu(I.encode_fc(I.FS_CTS, 0, 0))  # STmin=0
while not conn2.tx.finished:
    if not conn2.send_next_consecutive():
        break
print("  STmin=0 时耗时 %.1f ms（对照）" % (now2[0] - start))

print()
print("=" * 70)
print("[6] UDS 服务与 NRC 覆盖")
print("=" * 70)
from canlab import uds as U
server = U.UdsServer(clock=lambda: 0.0)
server.add_did(0xF190, b"VIN0123456789ABCD")
server.add_did(0x1234, b"\x00\x00", requires_security=True)
server.add_dtc(0x00A001, snapshot=b"\x01\x02\x03")
server.add_routine(0xFF00, result=b"\x01")
client = U.UdsClient(server, clock=lambda: 0.0)

served = []
for label, call in [
    ("0x10 会话控制", lambda: client.session_control(U.Session.EXTENDED)),
    ("0x11 ECU 复位", lambda: client.ecu_reset()),
    ("0x22 读数据", lambda: client.request(bytes([0x22, 0xF1, 0x90]))),
    ("0x2E 写数据", lambda: client.request(bytes([0x2E, 0x12, 0x34, 0x5A]))),
    ("0x27 安全访问", lambda: client.security_access_unlock(1)),
    ("0x19 读 DTC", lambda: client.request(bytes([0x19, 0x01, 0xFF]))),
    ("0x14 清 DTC", lambda: client.request(bytes([0x14, 0xFF, 0xFF, 0xFF]))),
    ("0x31 例程控制", lambda: client.routine_control(1, 0xFF00)),
    ("0x3E TesterPresent", lambda: client.request(bytes([0x3E, 0x00]))),
]:
    response = call()
    served.append((label, response))
    print("  %-18s -> %s" % (label, response.data.hex().upper() or "(suppressed)"))

print()
nrcs = []
for label, payload in [
    ("不信服务", bytes([0x99, 0x01])),
    ("子功能不支持", bytes([0x10, 0x7E])),
    ("长度错误", bytes([0x22, 0xF1])),
    ("条件不满足", bytes([0x14, 0xFF, 0xFF, 0xFF])),
    ("请求序号错", bytes([0x27, 0x02, 0x00, 0x00])),
    ("超出范围", bytes([0x22, 0x00, 0x01])),
    ("安全拒绝", bytes([0x22, 0x12, 0x34])),
    ("密钥无效", bytes([0x27, 0x01, 0x00, 0x00])),
]:
    srv = U.UdsServer(clock=lambda: 0.0)
    srv.add_did(0x1234, b"\x00", requires_security=True)
    cli = U.UdsClient(srv, clock=lambda: 0.0)
    response = cli.request(payload)
    nrcs.append((label, response.nrc))
    print("  %-14s -> NRC 0x%02X (%s)" % (label, response.nrc, response.nrc_name))

print()
print("=" * 70)
print("[7] 规模统计")
print("=" * 70)
root = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def count_lines(pattern_dir, suffix):
    total = 0
    files = 0
    biggest = []
    for base, _dirs, names in os.walk(pattern_dir):
        if "__pycache__" in base:
            continue
        for name in names:
            if name.endswith(suffix):
                path = os.path.join(base, name)
                with io.open(path, encoding="utf-8") as handle:
                    count = sum(1 for _ in handle)
                total += count
                files += 1
                biggest.append((count, os.path.relpath(path, root)))
    biggest.sort(reverse=True)
    return files, total, biggest

for label, sub, suffix in [("canlab/ 源码", "canlab", ".py"),
                           ("tests/ 测试", "tests", ".py"),
                           ("demo/ 脚本", "demo", ".capl")]:
    files, total, biggest = count_lines(os.path.join(root, sub), suffix)
    print("  %-14s %2d 个文件, %5d 行" % (label, files, total))
    for count, name in biggest[:5]:
        print("      %5d 行  %s" % (count, name))
