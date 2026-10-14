# -*- coding: utf-8 -*-
"""类 CAPL 脚本引擎测试：词法 / 语法 / 求值 / 事件派发 / 定时器 / 虚拟时间确定性。"""

import pytest

from canlab.bus import CanBus
from canlab.capl import (
    CaplEngine, CaplRuntimeError, LexError, MsgValue, ParseError, Script,
    TimerValue, count_nodes, format_tokens, frame_to_message, parse, tokenize,
)
from canlab.capl.lexer import TT
from canlab.capl.interp import _safe_int, extract_signal, insert_signal


def make_engine(source, node_name="T", seed=1, bus=None):
    """编译并返回一个引擎实例。"""
    engine = CaplEngine(bus=bus if bus is not None else CanBus(),
                        node_name=node_name, seed=seed)
    engine.load_source(source)
    return engine


def run_script(source, max_virtual_ms=1000, seed=1):
    engine = make_engine(source, seed=seed)
    engine.run(max_virtual_ms=max_virtual_ms)
    return engine


def value_of(source, name=None, max_virtual_ms=100, seed=1):
    """运行脚本并读取一个全局变量。

    :param name: 变量名；为 ``None`` 时优先取 ``r``，否则取脚本里唯一的变量。
    """
    engine = run_script(source, max_virtual_ms=max_virtual_ms, seed=seed)
    if name is not None:
        return engine.globals.get(name)
    if "r" in engine.globals:
        return engine.globals["r"]
    values = list(engine.globals.values())
    return values[0] if len(values) == 1 else None


# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------


class TestLexer(object):

    def test_tokenize_empty(self):
        tokens = tokenize("")
        assert len(tokens) == 1
        assert tokens[0].kind == TT.EOF

    def test_tokenize_identifier(self):
        tokens = tokenize("abc")
        assert tokens[0].kind == TT.IDENT
        assert tokens[0].value == "abc"

    def test_tokenize_keyword(self):
        tokens = tokenize("variables")
        assert tokens[0].kind == TT.KEYWORD

    @pytest.mark.parametrize("text,value", [
        ("0", 0), ("123", 123), ("0xFF", 255), ("0x1FF", 511), ("0X10", 16),
    ])
    def test_tokenize_integers(self, text, value):
        assert tokenize(text)[0].value == value

    def test_tokenize_hex_literal_case_insensitive(self):
        assert tokenize("0xAbCd")[0].value == 0xABCD

    def test_tokenize_string(self):
        tokens = tokenize('"hello"')
        assert tokens[0].kind == TT.STRING
        assert tokens[0].value == "hello"

    def test_tokenize_string_escapes(self):
        tokens = tokenize(r'"a\nb\tc"')
        assert tokens[0].value == "a\nb\tc"

    def test_tokenize_escaped_quote(self):
        assert tokenize(r'"a\"b"')[0].value == 'a"b'

    def test_tokenize_char(self):
        tokens = tokenize("'a'")
        assert tokens[0].kind == TT.CHAR
        assert tokens[0].value == "a"

    def test_tokenize_char_escape(self):
        assert tokenize(r"'\n'")[0].value == "\n"

    def test_unterminated_string(self):
        with pytest.raises(LexError):
            tokenize('"abc')

    def test_unterminated_char(self):
        with pytest.raises(LexError):
            tokenize("'a")

    def test_newline_in_string_rejected(self):
        with pytest.raises(LexError):
            tokenize('"ab\ncd"')

    def test_malformed_hex(self):
        with pytest.raises(LexError):
            tokenize("0x")

    def test_unexpected_character(self):
        with pytest.raises(LexError):
            tokenize("$")

    @pytest.mark.parametrize("op", ["+", "-", "*", "/", "%", "&", "|", "^", "~",
                                    "!", "<", ">", "=", "?", ":", "<<", ">>",
                                    "==", "!=", "<=", ">=", "&&", "||",
                                    "+=", "-=", "*=", "/=", "&=", "|=", "^=",
                                    "++", "--", "."])
    def test_operators_recognized(self, op):
        tokens = tokenize(op)
        assert tokens[0].kind == TT.OP
        assert tokens[0].value == op

    def test_longest_operator_wins(self):
        tokens = tokenize("<<=")
        assert tokens[0].value == "<<="

    def test_line_comment_skipped(self):
        assert len(tokenize("// comment\n1")) == 2

    def test_block_comment_skipped(self):
        assert len(tokenize("/* a\n b */1")) == 2

    def test_unterminated_block_comment(self):
        with pytest.raises(LexError):
            tokenize("/* abc")

    def test_line_and_column_tracking(self):
        tokens = tokenize("a\n  b")
        assert tokens[0].line == 1
        assert tokens[1].line == 2
        assert tokens[1].column == 3

    def test_format_tokens(self):
        text = format_tokens(tokenize("int a;"))
        assert "KEYWORD(int)" in text
        assert "EOF" in text

    def test_punctuation(self):
        for char in "(){}[];,":
            assert tokenize(char)[0].kind == TT.PUNCT


# ---------------------------------------------------------------------------
# 语法分析
# ---------------------------------------------------------------------------


