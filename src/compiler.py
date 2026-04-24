#!/usr/bin/env python3
"""
compiler.py  —  компилятор языка JavaLight (JL) → M68k-inspired Harvard ISA

Рантайм-библиотека вынесена в runtime.py (RuntimeEmitter).

Использование:
    python compiler.py <source.jl> <output_dir>

Создаёт файлы:
    <output_dir>/<stem>.imem    — бинарный образ памяти команд
    <output_dir>/<stem>.dmem    — бинарный образ памяти данных
    <output_dir>/<stem>.lst     — дизассемблерный листинг
"""

import os
import struct
import sys
from dataclasses import dataclass

from config import DEFAULT_OUT_DIR, IO_IN, IO_OUT
from runtime import InlineEmitter, RuntimeEmitter, reachable_fns

# ═══════════════════════════════════════════════════════════════════════════════
#  ISA  (встроенная копия isa.py — компилятор самодостаточен)
# ═══════════════════════════════════════════════════════════════════════════════

FP = 6  # A6 = frame pointer
SP = 7  # A7 = stack pointer


class Op:
    MOVE = 0x01
    MOVEA = 0x02
    ADD = 0x10
    SUB = 0x11
    MUL = 0x12
    DIV = 0x13
    CMP = 0x14
    AND = 0x20
    OR = 0x21
    XOR = 0x22
    NOT = 0x23
    ASL = 0x30
    ASR = 0x31
    LSL = 0x32
    LSR = 0x33
    JMP = 0x40
    JSR = 0x41
    RTS = 0x42
    BEQ = 0x50
    BNE = 0x51
    BLT = 0x52
    BGT = 0x53
    BLE = 0x54
    BGE = 0x55
    BMI = 0x56
    BPL = 0x57
    BCC = 0x58
    BCS = 0x59
    BVC = 0x5A
    BVS = 0x5B
    BRA = 0x5C
    LINK = 0x60
    UNLK = 0x61
    HALT = 0xFF


class AM:
    REG_D = 0x0
    REG_A = 0x1
    IMMED = 0x2
    MEM_IND = 0x3
    MEM_POST = 0x4
    MEM_PRE = 0x5
    MEM_DISP = 0x6
    MEM_IDX = 0x7
    NONE = 0xF


OP_NAMES = {v: k for k, v in vars(Op).items() if not k.startswith("_")}


def _rn(n):
    return f"D{n}" if n < 8 else f"A{n-8}"


class Operand:
    def __init__(self, mode, reg=0, imm=0, disp=0, idx_reg=0):
        self.mode = mode
        self.reg = reg
        self.imm = imm
        self.disp = disp
        self.idx_reg = idx_reg

    def extra_words(self):
        if self.mode == AM.IMMED:
            return [self.imm & 0xFFFFFFFF]
        if self.mode == AM.MEM_DISP:
            return [self.disp & 0xFFFFFFFF]
        if self.mode == AM.MEM_IDX:
            return [self.disp & 0xFFFFFFFF, self.idx_reg]
        return []

    def mnem(self):
        rn = _rn(self.reg)
        if self.mode == AM.REG_D:
            return rn
        if self.mode == AM.REG_A:
            return rn
        if self.mode == AM.IMMED:
            return f"#{self.imm}"
        if self.mode == AM.MEM_IND:
            return f"({rn})"
        if self.mode == AM.MEM_POST:
            return f"({rn})+"
        if self.mode == AM.MEM_PRE:
            return f"-({rn})"
        if self.mode == AM.MEM_DISP:
            return f"{self.disp}({rn})"
        if self.mode == AM.MEM_IDX:
            return f"{self.disp}({rn},{_rn(self.idx_reg)})"
        return ""


NONE_OP = Operand(AM.NONE)


def D(n):
    return Operand(AM.REG_D, reg=n)


def A(n):
    return Operand(AM.REG_A, reg=8 + n)


def Imm(v):
    return Operand(AM.IMMED, imm=int(v) & 0xFFFFFFFF)


def Ind(n):
    return Operand(AM.MEM_IND, reg=8 + n)


def Post(n):
    return Operand(AM.MEM_POST, reg=8 + n)


def Pre(n):
    return Operand(AM.MEM_PRE, reg=8 + n)


def Disp(n, d):
    return Operand(AM.MEM_DISP, reg=8 + n, disp=d)


def _ei(opcode, src, dst, sz=False):
    w = (
        (opcode & 0xFF) << 24
        | (src.mode & 0xF) << 20
        | (dst.mode & 0xF) << 16
        | (src.reg & 0xF) << 12
        | (dst.reg & 0xF) << 8
        | (1 if sz else 0) << 7
    )
    parts = [struct.pack(">I", w)]
    for x in src.extra_words():
        parts.append(struct.pack(">I", x & 0xFFFFFFFF))
    for x in dst.extra_words():
        parts.append(struct.pack(">I", x & 0xFFFFFFFF))
    return b"".join(parts)


def _branch(op, addr):
    w = (op << 24) | (AM.IMMED << 20) | (AM.NONE << 16)
    return struct.pack(">II", w, addr & 0xFFFFFFFF)


def _rts():
    return struct.pack(">I", (Op.RTS << 24) | (AM.NONE << 20) | (AM.NONE << 16))


def _halt():
    return struct.pack(">I", (Op.HALT << 24) | (AM.NONE << 20) | (AM.NONE << 16))


def _unlk(an):
    return _ei(Op.UNLK, Operand(AM.REG_A, reg=8 + an), NONE_OP)


def _link(an, d):
    return _ei(Op.LINK, Operand(AM.IMMED, imm=d & 0xFFFFFFFF), Operand(AM.REG_A, reg=8 + an))


# ═══════════════════════════════════════════════════════════════════════════════
#  ЛЕКСЕР
# ═══════════════════════════════════════════════════════════════════════════════

TK = type(
    "TK",
    (),
    {
        k: k
        for k in [
            "INT",
            "BOOL",
            "STR_LIT",
            "IDENT",
            "NUMBER",
            "PLUS",
            "MINUS",
            "STAR",
            "SLASH",
            "PERCENT",
            "EQ",
            "NEQ",
            "LT",
            "LE",
            "GT",
            "GE",
            "AND",
            "OR",
            "NOT",
            "ASSIGN",
            "LPAREN",
            "RPAREN",
            "LBRACE",
            "RBRACE",
            "SEMICOLON",
            "COMMA",
            "COLON",
            "IF",
            "ELSE",
            "WHILE",
            "FOR",
            "RETURN",
            "BREAK",
            "CONTINUE",
            "FN",
            "VAR",
            "CONST",
            "TRUE",
            "FALSE",
            "VOID",
            "EOF",
        ]
    },
)()

KEYWORDS = {
    "if": TK.IF,
    "else": TK.ELSE,
    "while": TK.WHILE,
    "for": TK.FOR,
    "return": TK.RETURN,
    "break": TK.BREAK,
    "continue": TK.CONTINUE,
    "fn": TK.FN,
    "var": TK.VAR,
    "const": TK.CONST,
    "true": TK.TRUE,
    "false": TK.FALSE,
    "int": TK.INT,
    "bool": TK.BOOL,
    "void": TK.VOID,
    "str": "str",
}


@dataclass
class Token:
    kind: str
    value: object
    line: int


class LexError(Exception):
    pass


class ParseError(Exception):
    pass


class CompileError(Exception):
    pass


