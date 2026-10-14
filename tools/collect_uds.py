# -*- coding: utf-8 -*-
"""补齐 UDS 服务/NRC 覆盖统计（含先决条件设置）。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from canlab import uds as U

print("--- 服务覆盖（含先决条件）---")
server = U.UdsServer(clock=lambda: 0.0)
server.add_did(0xF190, b"VIN0123456789ABCD")
server.add_did(0x1234, b"\x00\x00", requires_security=True)
server.add_dtc(0x00A001, snapshot=b"\x01\x02\x03")
server.add_routine(0xFF00, result=b"\x01")
client = U.UdsClient(server, clock=lambda: 0.0)

steps = []
steps.append(("0x10 会话控制", client.session_control(U.Session.EXTENDED)))
steps.append(("0x22 读数据", client.request(bytes([0x22, 0xF1, 0x90]))))
steps.append(("0x27 安全访问", client.security_access_unlock(1)))
steps.append(("0x2E 写数据", client.request(bytes([0x2E, 0x12, 0x34, 0x5A]))))
steps.append(("0x22 读回", client.request(bytes([0x22, 0x12, 0x34]))))
steps.append(("0x19 读 DTC", client.request(bytes([0x19, 0x01, 0xFF]))))
steps.append(("0x31 例程控制", client.routine_control(1, 0xFF00)))
steps.append(("0x3E TesterPresent", client.request(bytes([0x3E, 0x00]))))
steps.append(("0x14 清 DTC", client.request(bytes([0x14, 0xFF, 0xFF, 0xFF]))))
steps.append(("0x19 再读 DTC", client.request(bytes([0x19, 0x01, 0xFF]))))
steps.append(("0x11 ECU 复位", client.ecu_reset()))
positive = 0
for label, response in steps:
    kind = "NRC 0x%02X" % response.nrc if response.negative else "正响应"
    if not response.negative:
        positive += 1
    print("  %-16s -> %-46s %s"
          % (label, response.data.hex().upper() or "(suppressed)", kind))
print("  正响应 %d 条 / 共 %d 条" % (positive, len(steps)))

print()
print("--- NRC 覆盖 ---")


def fresh():
    srv = U.UdsServer(clock=lambda: 0.0)
    srv.add_did(0x1234, b"\x00", requires_security=True)
    srv.add_dtc(0x00A001)
    srv.add_routine(0xFF00)
    srv.add_routine(0xFF01, start_nrc=0x33)
    return srv, U.UdsClient(srv, clock=lambda: 0.0)


cases = []
srv, cli = fresh()
cases.append(("serviceNotSupported", cli.request(bytes([0x99, 0x01]))))
cases.append(("subFunctionNotSupported", cli.request(bytes([0x10, 0x7E]))))
cases.append(("incorrectMessageLength", cli.request(bytes([0x22, 0xF1]))))
cases.append(("conditionsNotCorrect", cli.request(bytes([0x14, 0xFF, 0xFF, 0xFF]))))
cases.append(("securityAccessDenied", cli.request(bytes([0x22, 0x12, 0x34]))))
cases.append(("requestSequenceError", cli.request(bytes([0x27, 0x02, 0x00, 0x00]))))
cases.append(("requestOutOfRange", cli.request(bytes([0x22, 0x00, 0x01]))))
cases.append(("subFunction(0x31)", cli.request(bytes([0x31, 0x09, 0xFF, 0x00]))))
cases.append(("routine NRC 透传 0x33", cli.request(bytes([0x31, 0x01, 0xFF, 0x01]))))

srv, cli = fresh()
cli.session_control(U.Session.EXTENDED)
cli.request(bytes([0x27, 0x01]))
cases.append(("invalidKey", cli.request(bytes([0x27, 0x02, 0xFF, 0xFF]))))
last = None
for _ in range(2):
    cli.request(bytes([0x27, 0x01]))
    last = cli.request(bytes([0x27, 0x02, 0xFF, 0xFF]))
cases.append(("exceedNumberOfAttempts", last))
cases.append(("requiredTimeDelayNotExpired",
              cli.request(bytes([0x27, 0x02, 0x00, 0x00]))))

for label, response in cases:
    print("  %-32s -> NRC 0x%02X %s" % (label, response.nrc, response.nrc_name))
print("  覆盖 NRC 种类: %d" % len({r.nrc for _l, r in cases if r.negative}))
