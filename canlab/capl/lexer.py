# -*- coding: utf-8 -*-
"""词法分析器：把类 CAPL 脚本源码切成记号流。

只借鉴 CAPL 的**书写习惯**（``variables {}``、``on message 0x123``、
``msTimer``、``output()`` 等），词法与语法分析器全部自行实现，不依赖任何
现有工具或库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 记号类型
# ---------------------------------------------------------------------------


class TT(object):
    """记号类型常量。"""

    IDENT = "IDENT"
    INT = "INT"
    STRING = "STRING"
    CHAR = "CHAR"
    KEYWORD = "KEYWORD"
    OP = "OP"
    PUNCT = "PUNCT"
    EOF = "EOF"


#: 语言关键字
KEYWORDS = frozenset([
    # 结构
    "variables", "includes", "on", "start", "stopMeasurement", "message",
    "timer", "key", "signal", "errorFrame", "envVar", "function", "return",
    # 类型
    "int", "long", "dword", "word", "byte", "char", "float", "double", "void",
    "msTimer", "timerUnit", "qword",
    # 语句
    "if", "else", "while", "for", "do", "break", "continue", "switch", "case",
    "default", "this",
    # 常量
    "true", "false",
])

#: 多字符运算符（按长度降序匹配，避免 ``<<`` 被当成两个 ``<``）
OPERATORS: Tuple[str, ...] = (
    "<<=", ">>=", "&&", "||", "==", "!=", "<=", ">=", "<<", ">>",
    "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "++", "--",
    "+", "-", "*", "/", "%", "&", "|", "^", "~", "!", "<", ">", "=", "?", ":",
    ".",
)

PUNCTUATION = frozenset("(){}[];,")


@dataclass
class Token(object):
    """一个词法记号。"""

    kind: str
    value: object
    line: int = 1
    column: int = 1
    #: 原始文本（错误提示用）
    text: str = ""

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<%s %r @%d:%d>" % (self.kind, self.value, self.line, self.column)


class LexError(Exception):
    """词法错误（非法字符、未闭合字符串等）。"""

    def __init__(self, message: str, line: int = 1, column: int = 1) -> None:
        super(LexError, self).__init__("line %d:%d: %s" % (line, column, message))
        self.line = line
        self.column = column


# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------

#: 转义字符表
ESCAPES: Dict[str, str] = {
    "n": "\n", "t": "\t", "r": "\r", "0": "\0",
    "\\": "\\", "'": "'", '"': '"', "b": "\b", "f": "\f", "v": "\v",
}


def is_ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


def is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def tokenize(source: str) -> List[Token]:
    """把源码切成记号流（末尾一定有一个 ``EOF`` 记号）。

    :raises LexError: 遇到非法字符或未闭合的字符串/字符字面量。
    """
    tokens: List[Token] = []
    index = 0
    line = 1
    column = 1
    length = len(source)

    def advance(count: int = 1) -> None:
        nonlocal index, line, column
        for _ in range(count):
            if index < length and source[index] == "\n":
                line += 1
                column = 1
            else:
                column += 1
            index += 1

    while index < length:
        ch = source[index]

        # 空白
        if ch in " \t\r\n":
            advance()
            continue

        # 注释
        if ch == "/" and index + 1 < length:
            nxt = source[index + 1]
            if nxt == "/":
                while index < length and source[index] != "\n":
                    advance()
                continue
            if nxt == "*":
                start_line, start_col = line, column
                advance(2)
                closed = False
                while index < length:
                    if source[index] == "*" and index + 1 < length and source[index + 1] == "/":
                        advance(2)
                        closed = True
                        break
                    advance()
                if not closed:
                    raise LexError("unterminated block comment", start_line, start_col)
                continue

        # 字符串字面量
        if ch == '"':
            start_line, start_col = line, column
            advance()
            chars: List[str] = []
            while True:
                if index >= length:
                    raise LexError("unterminated string literal", start_line, start_col)
                cur = source[index]
                if cur == '"':
                    advance()
                    break
                if cur == "\\":
                    advance()
                    if index >= length:
                        raise LexError("unterminated escape sequence", start_line, start_col)
                    esc = source[index]
                    chars.append(ESCAPES.get(esc, esc))
                    advance()
                    continue
                if cur == "\n":
                    raise LexError("newline inside string literal", start_line, start_col)
                chars.append(cur)
                advance()
            tokens.append(Token(TT.STRING, "".join(chars), start_line, start_col,
                                 '"%s"' % "".join(chars)))
            continue

        # 字符字面量
        if ch == "'":
            start_line, start_col = line, column
            advance()
            if index >= length:
                raise LexError("unterminated character literal", start_line, start_col)
            if source[index] == "\\":
                advance()
                if index >= length:
                    raise LexError("unterminated escape sequence", start_line, start_col)
                value = ESCAPES.get(source[index], source[index])
                advance()
            else:
                value = source[index]
                advance()
            if index >= length or source[index] != "'":
                raise LexError("unterminated character literal", start_line, start_col)
            advance()
            tokens.append(Token(TT.CHAR, value, start_line, start_col, "'%s'" % value))
            continue

        # 数字字面量
        if ch.isdigit():
            start_line, start_col = line, column
            text_chars: List[str] = []
            if ch == "0" and index + 1 < length and source[index + 1] in "xX":
                text_chars.append(source[index])
                advance()
                text_chars.append(source[index])
                advance()
                digits = 0
                while index < length and (source[index].isdigit()
                                          or source[index].lower() in "abcdef"):
                    text_chars.append(source[index])
                    advance()
                    digits += 1
                if digits == 0:
                    raise LexError("malformed hexadecimal literal", start_line, start_col)
                value_int = int("".join(text_chars), 16)
            else:
                while index < length and (source[index].isdigit() or source[index] == "."):
                    text_chars.append(source[index])
                    advance()
                text = "".join(text_chars)
                if text.endswith(".") or "." in text:
                    # 浮点字面量：CAPL 里存在，但本解释器按整数截断处理
                    try:
                        value_int = int(float(text))
                    except ValueError:
                        raise LexError("malformed numeric literal %r" % text,
                                       start_line, start_col)
                else:
                    value_int = int(text, 10)
            tokens.append(Token(TT.INT, value_int, start_line, start_col,
                                "".join(text_chars)))
            continue

        # 标识符 / 关键字
        if is_ident_start(ch):
            start_line, start_col = line, column
            chars = []
            while index < length and is_ident_char(source[index]):
                chars.append(source[index])
                advance()
            text = "".join(chars)
            kind = TT.KEYWORD if text in KEYWORDS else TT.IDENT
            tokens.append(Token(kind, text, start_line, start_col, text))
            continue

        # 运算符（先尝试长运算符）
        matched = None
        for op in OPERATORS:
            if source.startswith(op, index):
                matched = op
                break
        if matched is not None:
            start_line, start_col = line, column
            advance(len(matched))
            tokens.append(Token(TT.OP, matched, start_line, start_col, matched))
            continue

        # 标点
        if ch in PUNCTUATION:
            start_line, start_col = line, column
            advance()
            tokens.append(Token(TT.PUNCT, ch, start_line, start_col, ch))
            continue

        raise LexError("unexpected character %r" % ch, line, column)

    tokens.append(Token(TT.EOF, None, line, column, "<eof>"))
    return tokens


def format_tokens(tokens: List[Token]) -> str:
    """把记号流格式化成一行文本（调试与测试断言用）。"""
    parts = []
    for token in tokens:
        if token.kind == TT.EOF:
            parts.append("EOF")
        elif token.kind == TT.STRING:
            parts.append('STR(%s)' % token.value)
        elif token.kind == TT.CHAR:
            parts.append("CHR(%s)" % token.value)
        else:
            parts.append("%s(%s)" % (token.kind, token.text or token.value))
    return " ".join(parts)