def lex(src: str):
    tokens = []
    i = 0
    line = 1
    n = len(src)

    def peek(off=0):
        return src[i + off] if i + off < n else ""

    while i < n:
        # пропустить пробелы
        if src[i] in " \t\r":
            i += 1
            continue
        if src[i] == "\n":
            line += 1
            i += 1
            continue
        # однострочный комментарий
        if peek() == "/" and peek(1) == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        # блочный комментарий
        if peek() == "/" and peek(1) == "*":
            i += 2
            while i < n - 1 and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] == "\n":
                    line += 1
                i += 1
            i += 2
            continue
        # строковый литерал
        if src[i] == '"':
            i += 1
            s = []
            while i < n and src[i] != '"':
                if src[i] == "\\":
                    i += 1
                    esc = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(src[i])
                    if esc is None:
                        raise LexError(f"Неизвестный escape \\{src[i]} в строке {line}")
                    s.append(esc)
                else:
                    s.append(src[i])
                i += 1
            if i >= n:
                raise LexError(f"Незакрытая строка в строке {line}")
            i += 1
            tokens.append(Token(TK.STR_LIT, "".join(s), line))
            continue
        # числа
        if src[i].isdigit():
            j = i
            while i < n and src[i].isdigit():
                i += 1
            tokens.append(Token(TK.NUMBER, int(src[j:i]), line))
            continue
        # идентификаторы и ключевые слова
        if src[i].isalpha() or src[i] == "_":
            j = i
            while i < n and (src[i].isalnum() or src[i] == "_"):
                i += 1
            word = src[j:i]
            kind = KEYWORDS.get(word, TK.IDENT)
            if kind == "str":
                kind = TK.IDENT
                word = "str"  # тип str как ident
            tokens.append(Token(kind, word, line))
            continue
        # операторы
        two = src[i : i + 2]
        if two == "==":
            tokens.append(Token(TK.EQ, "==", line))
            i += 2
            continue
        if two == "!=":
            tokens.append(Token(TK.NEQ, "!=", line))
            i += 2
            continue
        if two == "<=":
            tokens.append(Token(TK.LE, "<=", line))
            i += 2
            continue
        if two == ">=":
            tokens.append(Token(TK.GE, ">=", line))
            i += 2
            continue
        if two == "&&":
            tokens.append(Token(TK.AND, "&&", line))
            i += 2
            continue
        if two == "||":
            tokens.append(Token(TK.OR, "||", line))
            i += 2
            continue
        one = src[i]
        simple = {
            "+": TK.PLUS,
            "-": TK.MINUS,
            "*": TK.STAR,
            "/": TK.SLASH,
            "%": TK.PERCENT,
            "<": TK.LT,
            ">": TK.GT,
            "!": TK.NOT,
            "=": TK.ASSIGN,
            "(": TK.LPAREN,
            "(": TK.LPAREN,
            ")": TK.RPAREN,
            "{": TK.LBRACE,
            "}": TK.RBRACE,
            ";": TK.SEMICOLON,
            ",": TK.COMMA,
            ":": TK.COLON,
        }
        if one in simple:
            tokens.append(Token(simple[one], one, line))
            i += 1
            continue
        raise LexError(f"Неожиданный символ {one!r} в строке {line}")

    tokens.append(Token(TK.EOF, None, line))
    return tokens


# ═══════════════════════════════════════════════════════════════════════════════
#  AST
# ═══════════════════════════════════════════════════════════════════════════════


class AstNode:
    """Базовый класс AST-узла."""

    line: int = 0

    def pretty(self, indent=0) -> str:
        raise NotImplementedError


def _ind(n):
    return "  " * n


# ── Типы ─────────────────────────────────────────────────────────────────────
class TypeNode(AstNode):
    def __init__(self, name, line=0):
        self.name = name
        self.line = line

    def pretty(self, i=0):
        return f"{_ind(i)}{self.name}"


# ── Выражения ─────────────────────────────────────────────────────────────────
class Literal(AstNode):
    def __init__(self, value, line=0):
        self.value = value
        self.line = line

    def pretty(self, i=0):
        return f"{_ind(i)}(Lit {self.value!r})"


class Ident(AstNode):
    def __init__(self, name, line=0):
        self.name = name
        self.line = line

    def pretty(self, i=0):
        return f"{_ind(i)}(Ident {self.name})"


class BinOp(AstNode):
    def __init__(self, op, left, right, line=0):
        self.op = op
        self.left = left
        self.right = right
        self.line = line

    def pretty(self, i=0):
        return f"{_ind(i)}(BinOp {self.op}\n" f"{self.left.pretty(i+1)}\n" f"{self.right.pretty(i+1)})"


class UnOp(AstNode):
    def __init__(self, op, operand, line=0):
        self.op = op
        self.operand = operand
        self.line = line

    def pretty(self, i=0):
        return f"{_ind(i)}(UnOp {self.op}\n{self.operand.pretty(i+1)})"


class Call(AstNode):
    def __init__(self, name, args, line=0):
        self.name = name
        self.args = args
        self.line = line

    def pretty(self, i=0):
        args_s = "\n".join(a.pretty(i + 1) for a in self.args)
        return f"{_ind(i)}(Call {self.name}\n{args_s})" if self.args else f"{_ind(i)}(Call {self.name})"


class Assign(AstNode):
    def __init__(self, name, value, line=0):
        self.name = name
        self.value = value
        self.line = line

    def pretty(self, i=0):
        return f"{_ind(i)}(Assign {self.name}\n{self.value.pretty(i+1)})"


# ── Операторы ─────────────────────────────────────────────────────────────────
class VarDecl(AstNode):
    def __init__(self, name, typ, init, is_const=False, line=0):
        self.name = name
        self.typ = typ
        self.init = init
        self.is_const = is_const
        self.line = line

    def pretty(self, i=0):
        kw = "const" if self.is_const else "var"
        init_s = f"\n{self.init.pretty(i+1)}" if self.init else ""
        return f"{_ind(i)}({kw} {self.name} : {self.typ.name}{init_s})"


class Block(AstNode):
    def __init__(self, stmts, line=0):
        self.stmts = stmts
        self.line = line

    def pretty(self, i=0):
        body = "\n".join(s.pretty(i + 1) for s in self.stmts)
        return f"{_ind(i)}(Block\n{body})"


class IfStmt(AstNode):
    def __init__(self, cond, then_, else_, line=0):
        self.cond = cond
        self.then_ = then_
        self.else_ = else_
        self.line = line

    def pretty(self, i=0):
        e = f"\n{_ind(i+1)}(else\n{self.else_.pretty(i+2)})" if self.else_ else ""
        return f"{_ind(i)}(If\n{self.cond.pretty(i+1)}\n{self.then_.pretty(i+1)}{e})"


class WhileStmt(AstNode):
    def __init__(self, cond, body, line=0):
        self.cond = cond
        self.body = body
        self.line = line

    def pretty(self, i=0):
        return f"{_ind(i)}(While\n{self.cond.pretty(i+1)}\n{self.body.pretty(i+1)})"


class ForStmt(AstNode):
    def __init__(self, init, cond, step, body, line=0):
        self.init = init
        self.cond = cond
        self.step = step
        self.body = body
        self.line = line

    def pretty(self, i=0):
        ii = f"\n{self.init.pretty(i+1)}" if self.init else ""
        cc = f"\n{self.cond.pretty(i+1)}" if self.cond else ""
        ss = f"\n{self.step.pretty(i+1)}" if self.step else ""
        return f"{_ind(i)}(For{ii}{cc}{ss}\n{self.body.pretty(i+1)})"


class ReturnStmt(AstNode):
    def __init__(self, value, line=0):
        self.value = value
        self.line = line

    def pretty(self, i=0):
        v = f"\n{self.value.pretty(i+1)}" if self.value else ""
        return f"{_ind(i)}(Return{v})"


class BreakStmt(AstNode):
    def __init__(self, line=0):
        self.line = line

    def pretty(self, i=0):
        return f"{_ind(i)}(Break)"


class ContinueStmt(AstNode):
    def __init__(self, line=0):
        self.line = line

    def pretty(self, i=0):
        return f"{_ind(i)}(Continue)"


class ExprStmt(AstNode):
    def __init__(self, expr, line=0):
        self.expr = expr
        self.line = line

    def pretty(self, i=0):
        return f"{_ind(i)}(ExprStmt\n{self.expr.pretty(i+1)})"


# ── Объявление функции ────────────────────────────────────────────────────────
class Param(AstNode):
    def __init__(self, name, typ, line=0):
        self.name = name
        self.typ = typ
        self.line = line

    def pretty(self, i=0):
        return f"{_ind(i)}(Param {self.name} : {self.typ.name})"


class FnDecl(AstNode):
    def __init__(self, name, params, ret, body, line=0):
        self.name = name
        self.params = params
        self.ret = ret
        self.body = body
        self.line = line

    def pretty(self, i=0):
        ps = "\n".join(p.pretty(i + 1) for p in self.params)
        return f"{_ind(i)}(FnDecl {self.name} -> {self.ret.name}\n" f"{ps}\n{self.body.pretty(i+1)})"


class Program(AstNode):
    def __init__(self, decls, line=0):
        self.decls = decls
        self.line = line

    def pretty(self, i=0):
        return "(Program\n" + "\n".join(d.pretty(i + 1) for d in self.decls) + ")"


# ═══════════════════════════════════════════════════════════════════════════════
#  ПАРСЕР
# ═══════════════════════════════════════════════════════════════════════════════


