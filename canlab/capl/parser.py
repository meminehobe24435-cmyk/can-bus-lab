# -*- coding: utf-8 -*-
"""语法分析器：递归下降，把记号流解析成 AST。

支持的语言子集（贴近 CAPL 书写习惯）::

    variables {
        int   counter;
        byte  payload[8];
        message 0x123 txMsg;
        msTimer t1;
    }

    on start              { ... }
    on message 0x123      { ... }
    on message *          { ... }
    on timer t1           { ... }
    on key 'a'            { ... }
    on stopMeasurement     { ... }

    语句：if/else、while、for、do-while、break/continue、return、
          赋值与复合赋值、自增自减、局部声明、表达式语句
    表达式：算术 + - * / %、位运算 & | ^ ~ << >>、比较、逻辑 && || !、
            三元 ? :、函数调用、成员访问 this.id / this.byte(i)、
            十六进制字面量、字符字面量、字符串字面量
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .lexer import KEYWORDS, TT, Token, tokenize

#: 变量类型 -> (位宽, 有符号)，None 表示不定长（数组 / 消息等）
TYPE_INFO: Dict[str, Tuple[Optional[int], bool]] = {
    "int": (16, True),
    "long": (32, True),
    "dword": (32, False),
    "word": (16, False),
    "byte": (8, False),
    "char": (8, True),
    "qword": (64, False),
    "float": (32, True),
    "double": (64, True),
}

#: 定时器对象类型
TIMER_TYPES = frozenset(["msTimer", "timer"])


class ParseError(Exception):
    """语法错误。"""

    def __init__(self, message: str, token: Optional[Token] = None) -> None:
        if token is not None:
            super(ParseError, self).__init__(
                "line %d:%d: %s (got %s)" % (token.line, token.column, message,
                                             token.text or token.kind)
            )
            self.line = token.line
            self.column = token.column
        else:
            super(ParseError, self).__init__(message)
            self.line = 0
            self.column = 0


# ---------------------------------------------------------------------------
# AST 节点
# ---------------------------------------------------------------------------


@dataclass
class Node(object):
    """所有 AST 节点的基类（携带源码位置，便于报错）。"""

    line: int = 0
    column: int = 0


# -- 表达式 -----------------------------------------------------------------


@dataclass
class IntLiteral(Node):
    value: int = 0


@dataclass
class StringLiteral(Node):
    value: str = ""


@dataclass
class CharLiteral(Node):
    value: str = ""


@dataclass
class Identifier(Node):
    name: str = ""


@dataclass
class ThisExpr(Node):
    """``this`` 关键字（消息事件里指向当前报文）。"""


@dataclass
class MemberAccess(Node):
    obj: Node = field(default_factory=Node)
    member: str = ""


@dataclass
class IndexAccess(Node):
    obj: Node = field(default_factory=Node)
    index: Node = field(default_factory=Node)


@dataclass
class CallExpr(Node):
    callee: str = ""
    args: List[Node] = field(default_factory=list)
    #: 方法调用形式的接收者（``msg.byte(0)`` 里的 ``msg``）；普通调用为 None
    receiver: Optional[Node] = None


@dataclass
class UnaryOp(Node):
    op: str = ""
    operand: Node = field(default_factory=Node)


@dataclass
class BinaryOp(Node):
    op: str = ""
    left: Node = field(default_factory=Node)
    right: Node = field(default_factory=Node)


@dataclass
class TernaryOp(Node):
    condition: Node = field(default_factory=Node)
    if_true: Node = field(default_factory=Node)
    if_false: Node = field(default_factory=Node)


@dataclass
class AssignExpr(Node):
    target: Node = field(default_factory=Node)
    op: str = "="
    value: Node = field(default_factory=Node)


@dataclass
class IncDecExpr(Node):
    target: Node = field(default_factory=Node)
    op: str = "++"
    prefix: bool = True


# -- 语句 -------------------------------------------------------------------


@dataclass
class VarDecl(Node):
    type_name: str = ""
    name: str = ""
    array_size: Optional[Node] = None
    init: Optional[Node] = None
    #: ``message 0x123 msg;`` 形式的报文声明
    message_id: Optional[Node] = None
    #: ``msTimer t;`` 形式的定时器声明
    is_timer: bool = False
    #: 报文声明是否带扩展帧标志
    extended: bool = False


@dataclass
class Block(Node):
    statements: List[Node] = field(default_factory=list)


@dataclass
class IfStmt(Node):
    condition: Node = field(default_factory=Node)
    then_branch: Node = field(default_factory=Block)
    else_branch: Optional[Node] = None


@dataclass
class WhileStmt(Node):
    condition: Node = field(default_factory=Node)
    body: Node = field(default_factory=Block)


@dataclass
class DoWhileStmt(Node):
    body: Node = field(default_factory=Block)
    condition: Node = field(default_factory=Node)


@dataclass
class ForStmt(Node):
    init: Optional[Node] = None
    condition: Optional[Node] = None
    update: Optional[Node] = None
    body: Node = field(default_factory=Block)


@dataclass
class BreakStmt(Node):
    pass


@dataclass
class ContinueStmt(Node):
    pass


@dataclass
class ReturnStmt(Node):
    value: Optional[Node] = None


@dataclass
class ExprStmt(Node):
    expr: Node = field(default_factory=Node)


# -- 顶层 -------------------------------------------------------------------


@dataclass
class EventHandler(Node):
    """``on ...`` 事件处理器。"""

    kind: str = ""            # start / message / timer / key / stopMeasurement
    message_id: Optional[int] = None
    wildcard: bool = False
    timer_name: str = ""
    key: str = ""
    body: Node = field(default_factory=Block)
    #: ``on message 0x123`` 中的扩展帧标记
    extended: bool = False


@dataclass
class FunctionDecl(Node):
    name: str = ""
    return_type: str = "void"
    params: List[VarDecl] = field(default_factory=list)
    body: Node = field(default_factory=Block)


@dataclass
class Script(Node):
    """一个脚本的完整 AST。"""

    variables: List[VarDecl] = field(default_factory=list)
    handlers: List[EventHandler] = field(default_factory=list)
    functions: List[FunctionDecl] = field(default_factory=list)
    #: 脚本里出现的 ``message`` 声明（供引擎预创建）
    messages: List[VarDecl] = field(default_factory=list)
    #: 脚本里出现的定时器声明
    timers: List[VarDecl] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 语法分析器
# ---------------------------------------------------------------------------


class Parser(object):
    """递归下降语法分析器。"""

    def __init__(self, tokens: Sequence[Token]) -> None:
        self.tokens = list(tokens)
        self.pos = 0

    # -- 记号工具 ---------------------------------------------------------

    @property
    def current(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 0) -> Token:
        index = self.pos + offset
        if index >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[index]

    def advance(self) -> Token:
        token = self.tokens[self.pos]
        if token.kind != TT.EOF:
            self.pos += 1
        return token

    def check(self, kind: str, value: object = None) -> bool:
        token = self.current
        if token.kind != kind:
            return False
        if value is None:
            return True
        return token.value == value

    def check_op(self, op: str) -> bool:
        return self.current.kind == TT.OP and self.current.value == op

    def match(self, kind: str, value: object = None) -> Optional[Token]:
        if self.check(kind, value):
            return self.advance()
        return None

    def match_op(self, *ops: str) -> Optional[Token]:
        if self.current.kind == TT.OP and self.current.value in ops:
            return self.advance()
        return None

    def expect(self, kind: str, value: object = None,
               message: str = "") -> Token:
        if self.check(kind, value):
            return self.advance()
        expected = value if value is not None else kind
        raise ParseError(message or "expected %r" % (expected,), self.current)

    def expect_op(self, op: str) -> Token:
        if self.check_op(op):
            return self.advance()
        raise ParseError("expected operator %r" % op, self.current)

    #: 允许当作标识符使用的关键字（CAPL 里这些词也能做变量/函数名）
    SOFT_KEYWORDS = frozenset(["key", "signal", "message", "timer", "byte",
                               "char", "int", "long", "word", "dword"])

    def expect_ident(self) -> str:
        token = self.current
        if token.kind == TT.IDENT:
            self.advance()
            return str(token.value)
        if token.kind == TT.KEYWORD and token.value in self.SOFT_KEYWORDS:
            self.advance()
            return str(token.value)
        raise ParseError("expected identifier", token)

    # -- 顶层 -------------------------------------------------------------

    def parse_script(self) -> Script:
        """解析整个脚本。"""
        script = Script(line=1, column=1)
        while not self.check(TT.EOF):
            token = self.current
            if token.kind == TT.KEYWORD and token.value == "variables":
                self.advance()
                self.expect(TT.PUNCT, "{")
                while not self.check(TT.PUNCT, "}"):
                    decls = self.parse_declaration(global_scope=True)
                    for decl in decls:
                        script.variables.append(decl)
                        if decl.message_id is not None:
                            script.messages.append(decl)
                        if decl.is_timer:
                            script.timers.append(decl)
                self.expect(TT.PUNCT, "}")
                continue
            if token.kind == TT.KEYWORD and token.value in ("includes",):
                self.advance()
                self.expect(TT.PUNCT, "{")
                while not self.check(TT.PUNCT, "}"):
                    if self.check(TT.EOF):
                        raise ParseError("unexpected end of file inside includes",
                                         self.current)
                    self.advance()
                self.expect(TT.PUNCT, "}")
                continue
            if token.kind == TT.KEYWORD and token.value == "on":
                script.handlers.append(self.parse_handler())
                continue
            # CAPL 的两种函数定义写法都支持：
            #   function int foo(int a) { ... }
            #   int foo(int a) { ... }
            if token.kind == TT.KEYWORD and token.value == "function":
                script.functions.append(self.parse_function())
                continue
            if (token.kind == TT.KEYWORD
                    and (token.value in TYPE_INFO or token.value == "void")
                    and self.peek(1).kind == TT.IDENT
                    and self.peek(2).kind == TT.PUNCT and self.peek(2).value == "("):
                script.functions.append(self.parse_c_function())
                continue
            raise ParseError("unexpected token at top level", token)
        return script

    def parse_c_function(self) -> FunctionDecl:
        """解析 CAPL 风格的 ``<类型> 名字(参数...) { ... }`` 函数定义。"""
        start = self.advance()
        return_type = str(start.value)
        name = self.expect_ident()
        self.expect(TT.PUNCT, "(")
        params: List[VarDecl] = []
        if not self.check(TT.PUNCT, ")"):
            while True:
                params.append(self.parse_declaration(global_scope=False,
                                                     allow_multiple=False,
                                                     allow_init=False,
                                                     require_semicolon=False)[0])
                if not self.match(TT.PUNCT, ","):
                    break
        self.expect(TT.PUNCT, ")")
        return FunctionDecl(name=name, return_type=return_type, params=params,
                            body=self.parse_block(), line=start.line,
                            column=start.column)

    def parse_handler(self) -> EventHandler:
        """解析 ``on ...`` 事件处理器。"""
        start = self.expect(TT.KEYWORD, "on")
        kind_token = self.expect(TT.KEYWORD, None, "expected event name after 'on'")
        kind = str(kind_token.value)
        handler = EventHandler(kind=kind, line=start.line, column=start.column)

        if kind == "start":
            pass
        elif kind == "message":
            if self.check_op("*"):
                self.advance()
                handler.wildcard = True
            else:
                handler.message_id = self.parse_integer_literal()
                if self.check(TT.KEYWORD, "extended"):
                    self.advance()
                    handler.extended = True
        elif kind == "timer":
            handler.timer_name = self.expect_ident()
        elif kind == "key":
            token = self.current
            if token.kind == TT.CHAR:
                self.advance()
                handler.key = str(token.value)
            elif token.kind == TT.INT:
                self.advance()
                handler.key = chr(int(token.value) & 0xFF)
            elif token.kind == TT.STRING:
                self.advance()
                handler.key = str(token.value)
            else:
                raise ParseError("expected a character literal after 'on key'", token)
        elif kind == "stopMeasurement":
            pass
        else:
            raise ParseError("unsupported event type %r" % kind, kind_token)

        handler.body = self.parse_block()
        return handler

    def parse_integer_literal(self) -> int:
        """解析一个整数字面量，也接受字符字面量。"""
        token = self.current
        if token.kind == TT.INT:
            self.advance()
            return int(token.value)
        if token.kind == TT.CHAR:
            self.advance()
            value = str(token.value)
            return ord(value) if value else 0
        raise ParseError("expected an integer literal", token)

    def parse_function(self) -> FunctionDecl:
        """解析用户自定义函数。"""
        start = self.expect(TT.KEYWORD, "function")
        return_type = "void"
        if self.check(TT.KEYWORD) and self.current.value in TYPE_INFO:
            return_type = str(self.advance().value)
        name = self.expect_ident()
        self.expect(TT.PUNCT, "(")
        params: List[VarDecl] = []
        if not self.check(TT.PUNCT, ")"):
            while True:
                params.append(self.parse_declaration(global_scope=False,
                                                     allow_multiple=False,
                                                     allow_init=False,
                                                     require_semicolon=False)[0])
                if not self.match(TT.PUNCT, ","):
                    break
        self.expect(TT.PUNCT, ")")
        decl = FunctionDecl(name=name, return_type=return_type, params=params,
                            body=self.parse_block(), line=start.line,
                            column=start.column)
        return decl

    # -- 声明 -------------------------------------------------------------

    def parse_declaration(self, global_scope: bool = False,
                          allow_multiple: bool = True,
                          allow_init: bool = True,
                          require_semicolon: bool = True) -> List[VarDecl]:
        """解析一条变量 / 报文 / 定时器声明（可含多个逗号分隔的声明符）。"""
        token = self.current
        if token.kind != TT.KEYWORD:
            raise ParseError("expected a type name", token)
        type_name = str(token.value)

        if type_name == "message":
            self.advance()
            message_id = self.parse_integer_literal()
            extended = bool(self.match(TT.KEYWORD, "extended"))
            name = self.expect_ident()
            decl = VarDecl(type_name="message", name=name,
                           message_id=IntLiteral(value=message_id),
                           extended=extended, line=token.line, column=token.column)
            if require_semicolon:
                self._expect_declaration_end()
            return [decl]

        if type_name in TIMER_TYPES:
            self.advance()
            name = self.expect_ident()
            decl = VarDecl(type_name=type_name, name=name, is_timer=True,
                           line=token.line, column=token.column)
            if require_semicolon:
                self._expect_declaration_end()
            return [decl]

        if type_name not in TYPE_INFO:
            raise ParseError("unknown type %r" % type_name, token)

        self.advance()
        decls: List[VarDecl] = []
        while True:
            name = self.expect_ident()
            decl = VarDecl(type_name=type_name, name=name,
                           line=token.line, column=token.column)
            if self.match(TT.PUNCT, "["):
                decl.array_size = self.parse_expression()
                self.expect(TT.PUNCT, "]")
            if allow_init and self.match_op("="):
                decl.init = self.parse_assignment()
            decls.append(decl)
            if not (allow_multiple and self.match(TT.PUNCT, ",")):
                break
        if require_semicolon:
            self._expect_declaration_end()
        return decls

    def _expect_declaration_end(self) -> None:
        if self.check(TT.PUNCT, ";"):
            self.advance()
            return
        if self.check(TT.PUNCT, "}") or self.check(TT.EOF):
            # 允许省略块内最后一条声明后的分号
            return
        raise ParseError("expected ';' after declaration", self.current)

    # -- 语句 -------------------------------------------------------------

    def parse_block(self) -> Block:
        """解析 ``{ ... }`` 语句块。"""
        start = self.expect(TT.PUNCT, "{")
        block = Block(statements=[], line=start.line, column=start.column)
        while not self.check(TT.PUNCT, "}"):
            if self.check(TT.EOF):
                raise ParseError("unexpected end of file, missing '}'", self.current)
            block.statements.append(self.parse_statement())
        self.expect(TT.PUNCT, "}")
        return block

    def parse_statement(self) -> Node:
        """解析一条语句。"""
        token = self.current

        if token.kind == TT.PUNCT and token.value == "{":
            return self.parse_block()
        if token.kind == TT.PUNCT and token.value == ";":
            self.advance()
            return ExprStmt(expr=IntLiteral(value=0), line=token.line,
                            column=token.column)
        if token.kind == TT.KEYWORD:
            value = str(token.value)
            if value == "if":
                return self.parse_if()
            if value == "while":
                return self.parse_while()
            if value == "do":
                return self.parse_do_while()
            if value == "for":
                return self.parse_for()
            if value == "break":
                self.advance()
                self.expect(TT.PUNCT, ";")
                return BreakStmt(line=token.line, column=token.column)
            if value == "continue":
                self.advance()
                self.expect(TT.PUNCT, ";")
                return ContinueStmt(line=token.line, column=token.column)
            if value == "return":
                self.advance()
                expr = None
                if not self.check(TT.PUNCT, ";"):
                    expr = self.parse_expression()
                self.expect(TT.PUNCT, ";")
                return ReturnStmt(value=expr, line=token.line, column=token.column)
            if value in TYPE_INFO or value == "message" or value in TIMER_TYPES:
                decls = self.parse_declaration(global_scope=False)
                # 一条语句里声明了多个变量时，用块包起来以便依次执行
                if len(decls) == 1:
                    return decls[0]
                return Block(statements=list(decls), line=token.line,
                             column=token.column)

        expr = self.parse_expression()
        self.expect(TT.PUNCT, ";")
        return ExprStmt(expr=expr, line=token.line, column=token.column)

    def parse_if(self) -> IfStmt:
        start = self.expect(TT.KEYWORD, "if")
        self.expect(TT.PUNCT, "(")
        condition = self.parse_expression()
        self.expect(TT.PUNCT, ")")
        then_branch = self.parse_statement()
        else_branch = None
        if self.check(TT.KEYWORD, "else"):
            self.advance()
            else_branch = self.parse_statement()
        return IfStmt(condition=condition, then_branch=then_branch,
                      else_branch=else_branch, line=start.line, column=start.column)

    def parse_while(self) -> WhileStmt:
        start = self.expect(TT.KEYWORD, "while")
        self.expect(TT.PUNCT, "(")
        condition = self.parse_expression()
        self.expect(TT.PUNCT, ")")
        body = self.parse_statement()
        return WhileStmt(condition=condition, body=body, line=start.line,
                         column=start.column)

    def parse_do_while(self) -> DoWhileStmt:
        start = self.expect(TT.KEYWORD, "do")
        body = self.parse_statement()
        self.expect(TT.KEYWORD, "while")
        self.expect(TT.PUNCT, "(")
        condition = self.parse_expression()
        self.expect(TT.PUNCT, ")")
        self.expect(TT.PUNCT, ";")
        return DoWhileStmt(body=body, condition=condition, line=start.line,
                           column=start.column)

    def parse_for(self) -> ForStmt:
        start = self.expect(TT.KEYWORD, "for")
        self.expect(TT.PUNCT, "(")
        init: Optional[Node] = None
        if not self.check(TT.PUNCT, ";"):
            if (self.check(TT.KEYWORD) and str(self.current.value) in TYPE_INFO):
                decls = self.parse_declaration(global_scope=False)
                init = decls[0] if len(decls) == 1 else Block(statements=list(decls))
            else:
                init = self.parse_expression()
                self.expect(TT.PUNCT, ";")
        else:
            self.expect(TT.PUNCT, ";")
        condition = None
        if not self.check(TT.PUNCT, ";"):
            condition = self.parse_expression()
        self.expect(TT.PUNCT, ";")
        update = None
        if not self.check(TT.PUNCT, ")"):
            update = self.parse_expression()
        self.expect(TT.PUNCT, ")")
        body = self.parse_statement()
        return ForStmt(init=init, condition=condition, update=update, body=body,
                       line=start.line, column=start.column)

    # -- 表达式（优先级由低到高）------------------------------------------

    def parse_expression(self) -> Node:
        expr = self.parse_assignment()
        # 逗号表达式：CAPL 里偶尔使用，这里返回最后一个值
        while self.check(TT.PUNCT, ","):
            self.advance()
            right = self.parse_assignment()
            expr = BinaryOp(op=",", left=expr, right=right)
        return expr

    def parse_assignment(self) -> Node:
        left = self.parse_ternary()
        token = self.current
        if token.kind == TT.OP and token.value in (
                "=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="):
            self.advance()
            value = self.parse_assignment()
            return AssignExpr(target=left, op=str(token.value), value=value,
                              line=token.line, column=token.column)
        return left

    def parse_ternary(self) -> Node:
        condition = self.parse_logical_or()
        if self.check_op("?"):
            token = self.advance()
            if_true = self.parse_assignment()
            self.expect_op(":")
            if_false = self.parse_assignment()
            return TernaryOp(condition=condition, if_true=if_true,
                             if_false=if_false, line=token.line, column=token.column)
        return condition

    def parse_logical_or(self) -> Node:
        left = self.parse_logical_and()
        while self.check_op("||"):
            token = self.advance()
            right = self.parse_logical_and()
            left = BinaryOp(op="||", left=left, right=right, line=token.line,
                            column=token.column)
        return left

    def parse_logical_and(self) -> Node:
        left = self.parse_bitwise_or()
        while self.check_op("&&"):
            token = self.advance()
            right = self.parse_bitwise_or()
            left = BinaryOp(op="&&", left=left, right=right, line=token.line,
                            column=token.column)
        return left

    def parse_bitwise_or(self) -> Node:
        left = self.parse_bitwise_xor()
        while self.check_op("|"):
            token = self.advance()
            right = self.parse_bitwise_xor()
            left = BinaryOp(op="|", left=left, right=right, line=token.line,
                            column=token.column)
        return left

    def parse_bitwise_xor(self) -> Node:
        left = self.parse_bitwise_and()
        while self.check_op("^"):
            token = self.advance()
            right = self.parse_bitwise_and()
            left = BinaryOp(op="^", left=left, right=right, line=token.line,
                            column=token.column)
        return left

    def parse_bitwise_and(self) -> Node:
        left = self.parse_equality()
        while self.check_op("&"):
            token = self.advance()
            right = self.parse_equality()
            left = BinaryOp(op="&", left=left, right=right, line=token.line,
                            column=token.column)
        return left

    def parse_equality(self) -> Node:
        left = self.parse_relational()
        while self.current.kind == TT.OP and self.current.value in ("==", "!="):
            token = self.advance()
            right = self.parse_relational()
            left = BinaryOp(op=str(token.value), left=left, right=right,
                            line=token.line, column=token.column)
        return left

    def parse_relational(self) -> Node:
        left = self.parse_shift()
        while self.current.kind == TT.OP and self.current.value in (
                "<", ">", "<=", ">="):
            token = self.advance()
            right = self.parse_shift()
            left = BinaryOp(op=str(token.value), left=left, right=right,
                            line=token.line, column=token.column)
        return left

    def parse_shift(self) -> Node:
        left = self.parse_additive()
        while self.current.kind == TT.OP and self.current.value in ("<<", ">>"):
            token = self.advance()
            right = self.parse_additive()
            left = BinaryOp(op=str(token.value), left=left, right=right,
                            line=token.line, column=token.column)
        return left

    def parse_additive(self) -> Node:
        left = self.parse_multiplicative()
        while self.current.kind == TT.OP and self.current.value in ("+", "-"):
            token = self.advance()
            right = self.parse_multiplicative()
            left = BinaryOp(op=str(token.value), left=left, right=right,
                            line=token.line, column=token.column)
        return left

    def parse_multiplicative(self) -> Node:
        left = self.parse_unary()
        while self.current.kind == TT.OP and self.current.value in ("*", "/", "%"):
            token = self.advance()
            right = self.parse_unary()
            left = BinaryOp(op=str(token.value), left=left, right=right,
                            line=token.line, column=token.column)
        return left

    def parse_unary(self) -> Node:
        token = self.current
        if token.kind == TT.OP and token.value in ("!", "~", "-", "+"):
            self.advance()
            operand = self.parse_unary()
            return UnaryOp(op=str(token.value), operand=operand, line=token.line,
                           column=token.column)
        if token.kind == TT.OP and token.value in ("++", "--"):
            self.advance()
            operand = self.parse_unary()
            return IncDecExpr(target=operand, op=str(token.value), prefix=True,
                              line=token.line, column=token.column)
        return self.parse_postfix()

    def parse_postfix(self) -> Node:
        expr = self.parse_primary()
        while True:
            token = self.current
            if token.kind == TT.PUNCT and token.value == "(":
                # 函数调用：支持 foo(...) 与 obj.method(...) 两种形式
                if isinstance(expr, Identifier):
                    callee = expr.name
                    receiver = None
                elif isinstance(expr, MemberAccess):
                    callee = expr.member
                    receiver = expr.obj
                else:
                    raise ParseError("only named functions can be called", token)
                self.advance()
                args: List[Node] = []
                if not self.check(TT.PUNCT, ")"):
                    while True:
                        args.append(self.parse_assignment())
                        if not self.match(TT.PUNCT, ","):
                            break
                self.expect(TT.PUNCT, ")")
                expr = CallExpr(callee=callee, args=args, receiver=receiver,
                                line=token.line, column=token.column)
                continue
            if token.kind == TT.PUNCT and token.value == "[":
                self.advance()
                index = self.parse_expression()
                self.expect(TT.PUNCT, "]")
                expr = IndexAccess(obj=expr, index=index, line=token.line,
                                   column=token.column)
                continue
            if token.kind == TT.OP and token.value == ".":
                self.advance()
                # 成员名允许是关键字：``msg.byte(0)`` 里的 ``byte`` 是类型关键字
                name_token = self.current
                if name_token.kind not in (TT.IDENT, TT.KEYWORD):
                    raise ParseError("expected a member name after '.'", name_token)
                self.advance()
                expr = MemberAccess(obj=expr, member=str(name_token.value),
                                    line=token.line, column=token.column)
                continue
            if token.kind == TT.OP and token.value in ("++", "--"):
                self.advance()
                expr = IncDecExpr(target=expr, op=str(token.value), prefix=False,
                                  line=token.line, column=token.column)
                continue
            break
        return expr

    def parse_primary(self) -> Node:
        token = self.current
        if token.kind == TT.INT:
            self.advance()
            return IntLiteral(value=int(token.value), line=token.line,
                              column=token.column)
        if token.kind == TT.STRING:
            self.advance()
            return StringLiteral(value=str(token.value), line=token.line,
                                 column=token.column)
        if token.kind == TT.CHAR:
            self.advance()
            return CharLiteral(value=str(token.value), line=token.line,
                               column=token.column)
        if token.kind == TT.KEYWORD and token.value in ("true", "false"):
            self.advance()
            return IntLiteral(value=1 if token.value == "true" else 0,
                              line=token.line, column=token.column)
        if token.kind == TT.KEYWORD and token.value == "this":
            self.advance()
            return ThisExpr(line=token.line, column=token.column)
        if token.kind == TT.IDENT or (token.kind == TT.KEYWORD
                                      and token.value in self.SOFT_KEYWORDS):
            self.advance()
            return Identifier(name=str(token.value), line=token.line,
                              column=token.column)
        if token.kind == TT.PUNCT and token.value == "(":
            self.advance()
            expr = self.parse_expression()
            self.expect(TT.PUNCT, ")")
            return expr
        raise ParseError("unexpected token in expression", token)


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------


def parse(source: str) -> Script:
    """把源码解析成 AST。

    :raises LexError: 词法错误。
    :raises ParseError: 语法错误。
    """
    return Parser(tokenize(source)).parse_script()


def parse_tokens(tokens: Sequence[Token]) -> Script:
    """从已有记号流解析（测试用）。"""
    return Parser(tokens).parse_script()


def count_nodes(node: object) -> int:
    """递归统计 AST 节点数量（测试与文档用）。"""
    if not isinstance(node, Node):
        return 0
    total = 1
    for field_name in getattr(node, "__dataclass_fields__", {}):
        value = getattr(node, field_name)
        if isinstance(value, Node):
            total += count_nodes(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                total += count_nodes(item)
    return total
