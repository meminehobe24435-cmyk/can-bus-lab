# -*- coding: utf-8 -*-
"""端到端测试：CLI 跑通全部 demo、报告生成、确定性校验、内置自检。"""

import hashlib
import io
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from canlab.cli import build_parser, main
from canlab.bus import CanBus
from canlab.capl import CaplEngine
from canlab.report import summarize, to_junit_xml, to_text

#: 仓库根目录（tests/ 的上一级）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: demo 脚本目录
SCRIPT_DIR = os.path.join(ROOT, "demo", "scripts")

DEMO_SCRIPTS = sorted(
    os.path.join(SCRIPT_DIR, name)
    for name in os.listdir(SCRIPT_DIR)
    if name.endswith(".capl")
)


def run_demo_in_process(path, seed=1, max_ms=60000.0):
    """在进程内跑一个 demo 脚本，返回 (engine, summary)。"""
    engine = CaplEngine(bus=CanBus(), node_name="TESTER", seed=seed)
    engine.load_file(path)
    engine.bus.on_frame = engine._on_bus_frame
    engine.run(max_virtual_ms=max_ms)
    return engine, summarize(engine, os.path.basename(path))


# ---------------------------------------------------------------------------
# demo 脚本清单
# ---------------------------------------------------------------------------


class TestDemoInventory(object):

    def test_at_least_five_scripts(self):
        assert len(DEMO_SCRIPTS) >= 5

    def test_expected_scripts_present(self):
        names = [os.path.basename(path) for path in DEMO_SCRIPTS]
        assert any("periodic" in name for name in names)
        assert any("diag" in name for name in names)
        assert any("dtc" in name for name in names)
        assert any("arbitration" in name for name in names)
        assert any("busoff" in name for name in names)

    def test_all_scripts_are_utf8(self):
        for path in DEMO_SCRIPTS:
            with io.open(path, encoding="utf-8") as handle:
                handle.read()


# ---------------------------------------------------------------------------
# 每个 demo 都能跑通且断言全过
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", DEMO_SCRIPTS, ids=lambda p: os.path.basename(p))
def test_demo_passes_in_process(path):
    engine, summary = run_demo_in_process(path)
    assert engine.errors == [], "脚本出现运行期错误: %s" % engine.errors
    assert summary.assertions > 0, "脚本没有任何断言"
    assert summary.assertions_failed == 0, "存在失败断言"
    assert summary.passed


@pytest.mark.parametrize("path", DEMO_SCRIPTS, ids=lambda p: os.path.basename(p))
def test_demo_reports_progress(path):
    engine, summary = run_demo_in_process(path)
    assert summary.virtual_ms >= 0.0
    assert summary.stats["frames_transmitted"] >= 0


@pytest.mark.parametrize("path", DEMO_SCRIPTS, ids=lambda p: os.path.basename(p))
def test_demo_junit_parseable(path):
    engine, _summary = run_demo_in_process(path)
    xml = to_junit_xml(engine, summarize(engine, os.path.basename(path)))
    root = ET.fromstring(xml)
    assert root.get("failures") == "0"


@pytest.mark.parametrize("path", DEMO_SCRIPTS, ids=lambda p: os.path.basename(p))
def test_demo_text_report_has_no_failures(path):
    engine, _summary = run_demo_in_process(path)
    text = to_text(engine, summarize(engine, os.path.basename(path)))
    assert "[FAIL]" not in text


# ---------------------------------------------------------------------------
# 确定性：同一 seed 两次运行逐字节一致
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", DEMO_SCRIPTS, ids=lambda p: os.path.basename(p))
def test_demo_deterministic_reports(path):
    """同一脚本连跑两次：(虚拟时间, 断言序列, 报告文本) 必须完全一致。"""
    first_engine, _s1 = run_demo_in_process(path)
    second_engine, _s2 = run_demo_in_process(path)

    first_text = to_text(first_engine, summarize(first_engine,
                                                 os.path.basename(path)))
    second_text = to_text(second_engine, summarize(second_engine,
                                                   os.path.basename(path)))
    assert first_text == second_text

    first_xml = to_junit_xml(first_engine, summarize(first_engine,
                                                     os.path.basename(path)))
    second_xml = to_junit_xml(second_engine, summarize(second_engine,
                                                       os.path.basename(path)))
    assert first_xml == second_xml

    assert first_engine.now_ms == second_engine.now_ms
    assert first_engine.globals == second_engine.globals


@pytest.mark.parametrize("path", DEMO_SCRIPTS, ids=lambda p: os.path.basename(p))
def test_demo_report_sha256_stable(path):
    """报告文本的 SHA256 两次一致（README 里给出的校验方式）。"""
    digests = []
    for _ in range(2):
        engine, _summary = run_demo_in_process(path)
        text = to_text(engine, summarize(engine, os.path.basename(path)))
        digests.append(hashlib.sha256(text.encode("utf-8")).hexdigest())
    assert digests[0] == digests[1]