class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def cur(self):
        return self.tokens[self.pos]

    def peek(self):
        return self.tokens[self.pos + 1] if self.pos + 1 < len(self.tokens) else self.tokens[-1]

    def at(self, *kinds):
        return self.cur().kind in kinds

    def eat(self, kind):
        t = self.cur()
        if t.kind != kind:
            raise ParseError(f"Строка {t.line}: ожидалось {kind!r}, получено {t.kind!r} ({t.value!r})")
        self.pos += 1
        return t

    def maybe(self, kind):
        if self.at(kind):
            return self.eat(kind)
        return None

    # ── Верхний уровень ──────────────────────────────────────────────────────
    def parse_program(self):
        decls = []
        while not self.at(TK.EOF):
            if self.at(TK.FN):
                decls.append(self.parse_fn())
            elif self.at(TK.VAR, TK.CONST):
                decls.append(self.parse_var_decl())
                self.eat(TK.SEMICOLON)
            else:
                raise ParseError(f"Строка {self.cur().line}: ожидалось объявление функции или переменной")
        return Program(decls, line=1)

    def parse_fn(self):
        ln = self.cur().line
        self.eat(TK.FN)
        name = self.eat(TK.IDENT).value
        self.eat(TK.LPAREN)
        params = []
        while not self.at(TK.RPAREN):
            pname = self.eat(TK.IDENT).value
            self.eat(TK.COLON)
            ptyp = self.parse_type()
            params.append(Param(pname, ptyp, line=ln))
            if not self.at(TK.RPAREN):
                self.eat(TK.COMMA)
        self.eat(TK.RPAREN)
        ret = TypeNode("void", line=ln)
        if self.maybe(TK.COLON):
            ret = self.parse_type()
        body = self.parse_block()
        return FnDecl(name, params, ret, body, line=ln)

    def parse_type(self):
        t = self.cur()
        if t.kind in (TK.INT, TK.BOOL, TK.VOID):
            self.pos += 1
            return TypeNode(t.value, line=t.line)
        if t.kind == TK.IDENT and t.value == "str":
            self.pos += 1
            return TypeNode("str", line=t.line)
        raise ParseError(f"Строка {t.line}: ожидался тип (int, bool, str, void)")

    # ── Блок и операторы ─────────────────────────────────────────────────────
    def parse_block(self):
        ln = self.cur().line
        self.eat(TK.LBRACE)
        stmts = []
        while not self.at(TK.RBRACE):
            stmts.append(self.parse_stmt())
        self.eat(TK.RBRACE)
        return Block(stmts, line=ln)

    def parse_stmt(self):
        t = self.cur()
        if t.kind in (TK.VAR, TK.CONST):
            s = self.parse_var_decl()
            self.eat(TK.SEMICOLON)
            return s
        if t.kind == TK.IF:
            return self.parse_if()
        if t.kind == TK.WHILE:
            return self.parse_while()
        if t.kind == TK.FOR:
            return self.parse_for()
        if t.kind == TK.RETURN:
            return self.parse_return()
        if t.kind == TK.BREAK:
            self.eat(TK.BREAK)
            self.eat(TK.SEMICOLON)
            return BreakStmt(line=t.line)
        if t.kind == TK.CONTINUE:
            self.eat(TK.CONTINUE)
            self.eat(TK.SEMICOLON)
            return ContinueStmt(line=t.line)
        if t.kind == TK.LBRACE:
            return self.parse_block()
        # присваивание или вызов функции
        expr = self.parse_expr()
        self.eat(TK.SEMICOLON)
        if isinstance(expr, Assign):
            return expr
        return ExprStmt(expr, line=t.line)

    def parse_var_decl(self):
        ln = self.cur().line
        is_const = self.cur().kind == TK.CONST
        self.pos += 1
        name = self.eat(TK.IDENT).value
        self.eat(TK.COLON)
        typ = self.parse_type()
        init = None
        if self.maybe(TK.ASSIGN):
            init = self.parse_expr()
        return VarDecl(name, typ, init, is_const, line=ln)

    def parse_if(self):
        ln = self.cur().line
        self.eat(TK.IF)
        self.eat(TK.LPAREN)
        cond = self.parse_expr()
        self.eat(TK.RPAREN)
        then_ = self.parse_block()
        else_ = None
        if self.maybe(TK.ELSE):
            if self.at(TK.IF):
                else_ = self.parse_if()
            else:
                else_ = self.parse_block()
        return IfStmt(cond, then_, else_, line=ln)

    def parse_while(self):
        ln = self.cur().line
        self.eat(TK.WHILE)
        self.eat(TK.LPAREN)
        cond = self.parse_expr()
        self.eat(TK.RPAREN)
        body = self.parse_block()
        return WhileStmt(cond, body, line=ln)

    def parse_for(self):
        ln = self.cur().line
        self.eat(TK.FOR)
        self.eat(TK.LPAREN)
        init = None
        if not self.at(TK.SEMICOLON):
            if self.at(TK.VAR, TK.CONST):
                init = self.parse_var_decl()
            else:
                e = self.parse_expr()
                init = e if isinstance(e, Assign) else ExprStmt(e, line=ln)
        self.eat(TK.SEMICOLON)
        cond = None
        if not self.at(TK.SEMICOLON):
            cond = self.parse_expr()
        self.eat(TK.SEMICOLON)
        step = None
        if not self.at(TK.RPAREN):
            e = self.parse_expr()
            step = e if isinstance(e, Assign) else ExprStmt(e, line=ln)
        self.eat(TK.RPAREN)
        body = self.parse_block()
        return ForStmt(init, cond, step, body, line=ln)

    def parse_return(self):
        ln = self.cur().line
        self.eat(TK.RETURN)
        val = None
        if not self.at(TK.SEMICOLON):
            val = self.parse_expr()
        self.eat(TK.SEMICOLON)
        return ReturnStmt(val, line=ln)

    # ── Выражения (Pratt / рекурсивный спуск) ────────────────────────────────
    def parse_expr(self):
        return self.parse_assign()

    def parse_assign(self):
        left = self.parse_or()
        if self.at(TK.ASSIGN):
            if not isinstance(left, Ident):
                raise ParseError(f"Строка {self.cur().line}: левая часть присваивания должна быть переменной")
            ln = self.cur().line
            self.eat(TK.ASSIGN)
            val = self.parse_assign()
            return Assign(left.name, val, line=ln)
        return left

    def _binop(self, ops_map, sub):
        left = sub()
        while self.cur().kind in ops_map:
            t = self.cur()
            op = ops_map[t.kind]
            self.pos += 1
            right = sub()
            left = BinOp(op, left, right, line=t.line)
        return left

    def parse_or(self):
        return self._binop({TK.OR: "||"}, self.parse_and)

    def parse_and(self):
        return self._binop({TK.AND: "&&"}, self.parse_eq)

    def parse_eq(self):
        return self._binop({TK.EQ: "==", TK.NEQ: "!="}, self.parse_rel)

    def parse_rel(self):
        return self._binop({TK.LT: "<", TK.LE: "<=", TK.GT: ">", TK.GE: ">="}, self.parse_add)

    def parse_add(self):
        return self._binop({TK.PLUS: "+", TK.MINUS: "-"}, self.parse_mul)

    def parse_mul(self):
        return self._binop({TK.STAR: "*", TK.SLASH: "/", TK.PERCENT: "%"}, self.parse_unary)

    def parse_unary(self):
        t = self.cur()
        if t.kind == TK.NOT:
            self.eat(TK.NOT)
            return UnOp("!", self.parse_unary(), line=t.line)
        if t.kind == TK.MINUS:
            self.eat(TK.MINUS)
            return UnOp("-", self.parse_unary(), line=t.line)
        return self.parse_primary()

    def parse_primary(self):
        t = self.cur()
        if t.kind == TK.NUMBER:
            self.pos += 1
            return Literal(t.value, line=t.line)
        if t.kind == TK.TRUE:
            self.pos += 1
            return Literal(True, line=t.line)
        if t.kind == TK.FALSE:
            self.pos += 1
            return Literal(False, line=t.line)
        if t.kind == TK.STR_LIT:
            self.pos += 1
            return Literal(t.value, line=t.line)
        if t.kind == TK.IDENT:
            name = t.value
            self.pos += 1
            if self.at(TK.LPAREN):
                self.eat(TK.LPAREN)
                args = []
                while not self.at(TK.RPAREN):
                    args.append(self.parse_expr())
                    if not self.at(TK.RPAREN):
                        self.eat(TK.COMMA)
                self.eat(TK.RPAREN)
                return Call(name, args, line=t.line)
            return Ident(name, line=t.line)
        if t.kind == TK.LPAREN:
            self.eat(TK.LPAREN)
            e = self.parse_expr()
            self.eat(TK.RPAREN)
            return e
        raise ParseError(f"Строка {t.line}: неожиданный токен {t.kind!r} ({t.value!r})")


# ═══════════════════════════════════════════════════════════════════════════════
#  ГЕНЕРАТОР КОДА
# ═══════════════════════════════════════════════════════════════════════════════