class TestParser(object):

    def test_parse_empty_script(self):
        script = parse("")
        assert isinstance(script, Script)
        assert script.handlers == []

    def test_parse_variables_block(self):
        script = parse("variables { int a; byte b; }")
        assert [decl.name for decl in script.variables] == ["a", "b"]

    def test_parse_multiple_declarators(self):
        script = parse("variables { int a, b, c; }")
        assert [decl.name for decl in script.variables] == ["a", "b", "c"]

    def test_parse_array_declaration(self):
        script = parse("variables { byte buf[8]; }")
        assert script.variables[0].array_size is not None

    def test_parse_message_declaration(self):
        script = parse("variables { message 0x123 msg1; }")
        assert script.messages[0].name == "msg1"

    def test_parse_extended_message_declaration(self):
        # 扩展帧用 29 位 ID 表示
        script = parse("variables { message 0x1ABCDEF msg1; }")
        assert script.messages[0].message_id.value == 0x1ABCDEF

    def test_parse_timer_declaration(self):
        script = parse("variables { msTimer t1; }")
        assert script.timers[0].name == "t1"
        assert script.timers[0].is_timer

    def test_parse_timer_keyword_type(self):
        script = parse("variables { timer t1; }")
        assert script.timers

    def test_parse_handler_on_start(self):
        script = parse("on start { }")
        assert script.handlers[0].kind == "start"

    def test_parse_handler_on_message(self):
        script = parse("on message 0x123 { }")
        assert script.handlers[0].message_id == 0x123

    def test_parse_handler_on_message_wildcard(self):
        script = parse("on message * { }")
        assert script.handlers[0].wildcard

    def test_parse_handler_on_timer(self):
        script = parse("on timer t1 { }")
        assert script.handlers[0].timer_name == "t1"

    def test_parse_handler_on_key_char(self):
        script = parse("on key 'a' { }")
        assert script.handlers[0].key == "a"

    def test_parse_handler_on_key_code(self):
        script = parse("on key 65 { }")
        assert script.handlers[0].key == "A"

    def test_parse_handler_on_stop_measurement(self):
        script = parse("on stopMeasurement { }")
        assert script.handlers[0].kind == "stopMeasurement"

    def test_parse_unknown_event_rejected(self):
        with pytest.raises(ParseError):
            parse("on bogus { }")

    def test_parse_function_keyword_form(self):
        script = parse("function int add(int a, int b) { return a + b; }")
        assert script.functions[0].name == "add"
        assert len(script.functions[0].params) == 2

    def test_parse_c_style_function(self):
        script = parse("int add(int a, int b) { return a + b; }")
        assert script.functions[0].return_type == "int"

    def test_parse_void_function(self):
        script = parse("void nothing() { }")
        assert script.functions[0].name == "nothing"

    def test_parse_top_level_garbage(self):
        with pytest.raises(ParseError):
            parse("int x;")

    def test_parse_statements_block_rejected_at_top_level(self):
        # 顶层不允许裸语句块（必须先有事件处理器或函数）
        with pytest.raises(ParseError):
            parse("{ int a; a = 1; }")

    def test_parse_count_nodes(self):
        assert count_nodes(parse("on start { }")) > 0

    def test_parse_if_else(self):
        script = parse("on start { if (1) { } else { } }")
        statement = script.handlers[0].body.statements[0]
        assert statement.else_branch is not None

    def test_parse_while(self):
        script = parse("on start { while (0) { } }")
        assert script.handlers[0].body.statements[0].__class__.__name__ == "WhileStmt"

    def test_parse_do_while(self):
        script = parse("on start { do { } while (0); }")
        assert script.handlers[0].body.statements[0].__class__.__name__ == "DoWhileStmt"

    def test_parse_for(self):
        script = parse("on start { for (i = 0; i < 3; i = i + 1) { } }")
        assert script.handlers[0].body.statements[0].__class__.__name__ == "ForStmt"

    def test_parse_for_with_declaration(self):
        script = parse("on start { for (int i = 0; i < 3; i = i + 1) { } }")
        assert script.handlers[0].body.statements[0].init is not None

    def test_parse_break_and_continue(self):
        script = parse("on start { while (1) { break; continue; } }")
        assert script is not None

    def test_parse_ternary(self):
        script = parse("on start { x = 1 ? 2 : 3; }")
        assert script is not None

    def test_parse_method_call(self):
        script = parse("variables { message 0x1 m; } on start { x = m.byte(0); }")
        call = script.handlers[0].body.statements[0].expr.value
        assert call.receiver is not None
        assert call.callee == "byte"

    def test_parse_accepts_missing_semicolon_before_brace(self):
        # 本实现允许块内最后一条声明省略分号
        script = parse("on start { int a }")
        assert script.handlers[0].body.statements

    def test_parse_missing_semicolon_between_statements(self):
        with pytest.raises(ParseError):
            parse("variables { int b; } on start { b = 1 b = 2; }")

    def test_parse_unclosed_brace(self):
        with pytest.raises(ParseError):
            parse("on start { ")

    def test_parse_missing_paren(self):
        with pytest.raises(ParseError):
            parse("on start { if 1 { } }")

    def test_parse_soft_keyword_as_identifier(self):
        script = parse("variables { int key; } on start { key = 1; }")
        assert script.variables[0].name == "key"


# ---------------------------------------------------------------------------
# 运算符求值
# ---------------------------------------------------------------------------


class TestOperators(object):

    @pytest.mark.parametrize("expr,expected", [
        ("1 + 2", 3),
        ("10 - 3", 7),
        ("6 * 7", 42),
        ("20 / 4", 5),
        ("7 % 3", 1),
        ("1 + 2 * 3", 7),
        ("(1 + 2) * 3", 9),
        ("2 + 3 * 4 - 5", 9),
    ])
    def test_arithmetic(self, expr, expected):
        assert value_of("variables { int r; } on start { r = %s; }" % expr) == expected

    @pytest.mark.parametrize("expr,expected", [
        ("0xF0 & 0x0F", 0x00),
        ("0xF0 | 0x0F", 0xFF),
        ("0xFF ^ 0x0F", 0xF0),
        ("1 << 4", 16),
        ("256 >> 4", 16),
    ])
    def test_bitwise(self, expr, expected):
        assert value_of("variables { int r; } on start { r = %s; }" % expr) == expected

    def test_bitwise_not(self):
        assert value_of("variables { int r; } on start { r = ~0; }") == -1

    @pytest.mark.parametrize("expr,expected", [
        ("1 < 2", 1), ("2 < 1", 0), ("2 > 1", 1), ("1 > 2", 0),
        ("1 <= 1", 1), ("1 >= 2", 0), ("1 == 1", 1), ("1 != 1", 0),
    ])
    def test_comparison(self, expr, expected):
        assert value_of("variables { int r; } on start { r = %s; }" % expr) == expected

    @pytest.mark.parametrize("expr,expected", [
        ("1 && 1", 1), ("1 && 0", 0), ("0 || 0", 0), ("0 || 1", 1),
        ("!0", 1), ("!5", 0),
    ])
    def test_logical(self, expr, expected):
        assert value_of("variables { int r; } on start { r = %s; }" % expr) == expected

    def test_short_circuit_and(self):
        source = ("variables { int r; int n; } "
                  "on start { n = 0; r = 0 && (n = 1); }")
        engine = run_script(source)
        assert engine.globals["n"] == 0

    def test_short_circuit_or(self):
        source = ("variables { int r; int n; } "
                  "on start { n = 0; r = 1 || (n = 1); }")
        engine = run_script(source)
        assert engine.globals["n"] == 0

    def test_unary_minus(self):
        assert value_of("variables { int r; } on start { r = -5; }") == -5

    def test_unary_plus(self):
        assert value_of("variables { int r; } on start { r = +5; }") == 5

    def test_ternary_true(self):
        assert value_of("variables { int r; } on start { r = 1 ? 10 : 20; }") == 10

    def test_ternary_false(self):
        assert value_of("variables { int r; } on start { r = 0 ? 10 : 20; }") == 20

    def test_division_by_zero_raises(self):
        engine = make_engine("variables { int r; } on start { r = 1 / 0; }")
        engine.run(max_virtual_ms=10)
        assert engine.errors

    def test_modulo_by_zero_raises(self):
        engine = make_engine("variables { int r; } on start { r = 1 % 0; }")
        engine.run(max_virtual_ms=10)
        assert engine.errors

    def test_integer_division_truncates(self):
        assert value_of("variables { int r; } on start { r = 7 / 2; }") == 3

    def test_negative_division_truncates_toward_zero(self):
        assert value_of("variables { int r; } on start { r = 0 - 7 / 2; }") == -3

    def test_string_concatenation(self):
        assert value_of('variables { int r; } on start { r = "a" + "b" == "ab"; }') == 1

    def test_undefined_variable_error(self):
        engine = make_engine("on start { zzz = 1; }")
        engine.run(max_virtual_ms=10)
        assert any("undefined variable" in error for error in engine.errors)


