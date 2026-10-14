# -*- coding: utf-8 -*-
"""测试报告生成：JUnit XML 与纯文本两种格式。

报告来源是 :class:`canlab.capl.interp.CaplEngine` 的运行结果：测试用例
（``testCaseBegin`` / ``testCaseEnd``）、断言（``testAssert*``）与运行统计。
**报告里的耗时全部是虚拟时间（毫秒）**，不是真实墙钟时间。
"""

from __future__ import annotations

import io
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence
from xml.dom import minidom

from .capl.interp import AssertionResult, CaplEngine, TestCaseResult


@dataclass
class RunSummary(object):
    """一次脚本运行的汇总。"""

    script: str = ""
    seed: int = 1
    virtual_ms: float = 0.0
    test_cases: int = 0
    assertions: int = 0
    assertions_passed: int = 0
    assertions_failed: int = 0
    errors: List[str] = field(default_factory=list)
    stats: Dict[str, object] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """全部断言通过且没有运行期错误才算通过。"""
        return self.assertions_failed == 0 and not self.errors


def summarize(engine: CaplEngine, script_name: str = "") -> RunSummary:
    """从引擎的运行结果构造汇总对象。"""
    snapshot = engine.stats_snapshot()
    return RunSummary(
        script=script_name,
        seed=engine.seed,
        virtual_ms=float(snapshot["virtual_ms"]),
        test_cases=int(snapshot["test_cases"]),
        assertions=int(snapshot["assertions"]),
        assertions_passed=int(snapshot["assertions_passed"]),
        assertions_failed=int(snapshot["assertions_failed"]),
        errors=list(engine.errors),
        stats=snapshot,
    )


def _group_assertions(engine: CaplEngine) -> List[TestCaseResult]:
    """把断言按测试用例归类；没有显式用例的断言归入一个合成用例。"""
    cases = list(engine.test_cases)
    orphan = [a for a in engine.assertions if a.test_case == "(no test case)"]
    if orphan:
        synthetic = TestCaseResult(name="(no test case)", start_ms=0.0,
                                   end_ms=engine.now_ms)
        synthetic.assertions.extend(orphan)
        cases.append(synthetic)
    return cases


def to_text(engine: CaplEngine, summary: Optional[RunSummary] = None,
            trace: Optional[Sequence[str]] = None) -> str:
    """生成纯文本报告（内容完全由虚拟时间决定，因此可复现）。"""
    if summary is None:
        summary = summarize(engine)
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("can-bus-lab 脚本测试报告")
    lines.append("=" * 78)
    lines.append("脚本            : %s" % (summary.script or "<inline>"))
    lines.append("随机种子 seed   : %d" % summary.seed)
    lines.append("虚拟时间        : %.3f ms" % summary.virtual_ms)
    lines.append("测试用例        : %d" % summary.test_cases)
    lines.append("断言            : %d 通过 / %d 失败 / 共 %d"
                 % (summary.assertions_passed, summary.assertions_failed,
                    summary.assertions))
    lines.append("事件回调        : start=%s message=%s timer=%s stop=%s"
                 % (summary.stats.get("start_handlers"),
                    summary.stats.get("message_events"),
                    summary.stats.get("timer_callbacks"),
                    summary.stats.get("stop_handlers")))
    lines.append("报文            : 发送 %s 帧 / 接收 %s 帧"
                 % (summary.stats.get("frames_transmitted"),
                    summary.stats.get("frames_received")))
    lines.append("结论            : %s" % ("通过" if summary.passed else "失败"))
    lines.append("-" * 78)

    for case in _group_assertions(engine):
        status = "PASS" if case.passed else "FAIL"
        lines.append("[%s] %s  (%.3f ms)" % (status, case.name, case.duration_ms))
        for assertion in case.assertions:
            lines.append("    %-4s %-16s %s  @%.3fms"
                         % ("ok" if assertion.passed else "FAIL",
                            assertion.kind, assertion.message, assertion.virtual_ms))
    if not engine.test_cases and not engine.assertions:
        lines.append("(脚本没有调用任何测试原语)")

    if summary.errors:
        lines.append("-" * 78)
        lines.append("运行期错误:")
        for error in summary.errors:
            lines.append("  ! %s" % error)

    if engine.log_lines:
        lines.append("-" * 78)
        lines.append("脚本输出 (write/writeLine):")
        for line in engine.log_lines[-40:]:
            lines.append("  | %s" % line)

    if trace:
        lines.append("-" * 78)
        lines.append("总线 trace (前 40 条):")
        for entry in trace[:40]:
            lines.append("  %s" % entry)
    lines.append("=" * 78)
    return "\n".join(lines) + "\n"


def to_junit_xml(engine: CaplEngine, summary: Optional[RunSummary] = None,
                 suite_name: str = "canlab") -> str:
    """生成 JUnit XML 报告。

    结构：``testsuites > testsuite > testcase(+failure)``。
    耗时字段 ``time`` 使用**虚拟秒**（虚拟毫秒 / 1000），以保证 CI 比较时稳定。
    """
    if summary is None:
        summary = summarize(engine)
    cases = _group_assertions(engine)

    testsuites = ET.Element("testsuites")
    testsuites.set("name", suite_name)
    testsuites.set("tests", str(len(cases)))
    failures = sum(1 for case in cases if not case.passed)
    testsuites.set("failures", str(failures))
    testsuites.set("errors", str(len(summary.errors)))

    suite = ET.SubElement(testsuites, "testsuite")
    suite.set("name", summary.script or suite_name)
    suite.set("tests", str(len(cases)))
    suite.set("failures", str(failures))
    suite.set("errors", str(len(summary.errors)))
    suite.set("time", "%.3f" % (summary.virtual_ms / 1000.0))
    suite.set("timestamp", "virtual")
    suite.set(
        "hostname", "can-bus-lab",
    )
    properties = ET.SubElement(suite, "properties")
    for key in ("seed", "virtual_ms", "assertions", "assertions_passed",
                "assertions_failed", "frames_transmitted", "frames_received",
                "timer_callbacks", "message_events", "events"):
        prop = ET.SubElement(properties, "property")
        prop.set("name", str(key))
        prop.set("value", str(summary.stats.get(key, getattr(summary, key, ""))))

    for case in cases:
        testcase = ET.SubElement(suite, "testcase")
        testcase.set("name", case.name)
        testcase.set("classname", suite_name)
        testcase.set("time", "%.3f" % (case.duration_ms / 1000.0))
        for assertion in case.failures:
            failure = ET.SubElement(testcase, "failure")
            failure.set("message", assertion.message)
            failure.set("type", assertion.kind)
            failure.text = ("断言 %s 在虚拟时刻 %.3f ms 失败：%s"
                            % (assertion.kind, assertion.virtual_ms, assertion.message))

    for error in summary.errors:
        testcase = ET.SubElement(suite, "testcase")
        testcase.set("name", "runtime-error")
        testcase.set("classname", suite_name)
        testcase.set("time", "0.000")
        failure = ET.SubElement(testcase, "error")
        failure.set("message", "脚本运行期错误")
        failure.text = error

    raw = ET.tostring(testsuites, encoding="utf-8")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")
    return pretty.decode("utf-8")


def write_report(path: str, content: str) -> None:
    """把报告写到文件（UTF-8、LF 换行，保证跨平台一致）。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def write_junit(path: str, engine: CaplEngine,
                summary: Optional[RunSummary] = None,
                suite_name: str = "canlab") -> None:
    """生成并写出 JUnit XML 报告。"""
    write_report(path, to_junit_xml(engine, summary, suite_name))