# Соглашение о вызовах:
#   Аргументы:  первый int/bool → D0; второй → D1; третий → D2
#               первый str (адрес) → A0; второй str → A1
#   Возврат:    int/bool → D0;  str → A0
#   FP = A6, SP = A7
#   Фрейм:  -4(A6), -8(A6), ... — локальные переменные (4 байта каждая)
#   Сохраняемые caller-save через фрейм callee.


class Scope:
    """Таблица имён одного лексического блока."""

    def __init__(self, parent=None):
        self.parent = parent
        self.vars = {}  # name → (fp_offset, type_name, is_const)

    def define(self, name, offset, typ, is_const=False):
        self.vars[name] = (offset, typ, is_const)

    def lookup(self, name):
        if name in self.vars:
            return self.vars[name]
        if self.parent:
            return self.parent.lookup(name)
        return None


class FnInfo:
    def __init__(self, name, params, ret_type):
        self.name = name
        self.params = params  # list of (name, type_name)
        self.ret_type = ret_type


class CodeGen:
    def __init__(self, inline: bool = False):
        # Режим генерации встроенных функций:
        #   False (по умолчанию) — call-режим: __rt_* как отдельные процедуры,
        #                          каждый вызов builtin транслируется в JSR.
        #   True  (--inline)     — inline-режим: тело builtin вставляется прямо
        #                          на месте вызова без JSR/LINK/UNLK/RTS.
        self.inline = inline

        # Код
        self.code = bytearray()
        self.fixups = []  # (offset_in_code, label_name)
        self.labels = {}  # label_name → code offset

        # Данные
        self.data = bytearray()
        self.str_cache = {}  # строка → dmem offset

        # Текущая функция
        self.fn_info = None
        self.scope = None
        self.fp_offset = 0  # следующий свободный слот фрейма (отрицательный)
        self.frame_size = 0  # итоговый размер фрейма (знаем в конце)

        # Функции верхнего уровня
        self.fns = {}  # name → FnInfo

        # Управление циклами
        self.loop_end_stack = []  # метки break
        self.loop_cont_stack = []  # метки continue

        self._lbl_cnt = 0

        # Встроенная runtime-библиотека
        self._runtime_labels = {}  # будет заполнено в emit_runtime

    # ── Вспомогательные ───────────────────────────────────────────────────────

    def _lbl(self, prefix="L"):
        self._lbl_cnt += 1
        return f"{prefix}_{self._lbl_cnt}"

    def _here(self):
        return len(self.code)

    def _label(self, name):
        self.labels[name] = self._here()

    def _emit(self, b: bytes):
        self.code.extend(b)

    def _branch(self, op, label):
        off = self._here()
        self._emit(_branch(op, 0))
        self.fixups.append((off + 4, label))

    def _jsr(self, label):
        off = self._here()
        self._emit(_branch(Op.JSR, 0))
        self.fixups.append((off + 4, label))

    def _jmp(self, label):
        off = self._here()
        self._emit(_branch(Op.JMP, 0))
        self.fixups.append((off + 4, label))

    def _fixup(self):
        for off, lbl in self.fixups:
            if lbl not in self.labels:
                raise CompileError(f"Неразрешённая метка: {lbl!r}")
            struct.pack_into(">I", self.code, off, self.labels[lbl])
        self.fixups.clear()

    # ── Данные ────────────────────────────────────────────────────────────────

    def _intern_str(self, s: str) -> int:
        """Добавить Pascal-строку в dmem (если ещё нет) и вернуть адрес."""
        if s in self.str_cache:
            return self.str_cache[s]
        addr = len(self.data)
        words = [len(s)] + [ord(c) for c in s]
        self.data.extend(struct.pack(f">{len(words)}I", *words))
        self.str_cache[s] = addr
        return addr

    def _reserve_buf(self, n_chars: int) -> int:
        """Зарезервировать буфер для строки (1 слово длины + n_chars слов)."""
        addr = len(self.data)
        self.data.extend(b"\x00" * (1 + n_chars) * 4)
        return addr

    # ── Локальные переменные ─────────────────────────────────────────────────

    def _alloc_local(self, name, typ, is_const=False):
        self.fp_offset -= 4
        self.scope.define(name, self.fp_offset, typ, is_const)
        return self.fp_offset

    def _var(self, name):
        info = self.scope.lookup(name)
        if info is None:
            raise CompileError(f"Неизвестная переменная: {name!r}")
        return info  # (offset, type, is_const)

    # ── Компиляция программы ─────────────────────────────────────────────

    def compile_program(self, prog: Program):
        # Первый проход: собрать информацию о функциях
        for d in prog.decls:
            if isinstance(d, FnDecl):
                params = [(p.name, p.typ.name) for p in d.params]
                self.fns[d.name] = FnInfo(d.name, params, d.ret.name)

        # Анализ достижимости: какие функции реально нужны
        reached = reachable_fns(prog)
        needed_rt = {n for n in reached if n.startswith("__rt_")}
        needed_user = {n for n in reached if not n.startswith("__rt_")}

        # Адрес 0: JMP main — точка входа всегда по нулевому адресу.
        # Эмулятору достаточно стартовать с PC=0; файл меток не нужен.
        self._label("__entry")
        self._jmp("main")

        # Рантайм — только в call-режиме генерируем __rt_* как отдельные процедуры.
        # В inline-режиме (self.inline=True) __rt_* не нужны: тело builtin
        # вставляется прямо на месте каждого вызова через InlineEmitter.
        import sys as _sys

        if not self.inline:
            RuntimeEmitter(self, _sys.modules[__name__]).emit(needed_rt)

        # Второй проход: только достижимые пользовательские функции
        for d in prog.decls:
            if isinstance(d, FnDecl):
                if d.name in needed_user:
                    self.compile_fn(d)
            else:
                raise CompileError(f"Глобальные var-объявления не поддерживаются (строка {d.line})")

        self._fixup()

    def compile_fn(self, fn: FnDecl):
        self.fn_info = self.fns[fn.name]
        self.scope = Scope()
        self.fp_offset = 0

        self._label(fn.name)

        # Шаг 1: аллоцировать слоты параметров (fp_offset движется вниз)
        d_idx = 0
        a_idx = 0
        param_slots = []
        for pname, ptyp in self.fn_info.params:
            off = self._alloc_local(pname, ptyp)
            if ptyp == "str":
                param_slots.append(("a", a_idx, off))
                a_idx += 1
            else:
                param_slots.append(("d", d_idx, off))
                d_idx += 1

        # Шаг 2: emit link(placeholder) — размер фрейма узнаем после тела
        link_off = self._here()
        self._emit(_link(FP, 0))

        # Шаг 3: сразу после link сохраняем параметры из регистров во фрейм.
        # Никакого splice — код идёт в правильном порядке сразу.
        for kind, reg_idx, off in param_slots:
            if kind == "a":
                self._emit(_ei(Op.MOVE, A(reg_idx), Disp(FP, off)))
            else:
                self._emit(_ei(Op.MOVE, D(reg_idx), Disp(FP, off)))

        # Шаг 4: компилировать тело функции
        self.compile_block(fn.body, fn.ret.name)

        # Шаг 5: неявный return для void-функций
        if fn.ret.name == "void":
            self._emit(_unlk(FP))
            if fn.name == "main":
                self._emit(_halt())
            else:
                self._emit(_rts())

        # Шаг 6: пропатчить link правильным размером фрейма
        frame = (-self.fp_offset + 3) & ~3
        struct.pack_into(">I", self.code, link_off + 4, (-frame) & 0xFFFFFFFF)

        self.fn_info = None
        self.scope = None
        self.fp_offset = 0

    def compile_block(self, block: Block, ret_type: str):
        self.scope = Scope(self.scope)
        saved_fp = self.fp_offset
        for stmt in block.stmts:
            self.compile_stmt(stmt, ret_type)
        # При выходе из блока fp_offset не восстанавливаем —
        # переменные продолжают занимать место во фрейме.
        # (упрощение: все локальные в функции имеют единый фрейм)
        self.scope = self.scope.parent

    def compile_stmt(self, stmt, ret_type: str):
        if isinstance(stmt, VarDecl):
            self.compile_var_decl(stmt)
        elif isinstance(stmt, Assign):
            self.compile_assign(stmt)
        elif isinstance(stmt, IfStmt):
            self.compile_if(stmt, ret_type)
        elif isinstance(stmt, WhileStmt):
            self.compile_while(stmt, ret_type)
        elif isinstance(stmt, ForStmt):
            self.compile_for(stmt, ret_type)
        elif isinstance(stmt, ReturnStmt):
            self.compile_return(stmt)
        elif isinstance(stmt, BreakStmt):
            if not self.loop_end_stack:
                raise CompileError(f"break вне цикла (строка {stmt.line})")
            self._jmp(self.loop_end_stack[-1])
        elif isinstance(stmt, ContinueStmt):
            if not self.loop_cont_stack:
                raise CompileError(f"continue вне цикла (строка {stmt.line})")
            self._jmp(self.loop_cont_stack[-1])
        elif isinstance(stmt, ExprStmt):
            self.compile_expr(stmt.expr)  # результат в D0/A0, игнорируем
        elif isinstance(stmt, Block):
            self.compile_block(stmt, ret_type)
        else:
            raise CompileError(f"Неизвестный оператор: {type(stmt).__name__}")

    def compile_cond(self, expr, lbl_false: str):
        """
        Скомпилировать булево условие и прыгнуть на lbl_false если оно ложно.
        Для сравнений (<, <=, >, >=, ==, !=) генерирует CMP + Bxx напрямую,
        без промежуточного вычисления bool в D0.
        Для остальных выражений использует общий путь: compile_expr + cmp #0.
        """
        # Прямая компиляция сравнений — одна/две инструкции
        if isinstance(expr, BinOp) and expr.op in ("<", "<=", ">", ">=", "==", "!="):
            inv = {"<": Op.BGE, "<=": Op.BGT, ">": Op.BLE, ">=": Op.BLT, "==": Op.BNE, "!=": Op.BEQ}

            # Оптимизация: правый операнд — literal → CMP #imm, D0
            def _as_imm(node):
                if isinstance(node, Literal) and isinstance(node.value, int):
                    return Imm(node.value)
                if isinstance(node, Literal) and isinstance(node.value, bool):
                    return Imm(1 if node.value else 0)
                if (
                    isinstance(node, UnOp)
                    and node.op == "-"
                    and isinstance(node.operand, Literal)
                    and isinstance(node.operand.value, int)
                ):
                    return Imm(-node.operand.value)
                return None

            right_imm = _as_imm(expr.right)
            if right_imm is not None:
                self.compile_expr(expr.left)  # left → D0
                self._emit(_ei(Op.CMP, right_imm, D(0)))  # CMP #imm, D0
                self._branch(inv[expr.op], lbl_false)
                return

            # Общий случай: оба не-immediate
            # Оптимизация: левый — Ident, правый — тоже Ident или literal.
            # Только в этом случае можно загрузить левый в D1 напрямую —
            # правый не будет использовать стек/D1 при вычислении.
            def _is_simple(node):
                return (
                    isinstance(node, Ident)
                    or isinstance(node, Literal)
                    or (isinstance(node, UnOp) and node.op == "-" and isinstance(node.operand, Literal))
                )

            if isinstance(expr.left, Ident) and _is_simple(expr.right):
                info = self._var(expr.left.name)
                off, typ, _ = info
                if typ != "str":
                    right_imm2 = _as_imm(expr.right)
                    if right_imm2 is not None:
                        # left=Ident, right=literal → 2 инструкции
                        self._emit(_ei(Op.MOVE, Disp(FP, off), D(0)))
                        self._emit(_ei(Op.CMP, right_imm2, D(0)))
                    else:
                        # left=Ident, right=Ident → D1=left, D0=right, без стека
                        self._emit(_ei(Op.MOVE, Disp(FP, off), D(1)))  # D1 = left
                        self.compile_expr(expr.right)  # D0 = right (простой load)
                        self._emit(_ei(Op.CMP, D(0), D(1)))
                    self._branch(inv[expr.op], lbl_false)
                    return
            self.compile_expr(expr.left)
            self._emit(_ei(Op.MOVE, D(0), Pre(SP)))
            self.compile_expr(expr.right)
            self._emit(_ei(Op.MOVE, Post(SP), D(1)))
            self._emit(_ei(Op.CMP, D(0), D(1)))  # flags для D1-D0
            self._branch(inv[expr.op], lbl_false)
            return
        # Прямая компиляция && и || тоже с коротким замыканием прямо на lbl_false
        if isinstance(expr, BinOp) and expr.op == "&&":
            self.compile_cond(expr.left, lbl_false)
            self.compile_cond(expr.right, lbl_false)
            return
        if isinstance(expr, BinOp) and expr.op == "||":
            lbl_ok = self._lbl("or_ok")
            # если левое ИСТИННО — пропустить проверку правого
            self.compile_expr(expr.left)
            self._emit(_ei(Op.CMP, Imm(0), D(0)))
            self._branch(Op.BNE, lbl_ok)
            self.compile_cond(expr.right, lbl_false)
            self._label(lbl_ok)
            return
        if isinstance(expr, UnOp) and expr.op == "!":
            # !cond — инвертируем: если sub-выражение ИСТИННО → прыгнуть на false
            lbl_sub_true = self._lbl("not_t")
            lbl_after = self._lbl("not_a")
            self.compile_cond_true(expr.operand, lbl_sub_true)
            # sub-выражение ложно → условие истинно, не прыгаем
            self._jmp(lbl_after)
            self._label(lbl_sub_true)
            self._jmp(lbl_false)
            self._label(lbl_after)
            return
        # Общий случай: вычислить в D0, проверить
        self.compile_expr(expr)
        self._emit(_ei(Op.CMP, Imm(0), D(0)))
        self._branch(Op.BEQ, lbl_false)

    def compile_cond_true(self, expr, lbl_true: str):
        """Прыгнуть на lbl_true если условие ИСТИННО (для реализации !)."""
        if isinstance(expr, BinOp) and expr.op in ("<", "<=", ">", ">=", "==", "!="):
            self.compile_expr(expr.left)
            self._emit(_ei(Op.MOVE, D(0), Pre(SP)))
            self.compile_expr(expr.right)
            self._emit(_ei(Op.MOVE, Post(SP), D(1)))
            self._emit(_ei(Op.CMP, D(0), D(1)))
            fwd = {"<": Op.BLT, "<=": Op.BLE, ">": Op.BGT, ">=": Op.BGE, "==": Op.BEQ, "!=": Op.BNE}
            self._branch(fwd[expr.op], lbl_true)
            return
        self.compile_expr(expr)
        self._emit(_ei(Op.CMP, Imm(0), D(0)))
        self._branch(Op.BNE, lbl_true)

    def compile_var_decl(self, decl: VarDecl):
        off = self._alloc_local(decl.name, decl.typ.name, decl.is_const)
        if decl.init is not None:
            self.compile_expr(decl.init)
            # результат в D0 (int/bool) или A0 (str)
            if decl.typ.name == "str":
                self._emit(_ei(Op.MOVE, A(0), Disp(FP, off)))
            else:
                self._emit(_ei(Op.MOVE, D(0), Disp(FP, off)))
        else:
            if decl.typ.name == "str":
                # var s: str без инициализации — выделить буфер 256 символов в dmem.
                # Адрес буфера сохраняется во фрейм-слоте.
                buf_addr = self._reserve_buf(256)
                self._emit(_ei(Op.MOVE, Imm(buf_addr), Disp(FP, off)))
            else:
                # инициализация нулём для int/bool
                self._emit(_ei(Op.MOVE, Imm(0), Disp(FP, off)))

    def compile_assign(self, stmt: Assign):
        info = self._var(stmt.name)
        off, typ, is_const = info
        if is_const:
            raise CompileError(f"Присваивание константе {stmt.name!r} (строка {stmt.line})")
        self.compile_expr(stmt.value)
        if typ == "str":
            self._emit(_ei(Op.MOVE, A(0), Disp(FP, off)))
        else:
            self._emit(_ei(Op.MOVE, D(0), Disp(FP, off)))

    def compile_if(self, stmt: IfStmt, ret_type):
        lbl_else = self._lbl("else")
        lbl_end = self._lbl("fi")

        self.compile_cond(stmt.cond, lbl_else)  # если ложно → else

        self.compile_block(stmt.then_, ret_type)
        if stmt.else_ is not None:
            self._jmp(lbl_end)

        self._label(lbl_else)
        if stmt.else_ is not None:
            if isinstance(stmt.else_, Block):
                self.compile_block(stmt.else_, ret_type)
            else:  # IfStmt (else if)
                self.compile_if(stmt.else_, ret_type)
            self._label(lbl_end)

    def compile_while(self, stmt: WhileStmt, ret_type):
        lbl_top = self._lbl("wh_top")
        lbl_end = self._lbl("wh_end")
        self.loop_end_stack.append(lbl_end)
        self.loop_cont_stack.append(lbl_top)

        self._label(lbl_top)
        self.compile_cond(stmt.cond, lbl_end)  # если ложно → выход

        self.compile_block(stmt.body, ret_type)
        self._jmp(lbl_top)
        self._label(lbl_end)

        self.loop_end_stack.pop()
        self.loop_cont_stack.pop()

    def compile_for(self, stmt: ForStmt, ret_type):
        lbl_top = self._lbl("for_top")
        lbl_cont = self._lbl("for_cont")
        lbl_end = self._lbl("for_end")
        self.loop_end_stack.append(lbl_end)
        self.loop_cont_stack.append(lbl_cont)

        # Инициализация в отдельном scope
        self.scope = Scope(self.scope)
        if stmt.init:
            self.compile_stmt(stmt.init, ret_type)

        self._label(lbl_top)
        if stmt.cond:
            self.compile_cond(stmt.cond, lbl_end)  # если ложно → выход

        self.compile_block(stmt.body, ret_type)

        self._label(lbl_cont)
        if stmt.step:
            self.compile_stmt(stmt.step, ret_type)
        self._jmp(lbl_top)
        self._label(lbl_end)
        self.scope = self.scope.parent

        self.loop_end_stack.pop()
        self.loop_cont_stack.pop()

    def compile_return(self, stmt: ReturnStmt):
        if stmt.value is not None:
            self.compile_expr(stmt.value)
        self._emit(_unlk(FP))
        if self.fn_info and self.fn_info.name == "main":
            self._emit(_halt())
        else:
            self._emit(_rts())

    # ── Компиляция выражений ─────────────────────────────────────────────────
    # Результат всегда в D0 (int/bool) или A0 (str).

    def compile_expr(self, expr) -> str:
        """Компилирует выражение. Возвращает тип результата ('int','bool','str')."""

        if isinstance(expr, Literal):
            return self._compile_literal(expr)

        if isinstance(expr, Ident):
            return self._compile_ident(expr)

        if isinstance(expr, Assign):
            t = self.compile_expr(expr.value)
            info = self._var(expr.name)
            off, typ, is_const = info
            if is_const:
                raise CompileError(f"Присваивание константе {expr.name!r}")
            if typ == "str":
                self._emit(_ei(Op.MOVE, A(0), Disp(FP, off)))
            else:
                self._emit(_ei(Op.MOVE, D(0), Disp(FP, off)))
            return typ

        if isinstance(expr, UnOp):
            return self._compile_unop(expr)

        if isinstance(expr, BinOp):
            return self._compile_binop(expr)

        if isinstance(expr, Call):
            return self._compile_call(expr)

        raise CompileError(f"Неизвестное выражение: {type(expr).__name__}")

    def _compile_literal(self, lit: Literal):
        if isinstance(lit.value, bool):
            v = 1 if lit.value else 0
            self._emit(_ei(Op.MOVE, Imm(v), D(0)))
            return "bool"
        if isinstance(lit.value, int):
            self._emit(_ei(Op.MOVE, Imm(lit.value), D(0)))
            return "int"
        if isinstance(lit.value, str):
            addr = self._intern_str(lit.value)
            self._emit(_ei(Op.MOVEA, Imm(addr), A(0)))
            return "str"
        raise CompileError(f"Неизвестный тип литерала: {type(lit.value)}")

    def _compile_ident(self, ident: Ident):
        info = self._var(ident.name)
        off, typ, _ = info
        if typ == "str":
            self._emit(_ei(Op.MOVEA, Disp(FP, off), A(0)))
        else:
            self._emit(_ei(Op.MOVE, Disp(FP, off), D(0)))
        return typ

    def _compile_unop(self, expr: UnOp):
        if expr.op == "-" and isinstance(expr.operand, Literal) and isinstance(expr.operand.value, int):
            # Оптимизация: -N → одна инструкция move.l #-N, D0
            self._emit(_ei(Op.MOVE, Imm(-expr.operand.value), D(0)))
            return "int"
        t = self.compile_expr(expr.operand)
        if expr.op == "-":
            # D0 = 0 - D0
            self._emit(_ei(Op.MOVE, D(0), D(1)))
            self._emit(_ei(Op.MOVE, Imm(0), D(0)))
            self._emit(_ei(Op.SUB, D(1), D(0)))
            return "int"
        if expr.op == "!":
            lbl_t = self._lbl("not_t")
            lbl_e = self._lbl("not_e")
            self._emit(_ei(Op.CMP, Imm(0), D(0)))
            self._branch(Op.BEQ, lbl_t)
            self._emit(_ei(Op.MOVE, Imm(0), D(0)))
            self._jmp(lbl_e)
            self._label(lbl_t)
            self._emit(_ei(Op.MOVE, Imm(1), D(0)))
            self._label(lbl_e)
            return "bool"
        raise CompileError(f"Неизвестный унарный оператор: {expr.op}")

    def _compile_binop(self, expr: BinOp):
        op = expr.op

        # Короткое замыкание для && и ||
        if op == "&&":
            lbl_false = self._lbl("and_f")
            lbl_end = self._lbl("and_e")
            self.compile_expr(expr.left)
            self._emit(_ei(Op.CMP, Imm(0), D(0)))
            self._branch(Op.BEQ, lbl_false)
            self.compile_expr(expr.right)
            self._emit(_ei(Op.CMP, Imm(0), D(0)))
            self._branch(Op.BEQ, lbl_false)
            self._emit(_ei(Op.MOVE, Imm(1), D(0)))
            self._jmp(lbl_end)
            self._label(lbl_false)
            self._emit(_ei(Op.MOVE, Imm(0), D(0)))
            self._label(lbl_end)
            return "bool"

        if op == "||":
            lbl_true = self._lbl("or_t")
            lbl_end = self._lbl("or_e")
            self.compile_expr(expr.left)
            self._emit(_ei(Op.CMP, Imm(0), D(0)))
            self._branch(Op.BNE, lbl_true)
            self.compile_expr(expr.right)
            self._emit(_ei(Op.CMP, Imm(0), D(0)))
            self._branch(Op.BNE, lbl_true)
            self._emit(_ei(Op.MOVE, Imm(0), D(0)))
            self._jmp(lbl_end)
            self._label(lbl_true)
            self._emit(_ei(Op.MOVE, Imm(1), D(0)))
            self._label(lbl_end)
            return "bool"

        arith = {"+": Op.ADD, "-": Op.SUB, "*": Op.MUL, "/": Op.DIV}
        cmp_ops = {"==", "!=", "<", "<=", ">", ">="}

        # ── Оптимизация: правый операнд — числовой литерал ──────────────
        # Тогда левый результат уже в D0 и можно применить операцию напрямую,
        # без push/pop для сохранения левого.
        def _right_imm(node):
            """Вернуть Imm(value) если node — числовой/булев литерал или -literal."""
            if isinstance(node, Literal) and isinstance(node.value, int):
                return Imm(node.value)
            if isinstance(node, Literal) and isinstance(node.value, bool):
                return Imm(1 if node.value else 0)
            if (
                isinstance(node, UnOp)
                and node.op == "-"
                and isinstance(node.operand, Literal)
                and isinstance(node.operand.value, int)
            ):
                return Imm(-node.operand.value)
            return None

        right_imm = _right_imm(expr.right)
        if right_imm is not None:
            t_left = self.compile_expr(expr.left)
            if t_left == "str":
                raise CompileError(f"Операция {op!r} не поддерживается для строк")
            # D0 = left; применяем op с immediate-правым
            if op in arith:
                if op == "/":
                    self._emit(_ei(Op.MOVE, right_imm, D(1)))
                    self._emit(_ei(Op.DIV, D(1), D(0)))
                elif op == "-":
                    # D0 - imm: используем SUB imm, D0 → но SUB src,dst = dst-src,
                    # т.е. emit SUB #imm, D0 → D0 = D0 - imm
                    self._emit(_ei(Op.SUB, right_imm, D(0)))
                else:
                    self._emit(_ei(arith[op], right_imm, D(0)))
                return "int"
            if op == "%":
                imm_val = right_imm.imm if right_imm.imm < 0x80000000 else right_imm.imm - 0x100000000
                self._emit(_ei(Op.MOVE, D(0), D(1)))  # D1 = a
                self._emit(_ei(Op.MOVE, right_imm, D(2)))  # D2 = b
                self._emit(_ei(Op.DIV, D(2), D(1)))  # D1 = a/b
                self._emit(_ei(Op.MUL, D(2), D(1)))  # D1 = (a/b)*b
                self._emit(_ei(Op.SUB, D(1), D(0)))  # D0 = a - (a/b)*b
                return "int"
            if op in cmp_ops:
                # CMP right_imm, D0  → flags для D0 - right_imm
                self._emit(_ei(Op.CMP, right_imm, D(0)))
                lbl_t = self._lbl("cmp_t")
                lbl_e = self._lbl("cmp_e")
                branch_map = {"==": Op.BEQ, "!=": Op.BNE, "<": Op.BLT, "<=": Op.BLE, ">": Op.BGT, ">=": Op.BGE}
                self._branch(branch_map[op], lbl_t)
                self._emit(_ei(Op.MOVE, Imm(0), D(0)))
                self._jmp(lbl_e)
                self._label(lbl_t)
                self._emit(_ei(Op.MOVE, Imm(1), D(0)))
                self._label(lbl_e)
                return "bool"

        # ── Общий случай: оба операнда не-immediate ──────────────────────
        # left → D0, push, right → D0, pop left → D1
        t_left = self.compile_expr(expr.left)
        if t_left == "str":
            raise CompileError(f"Операция {op!r} не поддерживается для строк")
        self._emit(_ei(Op.MOVE, D(0), Pre(SP)))  # push left
        self.compile_expr(expr.right)  # right → D0
        self._emit(_ei(Op.MOVE, Post(SP), D(1)))  # pop → D1 (left)
        # D1 = left, D0 = right

        if op in arith:
            if op == "/":
                self._emit(_ei(Op.DIV, D(0), D(1)))
                self._emit(_ei(Op.MOVE, D(1), D(0)))
            else:
                self._emit(_ei(arith[op], D(0), D(1)))
                self._emit(_ei(Op.MOVE, D(1), D(0)))
            return "int"

        if op == "%":
            self._emit(_ei(Op.MOVE, D(1), D(2)))
            self._emit(_ei(Op.MOVE, D(0), D(3)))
            self._emit(_ei(Op.DIV, D(3), D(2)))
            self._emit(_ei(Op.MUL, D(3), D(2)))
            self._emit(_ei(Op.MOVE, D(1), D(0)))
            self._emit(_ei(Op.SUB, D(2), D(0)))
            return "int"

        if op in cmp_ops:
            self._emit(_ei(Op.CMP, D(0), D(1)))  # flags for D1 - D0
            lbl_t = self._lbl("cmp_t")
            lbl_e = self._lbl("cmp_e")
            branch_map = {"==": Op.BEQ, "!=": Op.BNE, "<": Op.BLT, "<=": Op.BLE, ">": Op.BGT, ">=": Op.BGE}
            self._branch(branch_map[op], lbl_t)
            self._emit(_ei(Op.MOVE, Imm(0), D(0)))
            self._jmp(lbl_e)
            self._label(lbl_t)
            self._emit(_ei(Op.MOVE, Imm(1), D(0)))
            self._label(lbl_e)
            return "bool"

        raise CompileError(f"Неизвестный бинарный оператор: {op!r}")

    def _load_ident_to(self, name: str, reg_d: int) -> bool:
        """Загрузить int-переменную name прямо в D(reg_d). Вернуть True если успешно."""
        info = self.scope.lookup(name) if self.scope else None
        if info is None:
            return False
        off, typ, _ = info
        if typ == "str":
            return False
        self._emit(_ei(Op.MOVE, Disp(FP, off), D(reg_d)))
        return True

    def _compile_call(self, call: Call):
        name = call.name

        # ── Встроенные функции ────────────────────────────────────────────
        # В call-режиме (self.inline=False) каждый builtin транслируется
        # в вызов JSR __rt_*.
        # В inline-режиме (self.inline=True) InlineEmitter вставляет тело
        # операции прямо на месте — без JSR, LINK, UNLK, RTS.
        # Подготовка аргументов в регистры одинакова для обоих режимов.
        import sys as _sys

        _ie = InlineEmitter(self, _sys.modules[__name__]) if self.inline else None

        if name == "read_char":
            if self.inline:
                _ie.read_char()
            else:
                self._jsr("__rt_read_char")
            return "int"

        if name == "write_char":
            if len(call.args) != 1:
                raise CompileError("write_char требует 1 аргумент")
            self.compile_expr(call.args[0])  # D0 = символ
            if self.inline:
                _ie.write_char()
            else:
                self._jsr("__rt_write_char")
            return "void"

        if name == "str_len":
            self.compile_expr(call.args[0])  # A0 = строка
            if self.inline:
                _ie.str_len()
            else:
                self._jsr("__rt_str_len")
            return "int"

        if name == "str_get":
            # Подготовка аргументов: A0=строка, D0=индекс
            self.compile_expr(call.args[0])  # A0 = s
            self._emit(_ei(Op.MOVE, A(0), Pre(SP)))  # push A0
            self.compile_expr(call.args[1])  # D0 = i
            self._emit(_ei(Op.MOVEA, Post(SP), A(0)))  # pop A0
            if self.inline:
                _ie.str_get()  # scratch: A1, D1 (D1 сохраняется/восстанавливается)
            else:
                self._jsr("__rt_str_get")
            return "int"

        if name == "str_set":
            # Подготовка аргументов: A0=строка, D0=индекс, D1=символ
            self.compile_expr(call.args[0])  # A0 = s
            self._emit(_ei(Op.MOVE, A(0), Pre(SP)))  # push A0
            self.compile_expr(call.args[1])  # D0 = i
            self._emit(_ei(Op.MOVE, D(0), Pre(SP)))  # push D0
            self.compile_expr(call.args[2])  # D0 = c
            self._emit(_ei(Op.MOVE, D(0), D(1)))  # D1 = c
            self._emit(_ei(Op.MOVE, Post(SP), D(0)))  # pop D0 = i
            self._emit(_ei(Op.MOVEA, Post(SP), A(0)))  # pop A0 = s
            if self.inline:
                _ie.str_set()  # scratch: A1, D2 (D2 сохраняется/восстанавливается)
            else:
                self._jsr("__rt_str_set")
            return "void"

        if name == "str_set_len":
            self.compile_expr(call.args[0])  # A0 = s
            self._emit(_ei(Op.MOVE, A(0), Pre(SP)))  # push A0
            self.compile_expr(call.args[1])  # D0 = n
            self._emit(_ei(Op.MOVEA, Post(SP), A(0)))  # pop A0
            if self.inline:
                _ie.str_set_len()
            else:
                self._jsr("__rt_str_set_len")
            return "void"

        # ── Пользовательские функции ──────────────────────────────────────
        if name not in self.fns:
            raise CompileError(f"Неизвестная функция: {name!r} (строка {call.line})")

        fn = self.fns[name]
        if len(call.args) != len(fn.params):
            raise CompileError(
                f"Функция {name!r}: ожидается {len(fn.params)} аргументов, "
                f"передано {len(call.args)} (строка {call.line})"
            )

        # Вычислить аргументы и разложить по регистрам.
        # Оптимизация: если аргумент один — вычислить прямо в нужный регистр.
        # При нескольких аргументах: вычислить все на стек, затем снять в регистры.
        # Это корректно при вложенных вызовах (каждый compile_expr может
        # перезаписать D0/A0, поэтому предыдущие надо сохранить).
        if len(call.args) == 1:
            # Один аргумент: вычислить сразу в нужный регистр — без push/pop
            _, ptyp = fn.params[0]
            self.compile_expr(call.args[0])
            # Результат уже в D0 (int/bool) или A0 (str) — регистр совпадает
            # с соглашением о вызовах, дополнительных move не нужно
        else:
            # Несколько аргументов: сохранить через стек
            for arg in call.args:
                t = self.compile_expr(arg)
                if t == "str":
                    self._emit(_ei(Op.MOVE, A(0), Pre(SP)))
                else:
                    self._emit(_ei(Op.MOVE, D(0), Pre(SP)))

            d_idx = sum(1 for _, ptyp in fn.params if ptyp != "str") - 1
            a_idx = sum(1 for _, ptyp in fn.params if ptyp == "str") - 1
            for _, ptyp in reversed(fn.params):
                if ptyp == "str":
                    self._emit(_ei(Op.MOVEA, Post(SP), A(a_idx)))
                    a_idx -= 1
                else:
                    self._emit(_ei(Op.MOVE, Post(SP), D(d_idx)))
                    d_idx -= 1

        self._jsr(name)
        return fn.ret_type

    # ── Дизассемблерный листинг ───────────────────────────────────────────────

    def listing(self) -> str:
        from io import StringIO

        # Ширины колонок — фиксированные:
        #   addr : 6 символов (десятичный адрес)
        #   hex  : 32 символа (макс. 4 слова × 8 hex-символов)
        #   mnem : 28 символов (самые длинные мнемоники ~24 символа + запас)
        # Метки выводятся справа от мнемоники в единой выровненной колонке.
        ADDR_W = 6
        HEX_W = 32
        MNEM_W = 28

        out = StringIO()
        code = bytes(self.code)

        # Обратная карта: адрес → список меток.
        # Сортировка: пользовательские метки перед внутренними (__*).
        lbl_by_addr: dict[int, list[str]] = {}
        for name, addr in self.labels.items():
            lbl_by_addr.setdefault(addr, []).append(name)
        for addr in lbl_by_addr:
            lbl_by_addr[addr].sort(key=lambda n: (n.startswith("_"), n))

        offset = 0
        while offset < len(code):
            lbls = lbl_by_addr.get(offset, [])
            mnem, size = _decode(code, offset)
            hx = code[offset : offset + size].hex().upper()

            # Метки справа от мнемоники через "; ", или пусто
            lbl_str = ("; " + ", ".join(lbls)) if lbls else ""

            out.write(f"{offset:{ADDR_W}d}" f"  {hx:<{HEX_W}}" f"  {mnem:<{MNEM_W}}" f"{lbl_str}\n")
            offset += size

        return out.getvalue()