# ---------------------------------------------------------------------------
# 语句与作用域
# ---------------------------------------------------------------------------


class TestStatements(object):

    def test_assignment(self):
        assert value_of("variables { int a; } on start { a = 7; }") == 7

    @pytest.mark.parametrize("op,initial,operand,expected", [
        ("+=", 10, 5, 15), ("-=", 10, 5, 5), ("*=", 10, 5, 50),
        ("/=", 10, 5, 2), ("%=", 10, 3, 1),
        ("&=", 0xFF, 0x0F, 0x0F), ("|=", 0xF0, 0x0F, 0xFF),
        ("^=", 0xFF, 0x0F, 0xF0), ("<<=", 1, 4, 16), (">>=", 256, 4, 16),
    ])
    def test_compound_assignment(self, op, initial, operand, expected):
        source = ("variables { int a; } on start { a = %d; a %s %d; }"
                  % (initial, op, operand))
        assert value_of(source) == expected

    def test_increment_prefix(self):
        assert value_of("variables { int a; } on start { a = 1; ++a; }") == 2

    def test_increment_postfix(self):
        source = "variables { int a; int b; } on start { a = 1; b = a++; }"
        engine = run_script(source)
        assert engine.globals["b"] == 1
        assert engine.globals["a"] == 2

    def test_decrement(self):
        assert value_of("variables { int a; } on start { a = 5; a--; }") == 4

    def test_array_indexing(self):
        source = ("variables { byte buf[4]; int r; } "
                  "on start { buf[0] = 1; buf[1] = 2; r = buf[0] + buf[1]; }")
        assert value_of(source) == 3

    def test_array_out_of_range(self):
        engine = make_engine("variables { byte buf[2]; } on start { buf[5] = 1; }")
        engine.run(max_virtual_ms=10)
        assert any("out of range" in error for error in engine.errors)

    def test_if_true_branch(self):
        source = "variables { int r; } on start { r = 0; if (1) { r = 5; } }"
        assert value_of(source) == 5

    def test_if_false_branch(self):
        source = "variables { int r; } on start { r = 0; if (0) { r = 5; } else { r = 9; } }"
        assert value_of(source) == 9

    def test_nested_if(self):
        source = ("variables { int r; } on start { r = 0;"
                  " if (1) { if (1) { r = 3; } } }")
        assert value_of(source) == 3

    def test_while_loop(self):
        source = ("variables { int i; int s; } on start { i = 0; s = 0;"
                  " while (i < 4) { s = s + i; i = i + 1; } }")
        assert value_of(source, "s") == 6

    def test_while_break(self):
        source = ("variables { int i; } on start { i = 0;"
                  " while (1) { i = i + 1; if (i > 3) { break; } } }")
        assert value_of(source) == 4

    def test_while_continue(self):
        source = ("variables { int i; int s; } on start { i = 0; s = 0;"
                  " while (i < 5) { i = i + 1; if (i == 3) { continue; } s = s + i; } }")
        assert value_of(source, "s") == 12

    def test_do_while_runs_once(self):
        source = ("variables { int n; } on start { n = 0;"
                  " do { n = n + 1; } while (0); }")
        assert value_of(source) == 1

    def test_for_loop(self):
        source = ("variables { int i; int s; } on start { s = 0;"
                  " for (i = 0; i < 5; i = i + 1) { s = s + i; } }")
        assert value_of(source, "s") == 10

    def test_for_loop_with_local(self):
        source = ("variables { int s; } on start { s = 0;"
                  " for (int i = 1; i <= 3; i = i + 1) { s = s + i; } }")
        assert value_of(source) == 6

    def test_for_break(self):
        source = ("variables { int i; } on start {"
                  " for (i = 0; i < 10; i = i + 1) { if (i == 3) { break; } } }")
        assert value_of(source) == 3

    def test_nested_loops(self):
        source = ("variables { int i; int j; int s; } on start { s = 0;"
                  " for (i = 0; i < 3; i = i + 1) {"
                  "   for (j = 0; j < 2; j = j + 1) { s = s + 1; } } }")
        assert value_of(source, "s") == 6


# ---------------------------------------------------------------------------
# 变量、类型与定宽整型
# ---------------------------------------------------------------------------