def test_seed_changes_demo_output():
    """换 seed 会改变带随机抖动的脚本输出，证明确定性来自 seed 而不是巧合。"""
    path = [p for p in DEMO_SCRIPTS if "periodic" in os.path.basename(p)][0]
    first, _s = run_demo_in_process(path, seed=1)
    second, _s2 = run_demo_in_process(path, seed=12345)
    assert first.globals != second.globals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli(object):

    def test_parser_builds(self):
        parser = build_parser()
        assert parser is not None

    def test_no_command_prints_help(self, capsys):
        assert main([]) == 0

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as info:
            main(["--version"])
        assert info.value.code == 0

    def test_selftest_passes(self):
        assert main(["selftest"]) == 0

    @pytest.mark.parametrize("path", DEMO_SCRIPTS, ids=lambda p: os.path.basename(p))
    def test_cli_run_demo(self, path, tmp_path):
        report = tmp_path / "report.xml"
        code = main(["run", path, "--report", str(report), "--quiet"])
        assert code == 0, "CLI 对 %s 返回了 %d" % (os.path.basename(path), code)
        assert report.exists()
        ET.parse(str(report))

    @pytest.mark.parametrize("path", DEMO_SCRIPTS, ids=lambda p: os.path.basename(p))
    def test_cli_run_with_trace(self, path):
        assert main(["run", path, "--trace", "--quiet"]) == 0

    def test_cli_missing_script(self):
        assert main(["run", "no/such/file.capl"]) == 2

    def test_cli_failing_script(self, tmp_path):
        """断言失败的脚本：CLI 返回 1，但报告仍然生成。"""
        bad = tmp_path / "bad.capl"
        bad.write_text('on start { testAssertEquals(1, 2, "故意失败"); }',
                       encoding="utf-8")
        report = tmp_path / "bad.xml"
        assert main(["run", str(bad), "--report", str(report), "--quiet"]) == 1
        root = ET.parse(str(report)).getroot()
        assert root.get("failures") == "1"

    def test_cli_lex_error_script(self, tmp_path):
        bad = tmp_path / "lex.capl"
        bad.write_text('on start { writeLine("unterminated); }', encoding="utf-8")
        assert main(["run", str(bad)]) == 3

    def test_cli_parse_error_script(self, tmp_path):
        bad = tmp_path / "parse.capl"
        bad.write_text("variables { int a; } on start { if }", encoding="utf-8")
        assert main(["run", str(bad)]) == 3

    def test_cli_text_report(self, tmp_path):
        path = DEMO_SCRIPTS[0]
        out = tmp_path / "report.txt"
        assert main(["run", path, "--text-report", str(out), "--quiet"]) == 0
        assert out.read_text(encoding="utf-8").strip()

    def test_cli_dump_trace(self, tmp_path):
        path = DEMO_SCRIPTS[0]
        out = tmp_path / "trace.json"
        assert main(["run", path, "--dump-trace", str(out), "--quiet"]) == 0
        entries = json.loads(out.read_text(encoding="utf-8"))
        assert isinstance(entries, list)

    def test_cli_custom_seed(self):
        assert main(["run", DEMO_SCRIPTS[0], "--seed", "77", "--quiet"]) == 0

    def test_cli_custom_node_and_bitrate(self):
        assert main(["run", DEMO_SCRIPTS[0], "--node", "X", "--bitrate", "250000",
                     "--quiet"]) == 0

    def test_cli_replay_missing_file(self):
        assert main(["replay", "no/such.json"]) == 2

    def test_cli_replay_roundtrip(self, tmp_path):
        """先导出 trace，再重放，统计应一致。"""
        path = DEMO_SCRIPTS[0]
        trace_file = tmp_path / "trace.json"
        assert main(["run", path, "--dump-trace", str(trace_file), "--quiet"]) == 0
        stats_file = tmp_path / "stats.json"
        assert main(["replay", str(trace_file), "--json-out", str(stats_file)]) == 0
        stats = json.loads(stats_file.read_text(encoding="utf-8"))
        assert "REPLAY" in stats

    def test_cli_replay_empty_trace(self, tmp_path):
        trace_file = tmp_path / "empty.json"
        trace_file.write_text("[]", encoding="utf-8")
        assert main(["replay", str(trace_file)]) == 0

    def test_module_entry_point(self):
        """python -m canlab 可用（子进程验证，覆盖 __main__）。"""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = ROOT
        result = subprocess.run(
            [sys.executable, "-B", "-m", "canlab", "selftest"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    def test_module_run_demo(self, tmp_path):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = ROOT
        report = tmp_path / "r.xml"
        result = subprocess.run(
            [sys.executable, "-B", "-m", "canlab", "run", DEMO_SCRIPTS[0],
             "--report", str(report), "--quiet"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        assert report.exists()


# ---------------------------------------------------------------------------
# CLI 输出的字节级确定性（跨进程）
# ---------------------------------------------------------------------------


class TestCliDeterminism(object):

    @pytest.mark.parametrize("path", DEMO_SCRIPTS[:3], ids=lambda p: os.path.basename(p))
    def test_two_cli_runs_identical(self, path, tmp_path):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = ROOT
        outputs = []
        for index in range(2):
            report = tmp_path / ("r%d.xml" % index)
            result = subprocess.run(
                [sys.executable, "-B", "-m", "canlab", "run", path,
                 "--report", str(report), "--text-report", str(tmp_path / ("t%d.txt" % index))],
                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            assert result.returncode == 0
            # 注意：stdout 里包含临时路径（报告写入位置），跨进程比较时
            # 只比较与路径无关的产物 —— XML 报告与文本报告
            outputs.append((
                report.read_bytes(),
                (tmp_path / ("t%d.txt" % index)).read_bytes(),
            ))
        assert outputs[0] == outputs[1]

    @pytest.mark.parametrize("path", DEMO_SCRIPTS[:2], ids=lambda p: os.path.basename(p))
    def test_cli_stdout_body_identical(self, path, tmp_path):
        """stdout 去掉"报告已写入"那几行之后必须逐字节一致。"""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = ROOT
        bodies = []
        for index in range(2):
            result = subprocess.run(
                [sys.executable, "-B", "-m", "canlab", "run", path,
                 "--quiet", "--seed", "3"],
                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            assert result.returncode == 0
            bodies.append(result.stdout)
        assert bodies[0] == bodies[1]


# ---------------------------------------------------------------------------
# 端到端协议串联：位级 -> 总线 -> ISO-TP -> UDS
# ---------------------------------------------------------------------------


class TestFullStack(object):
    """把各层串起来：UDS 请求经 ISO-TP 分帧、经总线逐位仲裁、再被解析回来。"""

    def test_uds_over_isotp_over_bus(self):
        from canlab import isotp as I
        from canlab import uds as U
        from canlab.frame import Frame

        bus = CanBus()
        bus.add_node("TESTER")

        server = U.UdsServer(clock=lambda: 0.0)
        server.add_did(0xF190, b"VIN-0123456789")

        # 一条诊断请求：读 DID 0xF190（单帧即可承载）
        request = bytes([0x22, 0xF1, 0x90])
        assert len(request) <= I.sf_max_data_length(False)

        conn = I.IsoTpConnection(lambda payload: bus.send(
            "TESTER", I.frame_for(payload, 0x7DF)), I.IsoTpConfig(),
            lambda: bus.now_ms)
        conn.send(request)
        assert conn.tx.finished
        assert bus.stats.frames_sent == 1

        response = server.handle(request)
        assert response.data[0] == 0x62
        assert b"VIN-0123456789" in response.data

    def test_multiframe_uds_response(self):
        from canlab import isotp as I

        # 构造一条 60 字节的响应（在经典 CAN 上必须走多帧）
        payload = bytes([0x00, 0x00, 0x00, 0x00]) + bytes([0x5A]) * 56
        conn = I.IsoTpConnection(lambda p: None, I.IsoTpConfig(), lambda: 0.0)
        result = None
        for pdu in I.split_message(payload):
            got = conn.on_pdu(pdu)
            if got is not None:
                result = got
        assert result == payload

    def test_bus_arbitration_preserves_frames(self):
        bus = CanBus()
        bus.add_node("A")
        bus.add_node("B")
        bus.add_node("C")
        frames = [
            ("A", __import__("canlab.frame", fromlist=["Frame"]).Frame(
                identifier=0x600, data=b"\x01")),
            ("B", __import__("canlab.frame", fromlist=["Frame"]).Frame(
                identifier=0x300, data=b"\x02")),
            ("C", __import__("canlab.frame", fromlist=["Frame"]).Frame(
                identifier=0x100, data=b"\x03")),
        ]
        bus.contend(frames)
        identifiers = [frame.identifier for frame in bus.trace]
        assert identifiers == [0x100, 0x300, 0x600]

    def test_error_frame_corrupts_only_one_frame(self):
        bus = CanBus()
        bus.add_node("A")
        bus.add_node("B")
        from canlab.frame import Frame
        bus.inject_bit_error("A")
        bus.send("A", Frame(identifier=0x100, data=b"\x01"))
        # B 收到的帧数为 0（错误帧破坏了它）
        assert bus.node("B").rx_success == 0
        # 后面的帧正常
        bus.send("A", Frame(identifier=0x101, data=b"\x02"))
        assert bus.node("B").rx_success == 1