# ── Встроенный дизассемблер ───────────────────────────────────────────────────


def _decode(data, offset):
    if offset + 4 > len(data):
        return "???", 4
    w = struct.unpack_from(">I", data, offset)[0]
    op = (w >> 24) & 0xFF
    sm = (w >> 20) & 0xF
    dm = (w >> 16) & 0xF
    sr = (w >> 12) & 0xF
    dr = (w >> 8) & 0xF
    sz = ".b" if (w >> 7) & 1 else ".l"
    cur = offset + 4

    def rd():
        nonlocal cur
        v = struct.unpack_from(">I", data, cur)[0]
        cur += 4
        return v

    def rds():
        nonlocal cur
        v = struct.unpack_from(">i", data, cur)[0]
        cur += 4
        return v

    def op_str(mode, reg):
        rn = _rn(reg)
        if mode == AM.IMMED:
            return f"#{rd()}"
        if mode == AM.MEM_DISP:
            d = rds()
            return f"{d}({rn})"
        if mode == AM.MEM_IDX:
            d = rds()
            xr = rd()
            return f"{d}({rn},{_rn(xr)})"
        if mode == AM.NONE:
            return ""
        return {AM.REG_D: rn, AM.REG_A: rn, AM.MEM_IND: f"({rn})", AM.MEM_POST: f"({rn})+", AM.MEM_PRE: f"-({rn})"}[
            mode
        ]

    branch_ops = {
        Op.JMP: "jmp",
        Op.JSR: "jsr",
        Op.BEQ: "beq",
        Op.BNE: "bne",
        Op.BLT: "blt",
        Op.BGT: "bgt",
        Op.BLE: "ble",
        Op.BGE: "bge",
        Op.BMI: "bmi",
        Op.BPL: "bpl",
        Op.BCC: "bcc",
        Op.BCS: "bcs",
        Op.BVC: "bvc",
        Op.BVS: "bvs",
        Op.BRA: "bra",
    }
    if op in branch_ops:
        tgt = rd()
        return f"{branch_ops[op]} @{tgt}", cur - offset

    op_names = {
        Op.MOVE: "move",
        Op.MOVEA: "movea",
        Op.ADD: "add",
        Op.SUB: "sub",
        Op.MUL: "mul",
        Op.DIV: "div",
        Op.CMP: "cmp",
        Op.AND: "and",
        Op.OR: "or",
        Op.XOR: "xor",
        Op.NOT: "not",
        Op.ASL: "asl",
        Op.ASR: "asr",
        Op.LSL: "lsl",
        Op.LSR: "lsr",
        Op.LINK: "link",
        Op.UNLK: "unlk",
        Op.RTS: "rts",
        Op.HALT: "halt",
    }
    if op == Op.HALT:
        return "halt", 4
    if op == Op.RTS:
        return "rts", 4
    if op == Op.UNLK:
        s = op_str(sm, sr)
        return f"unlk {s}", cur - offset

    oname = op_names.get(op, f"op{op:02X}")
    s = op_str(sm, sr)
    d = op_str(dm, dr)
    if d:
        return f"{oname}{sz} {s}, {d}", cur - offset
    return f"{oname}{sz} {s}", cur - offset