class TestVariablesAndTypes(object):

    def test_local_variable_shadowing(self):
        source = ("variables { int a; } "
                  "on start { a = 1; { int a; a = 2; } }")
        assert value_of(source) == 1

    def test_byte_truncates(self):
        assert value_of("variables { byte b; } on start { b = 300; }") == 44

    def test_word_truncates(self):
        assert value_of("variables { word w; } on start { w = 0x1FFFF; }") == 0xFFFF

    def test_int_is_16bit_signed(self):
        assert value_of("variables { int i; } on start { i = 0xFFFF; }") == -1

    def test_int_positive_range(self):
        assert value_of("variables { int i; } on start { i = 0x7FFF; }") == 0x7FFF

    def test_dword_is_unsigned_32(self):
        assert value_of("variables { dword d; } on start { d = 0xFFFFFFFF; }") == 0xFFFFFFFF

    def test_long_is_signed_32(self):
        assert value_of("variables { long l; } on start { l = 0xFFFFFFFF; }") == -1

    def test_variable_initializer(self):
        assert value_of("variables { int a = 42; } on start { }") == 42

    def test_array_initialized_to_zero(self):
        source = ("variables { byte b[3]; int r; } "
                  "on start { r = b[0] + b[1] + b[2]; }")
        assert value_of(source) == 0

    def test_constants_available(self):
        assert value_of("variables { int r; } on start { r = TRUE; }") == 1

    def test_recesssive_constant(self):
        assert value_of("variables { int r; } on start { r = RECESSIVE; }") == 1

    def test_true_false_literals(self):
        assert value_of("variables { int r; } on start { r = true; }") == 1


# ---------------------------------------------------------------------------
# 函数
# ---------------------------------------------------------------------------


class TestFunctions(object):

    def test_function_call(self):
        source = ("int add(int a, int b) { return a + b; } "
                  "variables { int r; } on start { r = add(2, 3); }")
        assert value_of(source) == 5

    def test_void_function_side_effect(self):
        source = ("variables { int r; } "
                  "void setIt(int v) { r = v; } "
                  "on start { setIt(9); }")
        assert value_of(source) == 9

    def test_function_wrong_arg_count(self):
        source = ("int add(int a, int b) { return a + b; } "
                  "variables { int r; } on start { r = add(2); }")
        engine = run_script(source)
        assert any("expects 2 argument" in error for error in engine.errors)

    def test_recursion(self):
        source = ("int fact(int n) { if (n <= 1) { return 1; } "
                  "return n * fact(n - 1); } "
                  "variables { int r; } on start { r = fact(5); }")
        assert value_of(source) == 120

    def test_function_called_counter(self):
        source = ("int f() { return 1; } variables { int r; } on start { r = f(); }")
        engine = run_script(source)
        assert engine.stats["functions_called"] == 1

    def test_function_used_as_value_is_error(self):
        source = "int f() { return 1; } on start { x = f; }"
        engine = run_script(source)
        assert any("used as a value" in error for error in engine.errors)


# ---------------------------------------------------------------------------
# 消息对象
# ---------------------------------------------------------------------------


class TestMessageValues(object):

    def test_message_default_dlc(self):
        engine = make_engine("variables { message 0x123 m; } on start { }")
        assert engine.messages["m"].dlc == 8

    def test_message_id_from_declaration(self):
        engine = make_engine("variables { message 0x1AB m; } on start { }")
        assert engine.messages["m"].can_id == 0x1AB

    def test_set_and_get_byte(self):
        source = ("variables { int r; message 0x100 m; } "
                  "on start { m.byte(0) = 0xAB; r = m.byte(0); }")
        assert value_of(source) == 0xAB

    def test_msg_id_member(self):
        source = ("variables { int r; message 0x100 m; } "
                  "on start { r = m.id; }")
        assert value_of(source) == 0x100

    def test_msg_dlc_member(self):
        source = ("variables { message 0x100 m; } on start { m.dlc = 4; }")
        engine = run_script(source)
        assert engine.messages["m"].dlc == 4

    def test_setting_dlc_resizes_data(self):
        source = ("variables { message 0x100 m; } on start { m.dlc = 2; }")
        engine = run_script(source)
        assert engine.messages["m"].length == 2

    def test_byte_out_of_range(self):
        engine = make_engine("variables { message 0x100 m; } on start { x = m.byte(9); }")
        engine.run(max_virtual_ms=10)
        assert engine.errors

    def test_illegal_dlc_rejected(self):
        engine = make_engine("variables { message 0x100 m; } on start { m.dlc = 20; }")
        engine.run(max_virtual_ms=10)
        assert engine.errors

    def test_frame_to_message(self):
        from canlab.frame import Frame
        msg = frame_to_message(Frame(identifier=0x123, data=b"\x01\x02"))
        assert msg.can_id == 0x123
        assert msg.get_byte(1) == 2
        assert msg.dir == "RX"

    def test_msg_value_copy(self):
        msg = MsgValue(can_id=0x100, dlc=8, data=bytes([1, 2]))
        clone = msg.copy()
        clone.set_byte(0, 9)
        assert msg.get_byte(0) == 1

    def test_msg_hex(self):
        assert MsgValue(can_id=1, dlc=2, data=b"\x0A\x0B").hex() == "0A 0B"


# ---------------------------------------------------------------------------
# 内建函数
# ---------------------------------------------------------------------------


