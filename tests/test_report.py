# -*- coding: utf-8 -*-
"""报告生成测试：JUnit XML 与文本报告。"""

import io
import os
import xml.etree.ElementTree as ET

import pytest

from canlab.bus import CanBus
from canlab.capl import CaplEngine
from canlab.report import (
    RunSummary, summarize, to_junit_xml, to_text, write_junit, write_report,
)


def engine_from(source, seed=1):
    engine = CaplEngine(bus=CanBus(), node_name="T", seed=seed)
    engine.load_source(source)
    engine.run(max_virtual_ms=1000)
    return engine


PASSING = ('on start { testCaseBegin("ok-case"); testAssertTrue(1, "a"); '
           'testAssertEquals(2, 2, "b"); testCaseEnd(); }')

FAILING = ('on start { testCaseBegin("bad-case"); testAssertTrue(1, "a"); '
           'testAssertEquals(2, 3, "b"); testCaseEnd(); }')


# ---------------------------------------------------------------------------
# RunSummary
# ---------------------------------------------------------------------------


class TestRunSummary(object):

    def test_summarize_fields(self):
        engine = engine_from(PASSING)
        summary = summarize(engine, "x.capl")
        assert summary.script == "x.capl"
        assert summary.assertions == 2
        assert summary.assertions_passed == 2
        assert summary.assertions_failed == 0

    def test_summary_passed_true(self):
        assert summarize(engine_from(PASSING)).passed

    def test_summary_passed_false_on_failure(self):
        assert not summarize(engine_from(FAILING)).passed

    def test_summary_passed_false_on_runtime_error(self):
        engine = engine_from("variables { int a; } on start { a = 1 / 0; }")
        assert not summarize(engine).passed

    def test_summary_seed_recorded(self):
        engine = CaplEngine(bus=CanBus(), node_name="T", seed=42)
        engine.load_source(PASSING)
        engine.run(max_virtual_ms=10)
        assert summarize(engine).seed == 42

    def test_summary_virtual_time(self):
        engine = engine_from("on start { testWaitForTimeout(25); }")
        assert summarize(engine).virtual_ms == pytest.approx(25.0)

    def test_summary_test_case_count(self):
        engine = engine_from('on start { testCaseBegin("a"); testCaseEnd(); '
                             'testCaseBegin("b"); testCaseEnd(); }')
        assert summarize(engine).test_cases == 2

    def test_summary_stats_snapshot(self):
        engine = engine_from(PASSING)
        summary = summarize(engine)
        assert "frames_transmitted" in summary.stats


# ---------------------------------------------------------------------------
# 文本报告
# ---------------------------------------------------------------------------


class TestTextReport(object):

    def test_text_contains_script_name(self):
        engine = engine_from(PASSING)
        text = to_text(engine, summarize(engine, "demo.capl"))
        assert "demo.capl" in text

    def test_text_contains_pass_marker(self):
        engine = engine_from(PASSING)
        text = to_text(engine)
        assert "PASS" in text

    def test_text_contains_fail_marker(self):
        engine = engine_from(FAILING)
        text = to_text(engine)
        assert "FAIL" in text

    def test_text_reports_assertion_counts(self):
        engine = engine_from(FAILING)
        text = to_text(engine)
        assert "1 通过" in text and "1 失败" in text

    def test_text_reports_virtual_time(self):
        engine = engine_from("on start { testWaitForTimeout(12); }")
        assert "12.000 ms" in to_text(engine)

    def test_text_reports_runtime_errors(self):
        engine = engine_from("variables { int a; } on start { a = 1 / 0; }")
        text = to_text(engine)
        assert "运行期错误" in text

    def test_text_includes_log_lines(self):
        engine = engine_from('on start { writeLine("hello"); }')
        assert "hello" in to_text(engine)

    def test_text_includes_trace_when_given(self):
        engine = engine_from(PASSING)
        text = to_text(engine, trace=["frame-1", "frame-2"])
        assert "frame-1" in text

    def test_text_ends_with_newline(self):
        assert to_text(engine_from(PASSING)).endswith("\n")

    def test_text_reports_conclusion(self):
        assert "通过" in to_text(engine_from(PASSING))
        assert "失败" in to_text(engine_from(FAILING))

    def test_text_handles_no_assertions(self):
        engine = engine_from("on start { }")
        assert "没有任何测试原语" in to_text(engine) or "脚本没有调用" in to_text(engine)


# ---------------------------------------------------------------------------
# JUnit XML
# ---------------------------------------------------------------------------