# ═══════════════════════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ═══════════════════════════════════════════════════════════════════════════════


def compile_file(src_path: str, out_dir: str, inline: bool = False):
    stem = os.path.splitext(os.path.basename(src_path))[0]
    os.makedirs(out_dir, exist_ok=True)

    with open(src_path, encoding="utf-8") as f:
        src = f.read()

    # Лексический анализ
    try:
        tokens = lex(src)
    except LexError as e:
        print(f"Ошибка лексера: {e}", file=sys.stderr)
        sys.exit(1)

    # Синтаксический анализ
    try:
        parser = Parser(tokens)
        ast = parser.parse_program()
    except ParseError as e:
        print(f"Ошибка парсера: {e}", file=sys.stderr)
        sys.exit(1)

    # Вывод AST
    ast_path = os.path.join(out_dir, stem + ".ast")
    with open(ast_path, "w", encoding="utf-8") as f:
        f.write(ast.pretty())
        f.write("\n")
    print(f"AST:    {ast_path}")

    # Кодогенерация
    try:
        cg = CodeGen(inline=inline)
        cg.compile_program(ast)
    except CompileError as e:
        print(f"Ошибка компиляции: {e}", file=sys.stderr)
        sys.exit(1)

    code = bytes(cg.code)
    data = bytes(cg.data)

    # Выходные файлы
    imem_path = os.path.join(out_dir, stem + ".imem")
    dmem_path = os.path.join(out_dir, stem + ".dmem")
    lst_path = os.path.join(out_dir, stem + ".lst")

    with open(imem_path, "wb") as f:
        f.write(code)
    with open(dmem_path, "wb") as f:
        f.write(data)

    with open(lst_path, "w") as f:
        # Таблица меток выводится в начало листинга
        f.write("; Таблица меток:\n")
        for name, addr in sorted(cg.labels.items(), key=lambda x: x[1]):
            f.write(f";   {addr:6d}  {name}\n")
        f.write(";\n")
        f.write(cg.listing())

    mode = "inline" if inline else "call"
    print(f"Режим: {mode}")
    print(f"imem:   {imem_path}  ({len(code)} байт)")
    print(f"dmem:   {dmem_path}  ({len(data)} байт)")
    print(f"lst:    {lst_path}")
    print(f"\nТочка входа: PC=0 → JMP main ({cg.labels.get('main', '?')})")