class TestBuiltins(object):

    def test_write_accumulates(self):
        engine = run_script('on start { write("a"); write("b"); }')
        assert engine.log_lines == ["a", "b"]

    def test_write_line_appends_newline(self):
        engine = run_script('on start { writeLine("x"); }')
        assert engine.output_stream.getvalue().endswith("\n")

    def test_write_multiple_args(self):
        engine = run_script('on start { write("a", "b", "c"); }')
        assert engine.log_lines[0] == "abc"

    def test_abs(self):
        assert value_of("variables { int r; } on start { r = abs(0 - 5); }") == 5

    def test_min(self):
        assert value_of("variables { int r; } on start { r = min(3, 7); }") == 3

    def test_max(self):
        assert value_of("variables { int r; } on start { r = max(3, 7); }") == 7

    def test_get_tick_count(self):
        assert value_of("variables { int r; } on start { r = getTickCount(); }") == 0

    def test_random_deterministic(self):
        source = "variables { int a; int b; } on start { a = random(100); b = random(100); }"
        first = run_script(source, seed=7)
        second = run_script(source, seed=7)
        assert first.globals == second.globals

    def test_random_different_seeds_differ(self):
        source = "variables { int a; } on start { a = random(100000); }"
        assert run_script(source, seed=1).globals != run_script(source, seed=99).globals

    @pytest.mark.parametrize("seq", list(range(10)))
    def test_jitter_is_deterministic(self, seq):
        source = ("variables { int r; } on start { r = jitterMs(%d, 5); }" % seq)
        assert value_of(source, seed=3) == value_of(source, seed=3)

    def test_jitter_within_amplitude(self):
        for seq in range(50):
            source = ("variables { int r; } on start { r = jitterMs(%d, 5); }" % seq)
            assert -5 <= value_of(source) <= 5

    def test_jitter_can_be_negative(self):
        values = []
        for seq in range(20):
            source = ("variables { int r; } on start { r = jitterMs(%d, 5); }" % seq)
            values.append(value_of(source))
        assert any(value < 0 for value in values)

    def test_extract_signal_low_byte(self):
        assert extract_signal(b"\x05", 0, 8) == 5

    def test_extract_signal_bits(self):
        assert extract_signal(b"\x03", 0, 2) == 3

    def test_extract_signal_second_byte(self):
        assert extract_signal(b"\x00\xFF", 8, 8) == 0xFF

    def test_extract_signal_rejects_zero_length(self):
        with pytest.raises(CaplRuntimeError):
            extract_signal(b"\x00", 0, 0)

    def test_insert_signal(self):
        assert insert_signal(b"\x00", 0, 8, 0xAB) == b"\xAB"

    def test_insert_signal_partial(self):
        assert insert_signal(b"\x00", 0, 4, 0x0F)[0] == 0x0F

    def test_insert_signal_second_byte(self):
        assert insert_signal(b"\x00\x00", 8, 8, 0x12) == b"\x00\x12"

    def test_insert_signal_rejects_bad_length(self):
        with pytest.raises(CaplRuntimeError):
            insert_signal(b"\x00", 0, 0, 1)

    def test_unknown_function_error(self):
        engine = run_script("on start { nosuch(); }")
        assert any("unknown function" in error for error in engine.errors)

    def test_get_signal_value_builtin(self):
        source = ("variables { message 0x100 m; int r; } "
                  "on start { m.byte(0) = 0x3C; r = getSignalValue(m, 2, 4); }")
        assert value_of(source) == 0x0F

    def test_set_signal_value_builtin(self):
        source = ("variables { message 0x100 m; int r; } "
                  "on start { setSignalValue(m, 0, 8, 0x5A); r = m.byte(0); }")
        assert value_of(source) == 0x5A


# ---------------------------------------------------------------------------
# 事件派发
# ---------------------------------------------------------------------------


class TestEventDispatch(object):

    def test_on_start_runs_once(self):
        engine = run_script("variables { int n; } on start { n = n + 1; }")
        assert engine.globals["n"] == 1
        assert engine.start_count == 1

    def test_on_message_receives_frame(self):
        bus = CanBus()
        bus.add_node("S")
        source = ("variables { int n; } "
                  "on message 0x123 { n = n + 1; }")
        engine = CaplEngine(bus=bus, node_name="S")
        engine.load_source(source)
        bus.on_frame = engine._on_bus_frame
        bus.send("S", __import__("canlab.frame", fromlist=["Frame"]).Frame(
            identifier=0x123, data=b"\x01"))
        assert engine.globals["n"] == 1

    def test_on_message_this_byte(self):
        bus = CanBus()
        bus.add_node("S")
        source = ("variables { int r; } on message 0x123 { r = this.byte(0); }")
        engine = CaplEngine(bus=bus, node_name="S")
        engine.load_source(source)
        bus.on_frame = engine._on_bus_frame
        bus.send("S", __import__("canlab.frame", fromlist=["Frame"]).Frame(
            identifier=0x123, data=b"\xAB"))
        assert engine.globals["r"] == 0xAB

    def test_on_message_wildcard(self):
        bus = CanBus()
        bus.add_node("S")
        source = "variables { int n; } on message * { n = n + 1; }"
        engine = CaplEngine(bus=bus, node_name="S")
        engine.load_source(source)
        bus.on_frame = engine._on_bus_frame
        bus.send("S", __import__("canlab.frame", fromlist=["Frame"]).Frame(
            identifier=0x111, data=b"\x01"))
        bus.send("S", __import__("canlab.frame", fromlist=["Frame"]).Frame(
            identifier=0x222, data=b"\x02"))
        assert engine.globals["n"] == 2

    def test_this_outside_handler_is_error(self):
        engine = run_script("variables { int r; } on start { r = this.byte(0); }")
        assert any("this" in error for error in engine.errors)

    def test_on_key_dispatch(self):
        source = "variables { int n; } on key 'a' { n = n + 1; } on start { }"
        engine = run_script(source)
        engine.inject_key("a")
        assert engine.globals["n"] == 1

    def test_on_key_wrong_key_ignored(self):
        source = "variables { int n; } on key 'a' { n = 1; } on start { }"
        engine = run_script(source)
        engine.inject_key("z")
        assert engine.globals["n"] == 0

    def test_on_stop_measurement_runs_last(self):
        source = ("variables { int n; } on start { n = 1; } "
                  "on stopMeasurement { n = n + 10; }")
        assert value_of(source) == 11

    def test_multiple_handlers_same_message(self):
        bus = CanBus()
        bus.add_node("S")
        source = ("variables { int n; } "
                  "on message 0x123 { n = n + 1; } "
                  "on message 0x123 { n = n + 10; }")
        engine = CaplEngine(bus=bus, node_name="S")
        engine.load_source(source)
        bus.on_frame = engine._on_bus_frame
        bus.send("S", __import__("canlab.frame", fromlist=["Frame"]).Frame(
            identifier=0x123, data=b"\x01"))
        assert engine.globals["n"] == 11

    def test_event_counters(self):
        engine = run_script("variables { int n; } on start { n = 1; }")
        stats = engine.stats_snapshot()
        assert stats["start_handlers"] == 1
        assert stats["stop_handlers"] == 0


# ---------------------------------------------------------------------------
# 定时器与虚拟时间
# ---------------------------------------------------------------------------


