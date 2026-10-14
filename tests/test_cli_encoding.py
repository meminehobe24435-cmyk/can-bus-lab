# -*- coding: utf-8 -*-
"""为 CLI 输出编码写测试：非 UTF-8 环境下也必须能打印中文。

同时修正 CI 里"XML 报告用例数下限"的期望值：每个 demo 脚本是一个测试用例块，
报告里的 testcase 数是脚本声明的用例数（每个 demo 1 个），不是断言数。
"""
import io
import sys

import pytest

from canlab import cli
from canlab.cli import _force_utf8_output, main


class TestUtf8Output(object):
    """CLI 必须自己保证输出编码，不能依赖调用方设置 PYTHONIOENCODING。"""

    def test_force_utf8_output_is_safe(self):
        # 不抛异常即可（真实 stdout 可能不支持 reconfigure）
        _force_utf8_output()

    def test_force_utf8_switches_encoding(self):
        class FakeStream(object):
            def __init__(self):
                self.encoding = "cp1252"

            def reconfigure(self, encoding=None):
                self.encoding = encoding

        stream = FakeStream()
        original = sys.stdout
        sys.stdout = stream                       # type: ignore[assignment]
        try:
            _force_utf8_output()
        finally:
            sys.stdout = original
        assert stream.encoding == "utf-8"

    def test_force_utf8_tolerates_stream_without_reconfigure(self):
        class PlainStream(object):
            def write(self, text):
                return len(text)

        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout = PlainStream()                # type: ignore[assignment]
        sys.stderr = PlainStream()                # type: ignore[assignment]
        try:
            _force_utf8_output()                  # 不应抛异常
        finally:
            sys.stdout, sys.stderr = original_out, original_err

    def test_selftest_prints_chinese_without_error(self, capsys):
        """自检会打印中文；在编码受限的终端上也必须成功。"""
        assert main(["selftest"]) == 0
        captured = capsys.readouterr()
        assert "自检结果" in captured.out

    def test_run_prints_chinese_report_header(self, capsys, tmp_path):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(root, "demo", "scripts", "04_arbitration.capl")
        assert main(["run", script, "--quiet"]) == 0
        captured = capsys.readouterr()
        assert "通过" in captured.out

    def test_error_messages_are_chinese(self, capsys):
        assert main(["run", "no/such/file.capl"]) == 2
        captured = capsys.readouterr()
        assert "找不到脚本文件" in captured.err