class TestJunitXml(object):

    def test_xml_is_parseable(self):
        xml = to_junit_xml(engine_from(PASSING))
        root = ET.fromstring(xml)
        assert root.tag == "testsuites"

    def test_xml_counts_tests(self):
        xml = to_junit_xml(engine_from(PASSING))
        root = ET.fromstring(xml)
        assert root.get("tests") == "1"
        assert root.get("failures") == "0"

    def test_xml_reports_failure(self):
        xml = to_junit_xml(engine_from(FAILING))
        root = ET.fromstring(xml)
        assert root.get("failures") == "1"

    def test_xml_testcase_names(self):
        xml = to_junit_xml(engine_from(PASSING))
        root = ET.fromstring(xml)
        names = [case.get("name") for case in root.iter("testcase")]
        assert "ok-case" in names

    def test_xml_failure_text(self):
        xml = to_junit_xml(engine_from(FAILING))
        assert "bad" in xml

    def test_xml_suite_name_from_script(self):
        engine = engine_from(PASSING)
        xml = to_junit_xml(engine, summarize(engine, "my.capl"))
        assert "my.capl" in xml

    def test_xml_properties_present(self):
        xml = to_junit_xml(engine_from(PASSING))
        root = ET.fromstring(xml)
        names = [prop.get("name") for prop in root.iter("property")]
        assert "seed" in names
        assert "virtual_ms" in names

    def test_xml_time_uses_virtual_seconds(self):
        engine = engine_from("on start { testCaseBegin(\"c\"); "
                             "testWaitForTimeout(1000); testCaseEnd(); }")
        xml = to_junit_xml(engine)
        root = ET.fromstring(xml)
        suite = root.find("testsuite")
        assert float(suite.get("time")) == pytest.approx(1.0)

    def test_xml_runtime_error_element(self):
        engine = engine_from("variables { int a; } on start { a = 1 / 0; }")
        xml = to_junit_xml(engine)
        root = ET.fromstring(xml)
        assert root.find("testsuite").get("errors") == "1"

    def test_xml_deterministic(self):
        engine = engine_from(PASSING)
        first = to_junit_xml(engine, summarize(engine))
        second = to_junit_xml(engine, summarize(engine))
        assert first == second

    def test_xml_utf8_encoding(self):
        engine = engine_from('on start { testCaseBegin("中文用例"); '
                             'testAssertTrue(1, "中文断言"); testCaseEnd(); }')
        xml = to_junit_xml(engine)
        assert "中文用例" in xml


# ---------------------------------------------------------------------------
# 文件写入
# ---------------------------------------------------------------------------


class TestReportFiles(object):

    def test_write_report_creates_file(self, tmp_path):
        path = tmp_path / "out.txt"
        write_report(str(path), "hello")
        assert path.read_text(encoding="utf-8") == "hello"

    def test_write_report_creates_directories(self, tmp_path):
        path = tmp_path / "a" / "b" / "out.txt"
        write_report(str(path), "x")
        assert path.exists()

    def test_write_report_uses_lf(self, tmp_path):
        path = tmp_path / "out.txt"
        write_report(str(path), "a\nb\n")
        raw = path.read_bytes()
        assert b"\r\n" not in raw

    def test_write_junit(self, tmp_path):
        path = tmp_path / "report.xml"
        write_junit(str(path), engine_from(PASSING))
        content = path.read_text(encoding="utf-8")
        assert "testsuites" in content

    def test_written_xml_parseable(self, tmp_path):
        path = tmp_path / "report.xml"
        write_junit(str(path), engine_from(PASSING))
        ET.parse(str(path))

    def test_report_is_byte_identical_across_runs(self, tmp_path):
        """同一脚本两次运行写出的报告必须逐字节一致。"""
        source = ('variables { int i; msTimer t; } '
                  'on start { testCaseBegin("d"); setInterval(t, 5); } '
                  'on timer t { i = i + 1; testAssertInRange(jitterMs(i, 3), -3, 3, "j"); '
                  'if (i >= 5) { cancelInterval(t); testCaseEnd(); } }')
        path_a = tmp_path / "a.xml"
        path_b = tmp_path / "b.xml"
        first = CaplEngine(bus=CanBus(), node_name="T", seed=9)
        first.load_source(source)
        first.run(max_virtual_ms=2000)
        write_junit(str(path_a), first)
        second = CaplEngine(bus=CanBus(), node_name="T", seed=9)
        second.load_source(source)
        second.run(max_virtual_ms=2000)
        write_junit(str(path_b), second)
        assert path_a.read_bytes() == path_b.read_bytes()