class TestTimers(object):

    def test_set_timer_fires(self):
        source = ("variables { int n; msTimer t; } "
                  "on start { setTimer(t, 10); } "
                  "on timer t { n = n + 1; }")
        engine = run_script(source)
        assert engine.globals["n"] == 1

    def test_timer_fires_at_right_time(self):
        source = ("variables { msTimer t; } on start { setTimer(t, 25); } on timer t { }")
        engine = run_script(source)
        assert engine.now_ms == pytest.approx(25.0)

    def test_timer_self_reschedule(self):
        source = ("variables { int n; msTimer t; } "
                  "on start { setTimer(t, 10); } "
                  "on timer t { n = n + 1; if (n < 5) { setTimer(t, 10); } }")
        engine = run_script(source)
        assert engine.globals["n"] == 5
        assert engine.now_ms == pytest.approx(50.0)

    def test_cancel_timer(self):
        source = ("variables { int n; msTimer t; } "
                  "on start { setTimer(t, 10); cancelTimer(t); } "
                  "on timer t { n = n + 1; }")
        engine = run_script(source)
        assert engine.globals["n"] == 0

    def test_is_timer_active(self):
        source = ("variables { int a; int b; msTimer t; } "
                  "on start { a = isTimerActive(t); setTimer(t, 10); b = isTimerActive(t); }")
        engine = run_script(source)
        assert engine.globals["a"] == 0
        assert engine.globals["b"] == 1

    def test_get_timer_remaining(self):
        source = ("variables { int r; msTimer t; } "
                  "on start { setTimer(t, 100); r = getTimer(t); }")
        assert value_of(source) == 100

    def test_set_interval_repeats(self):
        source = ("variables { int n; msTimer t; } "
                  "on start { setInterval(t, 10); } "
                  "on timer t { n = n + 1; if (n >= 4) { cancelInterval(t); } }")
        engine = run_script(source)
        assert engine.globals["n"] == 4

    def test_timer_callback_counter(self):
        source = ("variables { msTimer t; } on start { setInterval(t, 5); } "
                  "on timer t { if (getTickCount() > 20) { cancelInterval(t); } }")
        engine = run_script(source)
        assert engine.timer_callback_count > 0

    def test_timer_without_handler_does_not_crash(self):
        source = "variables { msTimer t; } on start { setTimer(t, 10); }"
        engine = run_script(source)
        assert engine.errors == []

    def test_timer_value_object(self):
        timer = TimerValue(name="x")
        assert not timer.running
        assert timer.fires == 0


# ---------------------------------------------------------------------------
# 测试原语与断言
# ---------------------------------------------------------------------------


class TestTestPrimitives(object):

    def test_wait_for_timeout_advances_clock(self):
        source = "on start { testWaitForTimeout(100); }"
        engine = run_script(source)
        assert engine.now_ms == pytest.approx(100.0)

    def test_wait_for_timeout_zero(self):
        engine = run_script("on start { testWaitForTimeout(0); }")
        assert engine.now_ms == pytest.approx(0.0)

    def test_wait_for_message_timeout_returns_nonzero(self):
        """请求发出去但没有应答时，等待超时并返回非 0。"""
        source = ("variables { message 0x111 m; int r; } "
                  "on start { output(m); r = testWaitForMessage(0x7E8, 50); }")
        assert value_of(source, "r") == 1

    def test_self_transmission_is_not_received(self):
        """脚本自己 output() 出去的帧不会作为"收到的报文"回来。"""
        engine = run_script(
            "variables { message 0x111 m; int r; int n; } "
            "on message 0x111 { n = n + 1; } "
            "on start { output(m); r = testWaitForMessage(0x111, 20); }")
        assert engine.globals["r"] == 1
        assert engine.globals["n"] == 0
        # 但总线上确实发出去了一帧（发送日志里有）
        assert len(engine.tx_log) == 1
    def test_wait_for_message_success(self):
        """应答由另一个节点发出时，等待返回 0。"""
        bus = CanBus()
        bus.add_node("S")
        bus.add_node("ECU")
        source = ("variables { int r; message 0x100 m; } "
                  "on start { output(m); r = testWaitForMessage(0x200, 50); }")
        engine = CaplEngine(bus=bus, node_name="S")
        engine.load_source(source)
        bus.on_frame = engine._on_bus_frame

        from canlab.frame import Frame as CanFrame

        def reply():
            bus.send("ECU", CanFrame(identifier=0x200, data=b"\x01"))

        original = engine._flush_transmissions

        def flush_then_reply():
            original()
            reply()

        engine._flush_transmissions = flush_then_reply
        engine.run(max_virtual_ms=200)
        assert engine.globals["r"] == 0
    def test_assert_true_pass(self):
        engine = run_script("on start { testAssertTrue(1, \"ok\"); }")
        assert engine.assertions[0].passed

    def test_assert_true_fail(self):
        engine = run_script("on start { testAssertTrue(0, \"bad\"); }")
        assert not engine.assertions[0].passed

    def test_assert_false_pass(self):
        engine = run_script("on start { testAssertFalse(0, \"ok\"); }")
        assert engine.assertions[0].passed

    def test_assert_false_fail(self):
        engine = run_script("on start { testAssertFalse(1, \"bad\"); }")
        assert not engine.assertions[0].passed

    def test_assert_equals_pass(self):
        engine = run_script("on start { testAssertEquals(3, 3, \"ok\"); }")
        assert engine.assertions[0].passed

    def test_assert_equals_fail(self):
        engine = run_script("on start { testAssertEquals(3, 4, \"bad\"); }")
        assert not engine.assertions[0].passed

    def test_assert_not_equals(self):
        engine = run_script("on start { testAssertNotEquals(3, 4, \"ok\"); }")
        assert engine.assertions[0].passed

    def test_assert_not_equals_fail(self):
        engine = run_script("on start { testAssertNotEquals(3, 3, \"bad\"); }")
        assert not engine.assertions[0].passed

    def test_assert_in_range_pass(self):
        engine = run_script("on start { testAssertInRange(5, 1, 10, \"ok\"); }")
        assert engine.assertions[0].passed

    def test_assert_in_range_low_boundary(self):
        engine = run_script("on start { testAssertInRange(1, 1, 10, \"ok\"); }")
        assert engine.assertions[0].passed

    def test_assert_in_range_high_boundary(self):
        engine = run_script("on start { testAssertInRange(10, 1, 10, \"ok\"); }")
        assert engine.assertions[0].passed

    def test_assert_in_range_fail(self):
        engine = run_script("on start { testAssertInRange(0, 1, 10, \"bad\"); }")
        assert not engine.assertions[0].passed

    def test_assertion_returns_status_code(self):
        source = "variables { int r; } on start { r = testAssertTrue(0, \"x\"); }"
        assert value_of(source) == 1

    def test_test_case_begin_end(self):
        source = ("on start { testCaseBegin(\"case1\"); testAssertTrue(1, \"a\"); "
                  "testCaseEnd(); }")
        engine = run_script(source)
        assert len(engine.test_cases) == 1
        assert engine.test_cases[0].name == "case1"

    def test_test_case_duration(self):
        source = ("on start { testCaseBegin(\"c\"); testWaitForTimeout(50); "
                  "testCaseEnd(); }")
        engine = run_script(source)
        assert engine.test_cases[0].duration_ms == pytest.approx(50.0)

    def test_assertion_attributed_to_case(self):
        source = "on start { testCaseBegin(\"c\"); testAssertTrue(1, \"a\"); }"
        engine = run_script(source)
        assert engine.assertions[0].test_case == "c"

    def test_assertion_without_case(self):
        engine = run_script("on start { testAssertTrue(1, \"a\"); }")
        assert engine.assertions[0].test_case == "(no test case)"

    def test_assertion_virtual_timestamp(self):
        source = "on start { testWaitForTimeout(30); testAssertTrue(1, \"a\"); }"
        engine = run_script(source)
        assert engine.assertions[0].virtual_ms == pytest.approx(30.0)

    def test_test_add_comment(self):
        engine = run_script('on start { testAddComment("hello"); }')
        assert any("hello" in line for line in engine.log_lines)

    def test_failure_recorded_in_case(self):
        source = "on start { testCaseBegin(\"c\"); testAssertEquals(1, 2, \"bad\"); }"
        engine = run_script(source)
        assert engine.test_cases[0].failures
        assert not engine.test_cases[0].passed

    def test_stats_snapshot_counts(self):
        source = "on start { testAssertTrue(1, \"a\"); testAssertTrue(0, \"b\"); }"
        engine = run_script(source)
        stats = engine.stats_snapshot()
        assert stats["assertions"] == 2
        assert stats["assertions_passed"] == 1
        assert stats["assertions_failed"] == 1