HELP = """
compiler.py — компилятор языка JavaLight (JL) в бинарный формат M68k Harvard ISA

Использование:
  python compiler.py <source.jl> [output_dir]
  python compiler.py -h | --help

Обязательные аргументы:
  <source.jl>          Исходный файл на языке JL

Необязательные аргументы:
  [output_dir]         Директория для выходных файлов
                       По умолчанию: {default_out!r}
  --inline             Вставлять код встроенных функций (read_char, write_char,
                       str_get и т.д.) прямо на месте вызова без JSR/LINK/RTS.
                       Уменьшает imem для программ с редкими вызовами;
                       увеличивает — для программ с частыми (напр. в цикле).
  -h, --help           Показать эту справку и выйти

Выходные файлы (имя берётся из <source.jl> без расширения):
  <stem>.imem          Бинарный образ памяти команд (instruction memory)
  <stem>.dmem          Бинарный образ памяти данных (data memory)
  <stem>.lst           Листинг кода (включает таблицу меток в начале)
  <stem>.ast           Абстрактное синтаксическое дерево (human-readable)

Константы (из config.py):
  IO_IN  = {io_in:#010x}    Адрес порта ввода  в dmem (memory-mapped)
  IO_OUT = {io_out:#010x}   Адрес порта вывода в dmem (memory-mapped)
  DEFAULT_OUT_DIR = {default_out!r}

Примеры:
  python compiler.py hello.jl
  python compiler.py hello.jl ./build
  python compiler.py programs/sort.jl out/
"""


def run(source_file: str, out_dir: str, inline: bool = False) -> None:
    compile_file(source_file, out_dir, inline=inline)


def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(
            HELP.format(
                default_out=DEFAULT_OUT_DIR,
                io_in=IO_IN,
                io_out=IO_OUT,
            )
        )
        sys.exit(0 if args and args[0] in ("-h", "--help") else 1)

    # Разобрать аргументы: [--inline] <source.jl> [output_dir]
    inline = False
    positional = []
    for arg in args:
        if arg == "--inline":
            inline = True
        elif arg.startswith("-"):
            print(f"Ошибка: неизвестный ключ {arg!r}.", file=sys.stderr)
            print("Запустите с -h для справки.", file=sys.stderr)
            sys.exit(1)
        else:
            positional.append(arg)

    if len(positional) == 0 or len(positional) > 2:
        print("Ошибка: неверные аргументы.", file=sys.stderr)
        print("Запустите с -h для справки.", file=sys.stderr)
        sys.exit(1)

    src_path = positional[0]
    out_dir = positional[1] if len(positional) == 2 else DEFAULT_OUT_DIR
    compile_file(src_path, out_dir, inline=inline)


if __name__ == "__main__":
    main()