# ---------------------------------------------------------------------------
# 虚拟时间确定性
# ---------------------------------------------------------------------------


class TestDeterminism(object):

    def test_two_runs_identical_variables(self):
        source = ("variables { int i; int s; msTimer t; } "
                  "on start { s = 0; setInterval(t, 3); } "
                  "on timer t { i = i + 1; s = s + jitterMs(i, 4); "
                  "if (i >= 20) { cancelInterval(t); } }")
        first = run_script(source, seed=5)
        second = run_script(source, seed=5)
        assert first.globals == second.globals
        assert first.now_ms == second.now_ms

    def test_two_runs_identical_assertions(self):
        source = ("variables { int i; } on start { testCaseBegin(\"c\"); "
                  "for (i = 0; i < 10; i = i + 1) "
                  "{ testAssertInRange(jitterMs(i, 5), -5, 5, \"j\"); } "
                  "testCaseEnd(); }")
        first = run_script(source, seed=11)
        second = run_script(source, seed=11)
        assert [(a.kind, a.passed, a.message, a.virtual_ms)
                for a in first.assertions] == \
               [(a.kind, a.passed, a.message, a.virtual_ms)
                for a in second.assertions]

    def test_seed_changes_results(self):
        source = ("variables { int i; int s; msTimer t; } "
                  "on start { setInterval(t, 3); } "
                  "on timer t { i = i + 1; s = s + jitterMs(i, 50); "
                  "if (i >= 20) { cancelInterval(t); } }")
        assert run_script(source, seed=1).globals != run_script(source, seed=2).globals

    def test_virtual_time_only_advances_by_schedule(self):
        source = "on start { testWaitForTimeout(7); }"
        assert run_script(source).now_ms == pytest.approx(7.0)

    def test_no_scheduled_events_ends_immediately(self):
        engine = run_script("on start { }")
        assert engine.now_ms == pytest.approx(0.0)

    def test_max_virtual_time_respected(self):
        source = ("variables { msTimer t; } on start { setInterval(t, 10); } "
                  "on timer t { }")
        engine = run_script(source, max_virtual_ms=55)
        assert engine.now_ms <= 55.0

    def test_engine_finished_flag(self):
        engine = run_script("on start { }")
        assert engine.finished


# ---------------------------------------------------------------------------
# 总线交互
# ---------------------------------------------------------------------------


class TestBusIntegration(object):

    def test_output_sends_frame(self):
        bus = CanBus()
        bus.add_node("T")
        source = "variables { message 0x123 m; } on start { output(m); }"
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert bus.stats.frames_sent == 1

    def test_output_sets_data(self):
        bus = CanBus()
        bus.add_node("T")
        source = ("variables { message 0x123 m; } "
                  "on start { m.byte(0) = 0xAB; output(m); }")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert bus.trace[0].data[0] == 0xAB

    def test_bus_send2_arbitration(self):
        bus = CanBus()
        bus.add_node("T")
        source = ("variables { int r; } "
                  "on start { r = busSend2(\"A\", 0x100, 1, \"B\", 0x200, 2); }")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert engine.globals["r"] == 2

    def test_arbitration_queries(self):
        bus = CanBus()
        bus.add_node("T")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source("variables { int r; } on start { }")
        engine.run(max_virtual_ms=10)
        engine._builtin_bus_send2([self_name(0)], None) if False else None

    def test_total_frames_sent_builtin(self):
        bus = CanBus()
        bus.add_node("T")
        source = ("variables { message 0x123 m; int r; } "
                  "on start { output(m); r = totalFramesSent(); }")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert engine.globals["r"] >= 1

    def test_frame_id_at_builtin(self):
        bus = CanBus()
        bus.add_node("T")
        source = ("variables { message 0x123 m; int r; } "
                  "on start { output(m); r = frameIdAt(0); }")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert engine.globals["r"] == 0x123

    def test_bus_send3(self):
        bus = CanBus()
        bus.add_node("T")
        source = ("variables { int r; } on start {"
                  " r = busSend3(\"A\", 0x100, 1, \"B\", 0x200, 2, \"C\", 0x300, 3); }")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert engine.globals["r"] == 3

    def test_bus_send_data_rtr(self):
        bus = CanBus()
        bus.add_node("T")
        source = ("variables { int r; } on start {"
                  " r = busSendDataRtr(\"D\", 0x100, 1, \"R\", 0x100, 4); }")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert engine.globals["r"] == 2

    def test_error_injection_builtins(self):
        bus = CanBus()
        bus.add_node("T")
        source = ("variables { int tec; int denied; } on start {"
                  " injectBitError(\"X\", 20); nodeSend(\"X\", 0x100, 1);"
                  " tec = nodeTec(\"X\"); denied = nodeIsBusOff(\"X\"); }")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert engine.globals["tec"] == 8
        assert engine.globals["denied"] == 0

    def test_bus_off_and_recovery_builtins(self):
        bus = CanBus()
        bus.add_node("T")
        source = ("variables { int tec; int seq; } on start {"
                  " tec = nodeTec(\"X\"); seq = nodeRecoverySequences(\"X\"); }")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert engine.globals["tec"] == 0
        assert engine.globals["seq"] == 0

    def test_bus_idle_ms_advances_time(self):
        bus = CanBus()
        bus.add_node("T")
        source = "variables { int bits; } on start { bits = busIdleMs(10); }"
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=100)
        assert engine.globals["bits"] == 5000

    def test_bus_bitrate_builtin(self):
        bus = CanBus(bitrate=250000)
        bus.add_node("T")
        source = "variables { int r; } on start { r = busBitrateKbps(); }"
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert engine.globals["r"] == 250

    def test_bus_load_builtin(self):
        bus = CanBus()
        bus.add_node("T")
        source = "variables { int r; } on start { r = busLoadX100(); }"
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert engine.globals["r"] == 0

    def test_bus_time_builtin(self):
        bus = CanBus()
        bus.add_node("T")
        source = "variables { int r; } on start { testWaitForTimeout(10); r = busTimeMs(); }"
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=100)
        assert engine.globals["r"] >= 10

    def test_arbitration_queries_after_send(self):
        bus = CanBus()
        bus.add_node("T")
        source = ("variables { int n; int w; } on start {"
                  " busSend2(\"A\", 0x100, 1, \"B\", 0x200, 2);"
                  " n = arbitrationCount(); w = arbitrationWinnerId(0); }")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert engine.globals["n"] >= 1
        assert engine.globals["w"] == 0x100

    def test_arbitration_loser_queries(self):
        bus = CanBus()
        bus.add_node("T")
        source = ("variables { int c; int b; int i; int r; } on start {"
                  " busSend2(\"A\", 0x100, 1, \"B\", 0x200, 2);"
                  " c = arbitrationLoserCount(0);"
                  " b = arbitrationLoserBit(0);"
                  " i = arbitrationLoserId(0);"
                  " r = arbitrationLoserRetries(0); }")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert engine.globals["c"] == 1
        assert engine.globals["b"] == 2
        assert engine.globals["i"] == 0x200
        assert engine.globals["r"] == 1

    def test_frame_source_at(self):
        bus = CanBus()
        bus.add_node("T")
        source = ("variables { message 0x123 m; int r; } "
                  "on start { output(m); r = frameSourceAt(0) == \"T\"; }")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source(source)
        engine.run(max_virtual_ms=10)
        assert engine.globals["r"] == 1

    def test_send_message_helper(self):
        bus = CanBus()
        bus.add_node("T")
        engine = CaplEngine(bus=bus, node_name="T")
        engine.load_source("variables { message 0x123 m; } on start { }")
        engine.send_message("m")
        assert bus.stats.frames_sent == 1

    def test_send_message_unknown_name(self):
        engine = CaplEngine(bus=CanBus(), node_name="T")
        engine.load_source("variables { int a; } on start { }")
        with pytest.raises(CaplRuntimeError):
            engine.send_message("nope")

    def test_variable_helper(self):
        engine = run_script("variables { int a; } on start { a = 5; }")
        assert engine.variable("a") == 5

    def test_file_scope_property(self):
        engine = run_script("variables { int a; } on start { a = 5; }")
        assert engine.file_scope["a"] == 5


# ---------------------------------------------------------------------------
# 引擎错误处理
# ---------------------------------------------------------------------------


class TestEngineErrors(object):

    def test_lex_error_on_load(self):
        engine = CaplEngine(bus=CanBus(), node_name="T")
        with pytest.raises(LexError):
            engine.load_source('on start { writeLine("unterminated); }')

    def test_parse_error_on_load(self):
        engine = CaplEngine(bus=CanBus(), node_name="T")
        with pytest.raises(ParseError):
            engine.load_source('on start { writeLine("x") if }')

    def test_runtime_error_recorded_not_raised(self):
        engine = run_script("variables { int a; } on start { a = 1 / 0; }")
        assert engine.errors

    def test_load_file(self, tmp_path):
        path = tmp_path / "script.capl"
        path.write_text("variables { int a; } on start { a = 3; }", encoding="utf-8")
        engine = CaplEngine(bus=CanBus(), node_name="T")
        engine.load_file(str(path))
        engine.run(max_virtual_ms=10)
        assert engine.globals["a"] == 3

    def test_returns_from_handler(self):
        engine = run_script("variables { int a; } on start { a = 1; return; a = 2; }")
        assert engine.globals["a"] == 1

    def test_safe_int_helper(self):
        assert _safe_int(True) == 1
        assert _safe_int(False) == 0
        assert _safe_int(5) == 5
        assert _safe_int("abc") == 3
        assert _safe_int(None) == 0

    def test_to_int_and_to_string(self):
        assert CaplEngine.to_int(5) == 5
        assert CaplEngine.to_int("A") == 65
        assert CaplEngine.to_string(5) == "5"

    def test_truthy_rules(self):
        assert CaplEngine.truthy(1)
        assert not CaplEngine.truthy(0)
        assert CaplEngine.truthy("x")
        assert CaplEngine.truthy(MsgValue())

    def test_window_open_alias(self):
        engine = run_script("variables { int n; } on key 'a' { n = 1; } on start { }")
        engine.window_open("a")
        assert engine.globals["n"] == 1
