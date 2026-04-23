#!/usr/bin/env python3
"""
compiler.py  —  компилятор языка JavaLight (JL) → M68k-inspired Harvard ISA

Использование:
    python compiler.py <source.jl> <output_dir>

Создаёт файлы:
    <output_dir>/<stem>.imem    — бинарный образ памяти команд
    <output_dir>/<stem>.dmem    — бинарный образ памяти данных
    <output_dir>/<stem>.labels  — таблица меток
    <output_dir>/<stem>.lst     — дизассемблерный листинг
"""

import sys
import os
import struct
import re
from dataclasses import dataclass, field
from typing import Optional

from config import IO_IN, IO_OUT, DEFAULT_OUT_DIR

# ═══════════════════════════════════════════════════════════════════════════════
#  ISA  (встроенная копия isa.py — компилятор самодостаточен)
# ═══════════════════════════════════════════════════════════════════════════════

FP = 6   # A6 = frame pointer
SP = 7   # A7 = stack pointer

class Op:
    MOVE=0x01; MOVEA=0x02
    ADD=0x10;  SUB=0x11;  MUL=0x12;  DIV=0x13;  CMP=0x14
    AND=0x20;  OR=0x21;   XOR=0x22;  NOT=0x23
    ASL=0x30;  ASR=0x31;  LSL=0x32;  LSR=0x33
    JMP=0x40;  JSR=0x41;  RTS=0x42
    BEQ=0x50;  BNE=0x51;  BLT=0x52;  BGT=0x53
    BLE=0x54;  BGE=0x55;  BMI=0x56;  BPL=0x57
    BCC=0x58;  BCS=0x59;  BVC=0x5A;  BVS=0x5B;  BRA=0x5C
    LINK=0x60; UNLK=0x61; HALT=0xFF

class AM:
    REG_D=0x0; REG_A=0x1; IMMED=0x2
    MEM_IND=0x3; MEM_POST=0x4; MEM_PRE=0x5
    MEM_DISP=0x6; MEM_IDX=0x7; NONE=0xF

OP_NAMES = {v: k for k, v in vars(Op).items() if not k.startswith('_')}

def _rn(n): return f"D{n}" if n < 8 else f"A{n-8}"

class Operand:
    def __init__(self, mode, reg=0, imm=0, disp=0, idx_reg=0):
        self.mode=mode; self.reg=reg; self.imm=imm
        self.disp=disp; self.idx_reg=idx_reg
    def extra_words(self):
        if self.mode==AM.IMMED:    return [self.imm & 0xFFFFFFFF]
        if self.mode==AM.MEM_DISP: return [self.disp & 0xFFFFFFFF]
        if self.mode==AM.MEM_IDX:  return [self.disp & 0xFFFFFFFF, self.idx_reg]
        return []
    def mnem(self):
        rn = _rn(self.reg)
        if self.mode==AM.REG_D:    return rn
        if self.mode==AM.REG_A:    return rn
        if self.mode==AM.IMMED:    return f"#{self.imm}"
        if self.mode==AM.MEM_IND:  return f"({rn})"
        if self.mode==AM.MEM_POST: return f"({rn})+"
        if self.mode==AM.MEM_PRE:  return f"-({rn})"
        if self.mode==AM.MEM_DISP: return f"{self.disp}({rn})"
        if self.mode==AM.MEM_IDX:  return f"{self.disp}({rn},{_rn(self.idx_reg)})"
        return ""

NONE_OP = Operand(AM.NONE)

def D(n):       return Operand(AM.REG_D, reg=n)
def A(n):       return Operand(AM.REG_A, reg=8+n)
def Imm(v):     return Operand(AM.IMMED, imm=int(v) & 0xFFFFFFFF)
def Ind(n):     return Operand(AM.MEM_IND,  reg=8+n)
def Post(n):    return Operand(AM.MEM_POST, reg=8+n)
def Pre(n):     return Operand(AM.MEM_PRE,  reg=8+n)
def Disp(n, d): return Operand(AM.MEM_DISP, reg=8+n, disp=d)

def _ei(opcode, src, dst, sz=False):
    w = ((opcode & 0xFF) << 24 | (src.mode & 0xF) << 20 | (dst.mode & 0xF) << 16 |
         (src.reg & 0xF) << 12 | (dst.reg & 0xF) << 8   | (1 if sz else 0) << 7)
    parts = [struct.pack('>I', w)]
    for x in src.extra_words(): parts.append(struct.pack('>I', x & 0xFFFFFFFF))
    for x in dst.extra_words(): parts.append(struct.pack('>I', x & 0xFFFFFFFF))
    return b''.join(parts)

def _branch(op, addr):
    w = (op << 24) | (AM.IMMED << 20) | (AM.NONE << 16)
    return struct.pack('>II', w, addr & 0xFFFFFFFF)

def _rts():  return struct.pack('>I', (Op.RTS  << 24)|(AM.NONE<<20)|(AM.NONE<<16))
def _halt(): return struct.pack('>I', (Op.HALT << 24)|(AM.NONE<<20)|(AM.NONE<<16))
def _unlk(an): return _ei(Op.UNLK, Operand(AM.REG_A, reg=8+an), NONE_OP)
def _link(an, d):
    return _ei(Op.LINK, Operand(AM.IMMED, imm=d & 0xFFFFFFFF), Operand(AM.REG_A, reg=8+an))

# ═══════════════════════════════════════════════════════════════════════════════
#  ЛЕКСЕР
# ═══════════════════════════════════════════════════════════════════════════════

TK = type('TK', (), {k: k for k in [
    'INT', 'BOOL', 'STR_LIT', 'IDENT', 'NUMBER',
    'PLUS', 'MINUS', 'STAR', 'SLASH', 'PERCENT',
    'EQ', 'NEQ', 'LT', 'LE', 'GT', 'GE',
    'AND', 'OR', 'NOT', 'ASSIGN',
    'LPAREN', 'RPAREN', 'LBRACE', 'RBRACE', 'SEMICOLON', 'COMMA', 'COLON',
    'IF', 'ELSE', 'WHILE', 'FOR', 'RETURN', 'BREAK', 'CONTINUE',
    'FN', 'VAR', 'CONST', 'TRUE', 'FALSE', 'VOID',
    'EOF',
]})()

KEYWORDS = {
    'if': TK.IF, 'else': TK.ELSE, 'while': TK.WHILE, 'for': TK.FOR,
    'return': TK.RETURN, 'break': TK.BREAK, 'continue': TK.CONTINUE,
    'fn': TK.FN, 'var': TK.VAR, 'const': TK.CONST,
    'true': TK.TRUE, 'false': TK.FALSE,
    'int': TK.INT, 'bool': TK.BOOL, 'void': TK.VOID, 'str': 'str',
}

@dataclass
class Token:
    kind: str
    value: object
    line: int

class LexError(Exception): pass
class ParseError(Exception): pass
class CompileError(Exception): pass

def lex(src: str):
    tokens = []
    i = 0
    line = 1
    n = len(src)

    def peek(off=0): return src[i+off] if i+off < n else ''

    while i < n:
        # пропустить пробелы
        if src[i] in ' \t\r':
            i += 1; continue
        if src[i] == '\n':
            line += 1; i += 1; continue
        # однострочный комментарий
        if peek() == '/' and peek(1) == '/':
            while i < n and src[i] != '\n': i += 1
            continue
        # блочный комментарий
        if peek() == '/' and peek(1) == '*':
            i += 2
            while i < n - 1 and not (src[i] == '*' and src[i+1] == '/'):
                if src[i] == '\n': line += 1
                i += 1
            i += 2; continue
        # строковый литерал
        if src[i] == '"':
            i += 1; s = []
            while i < n and src[i] != '"':
                if src[i] == '\\':
                    i += 1
                    esc = {'n':'\n','t':'\t','\\':'\\','"':'"'}.get(src[i])
                    if esc is None: raise LexError(f"Неизвестный escape \\{src[i]} в строке {line}")
                    s.append(esc)
                else:
                    s.append(src[i])
                i += 1
            if i >= n: raise LexError(f"Незакрытая строка в строке {line}")
            i += 1
            tokens.append(Token(TK.STR_LIT, ''.join(s), line)); continue
        # числа
        if src[i].isdigit():
            j = i
            while i < n and src[i].isdigit(): i += 1
            tokens.append(Token(TK.NUMBER, int(src[j:i]), line)); continue
        # идентификаторы и ключевые слова
        if src[i].isalpha() or src[i] == '_':
            j = i
            while i < n and (src[i].isalnum() or src[i] == '_'): i += 1
            word = src[j:i]
            kind = KEYWORDS.get(word, TK.IDENT)
            if kind == 'str': kind = TK.IDENT; word = 'str'  # тип str как ident
            tokens.append(Token(kind, word, line)); continue
        # операторы
        two = src[i:i+2]
        if two == '==': tokens.append(Token(TK.EQ,  '==', line)); i+=2; continue
        if two == '!=': tokens.append(Token(TK.NEQ, '!=', line)); i+=2; continue
        if two == '<=': tokens.append(Token(TK.LE,  '<=', line)); i+=2; continue
        if two == '>=': tokens.append(Token(TK.GE,  '>=', line)); i+=2; continue
        if two == '&&': tokens.append(Token(TK.AND, '&&', line)); i+=2; continue
        if two == '||': tokens.append(Token(TK.OR,  '||', line)); i+=2; continue
        one = src[i]
        simple = {
            '+':TK.PLUS, '-':TK.MINUS, '*':TK.STAR, '/':TK.SLASH, '%':TK.PERCENT,
            '<':TK.LT,   '>':TK.GT,    '!':TK.NOT,  '=':TK.ASSIGN,
            '(':TK.LPAREN,'(':TK.LPAREN,')':TK.RPAREN,
            '{':TK.LBRACE,'}':TK.RBRACE,
            ';':TK.SEMICOLON,',':TK.COMMA,':':TK.COLON,
        }
        if one in simple:
            tokens.append(Token(simple[one], one, line)); i += 1; continue
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

def _ind(n): return '  ' * n

# ── Типы ─────────────────────────────────────────────────────────────────────
class TypeNode(AstNode):
    def __init__(self, name, line=0):
        self.name = name; self.line = line
    def pretty(self, i=0): return f"{_ind(i)}{self.name}"

# ── Выражения ─────────────────────────────────────────────────────────────────
class Literal(AstNode):
    def __init__(self, value, line=0):
        self.value = value; self.line = line
    def pretty(self, i=0): return f"{_ind(i)}(Lit {self.value!r})"

class Ident(AstNode):
    def __init__(self, name, line=0):
        self.name = name; self.line = line
    def pretty(self, i=0): return f"{_ind(i)}(Ident {self.name})"

class BinOp(AstNode):
    def __init__(self, op, left, right, line=0):
        self.op = op; self.left = left; self.right = right; self.line = line
    def pretty(self, i=0):
        return (f"{_ind(i)}(BinOp {self.op}\n"
                f"{self.left.pretty(i+1)}\n"
                f"{self.right.pretty(i+1)})")

class UnOp(AstNode):
    def __init__(self, op, operand, line=0):
        self.op = op; self.operand = operand; self.line = line
    def pretty(self, i=0):
        return f"{_ind(i)}(UnOp {self.op}\n{self.operand.pretty(i+1)})"

class Call(AstNode):
    def __init__(self, name, args, line=0):
        self.name = name; self.args = args; self.line = line
    def pretty(self, i=0):
        args_s = '\n'.join(a.pretty(i+1) for a in self.args)
        return f"{_ind(i)}(Call {self.name}\n{args_s})" if self.args else f"{_ind(i)}(Call {self.name})"

class Assign(AstNode):
    def __init__(self, name, value, line=0):
        self.name = name; self.value = value; self.line = line
    def pretty(self, i=0):
        return f"{_ind(i)}(Assign {self.name}\n{self.value.pretty(i+1)})"

# ── Операторы ─────────────────────────────────────────────────────────────────
class VarDecl(AstNode):
    def __init__(self, name, typ, init, is_const=False, line=0):
        self.name = name; self.typ = typ; self.init = init
        self.is_const = is_const; self.line = line
    def pretty(self, i=0):
        kw = 'const' if self.is_const else 'var'
        init_s = f"\n{self.init.pretty(i+1)}" if self.init else ""
        return f"{_ind(i)}({kw} {self.name} : {self.typ.name}{init_s})"

class Block(AstNode):
    def __init__(self, stmts, line=0):
        self.stmts = stmts; self.line = line
    def pretty(self, i=0):
        body = '\n'.join(s.pretty(i+1) for s in self.stmts)
        return f"{_ind(i)}(Block\n{body})"

class IfStmt(AstNode):
    def __init__(self, cond, then_, else_, line=0):
        self.cond = cond; self.then_ = then_; self.else_ = else_; self.line = line
    def pretty(self, i=0):
        e = f"\n{_ind(i+1)}(else\n{self.else_.pretty(i+2)})" if self.else_ else ""
        return f"{_ind(i)}(If\n{self.cond.pretty(i+1)}\n{self.then_.pretty(i+1)}{e})"

class WhileStmt(AstNode):
    def __init__(self, cond, body, line=0):
        self.cond = cond; self.body = body; self.line = line
    def pretty(self, i=0):
        return f"{_ind(i)}(While\n{self.cond.pretty(i+1)}\n{self.body.pretty(i+1)})"

class ForStmt(AstNode):
    def __init__(self, init, cond, step, body, line=0):
        self.init = init; self.cond = cond; self.step = step
        self.body = body; self.line = line
    def pretty(self, i=0):
        ii = f"\n{self.init.pretty(i+1)}" if self.init else ""
        cc = f"\n{self.cond.pretty(i+1)}" if self.cond else ""
        ss = f"\n{self.step.pretty(i+1)}" if self.step else ""
        return f"{_ind(i)}(For{ii}{cc}{ss}\n{self.body.pretty(i+1)})"

class ReturnStmt(AstNode):
    def __init__(self, value, line=0):
        self.value = value; self.line = line
    def pretty(self, i=0):
        v = f"\n{self.value.pretty(i+1)}" if self.value else ""
        return f"{_ind(i)}(Return{v})"

class BreakStmt(AstNode):
    def __init__(self, line=0): self.line = line
    def pretty(self, i=0): return f"{_ind(i)}(Break)"

class ContinueStmt(AstNode):
    def __init__(self, line=0): self.line = line
    def pretty(self, i=0): return f"{_ind(i)}(Continue)"

class ExprStmt(AstNode):
    def __init__(self, expr, line=0):
        self.expr = expr; self.line = line
    def pretty(self, i=0): return f"{_ind(i)}(ExprStmt\n{self.expr.pretty(i+1)})"

# ── Объявление функции ────────────────────────────────────────────────────────
class Param(AstNode):
    def __init__(self, name, typ, line=0):
        self.name = name; self.typ = typ; self.line = line
    def pretty(self, i=0): return f"{_ind(i)}(Param {self.name} : {self.typ.name})"

class FnDecl(AstNode):
    def __init__(self, name, params, ret, body, line=0):
        self.name = name; self.params = params; self.ret = ret
        self.body = body; self.line = line
    def pretty(self, i=0):
        ps = '\n'.join(p.pretty(i+1) for p in self.params)
        return (f"{_ind(i)}(FnDecl {self.name} -> {self.ret.name}\n"
                f"{ps}\n{self.body.pretty(i+1)})")

class Program(AstNode):
    def __init__(self, decls, line=0):
        self.decls = decls; self.line = line
    def pretty(self, i=0):
        return "(Program\n" + '\n'.join(d.pretty(i+1) for d in self.decls) + ")"

# ═══════════════════════════════════════════════════════════════════════════════
#  ПАРСЕР
# ═══════════════════════════════════════════════════════════════════════════════

class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def cur(self):  return self.tokens[self.pos]
    def peek(self): return self.tokens[self.pos + 1] if self.pos + 1 < len(self.tokens) else self.tokens[-1]
    def at(self, *kinds): return self.cur().kind in kinds

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
        ret = TypeNode('void', line=ln)
        if self.maybe(TK.COLON):
            ret = self.parse_type()
        body = self.parse_block()
        return FnDecl(name, params, ret, body, line=ln)

    def parse_type(self):
        t = self.cur()
        if t.kind in (TK.INT, TK.BOOL, TK.VOID):
            self.pos += 1
            return TypeNode(t.value, line=t.line)
        if t.kind == TK.IDENT and t.value == 'str':
            self.pos += 1
            return TypeNode('str', line=t.line)
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
            s = self.parse_var_decl(); self.eat(TK.SEMICOLON); return s
        if t.kind == TK.IF:     return self.parse_if()
        if t.kind == TK.WHILE:  return self.parse_while()
        if t.kind == TK.FOR:    return self.parse_for()
        if t.kind == TK.RETURN: return self.parse_return()
        if t.kind == TK.BREAK:
            self.eat(TK.BREAK); self.eat(TK.SEMICOLON)
            return BreakStmt(line=t.line)
        if t.kind == TK.CONTINUE:
            self.eat(TK.CONTINUE); self.eat(TK.SEMICOLON)
            return ContinueStmt(line=t.line)
        if t.kind == TK.LBRACE: return self.parse_block()
        # присваивание или вызов функции
        expr = self.parse_expr()
        self.eat(TK.SEMICOLON)
        if isinstance(expr, Assign): return expr
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
        self.eat(TK.IF); self.eat(TK.LPAREN)
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
        self.eat(TK.WHILE); self.eat(TK.LPAREN)
        cond = self.parse_expr()
        self.eat(TK.RPAREN)
        body = self.parse_block()
        return WhileStmt(cond, body, line=ln)

    def parse_for(self):
        ln = self.cur().line
        self.eat(TK.FOR); self.eat(TK.LPAREN)
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
            ln = self.cur().line; self.eat(TK.ASSIGN)
            val = self.parse_assign()
            return Assign(left.name, val, line=ln)
        return left

    def _binop(self, ops_map, sub):
        left = sub()
        while self.cur().kind in ops_map:
            t = self.cur(); op = ops_map[t.kind]; self.pos += 1
            right = sub()
            left = BinOp(op, left, right, line=t.line)
        return left

    def parse_or(self):
        return self._binop({TK.OR: '||'}, self.parse_and)
    def parse_and(self):
        return self._binop({TK.AND: '&&'}, self.parse_eq)
    def parse_eq(self):
        return self._binop({TK.EQ: '==', TK.NEQ: '!='}, self.parse_rel)
    def parse_rel(self):
        return self._binop({TK.LT:'<', TK.LE:'<=', TK.GT:'>', TK.GE:'>='}, self.parse_add)
    def parse_add(self):
        return self._binop({TK.PLUS:'+', TK.MINUS:'-'}, self.parse_mul)
    def parse_mul(self):
        return self._binop({TK.STAR:'*', TK.SLASH:'/', TK.PERCENT:'%'}, self.parse_unary)

    def parse_unary(self):
        t = self.cur()
        if t.kind == TK.NOT:
            self.eat(TK.NOT); return UnOp('!', self.parse_unary(), line=t.line)
        if t.kind == TK.MINUS:
            self.eat(TK.MINUS); return UnOp('-', self.parse_unary(), line=t.line)
        return self.parse_primary()

    def parse_primary(self):
        t = self.cur()
        if t.kind == TK.NUMBER:
            self.pos += 1; return Literal(t.value, line=t.line)
        if t.kind == TK.TRUE:
            self.pos += 1; return Literal(True,  line=t.line)
        if t.kind == TK.FALSE:
            self.pos += 1; return Literal(False, line=t.line)
        if t.kind == TK.STR_LIT:
            self.pos += 1; return Literal(t.value, line=t.line)
        if t.kind == TK.IDENT:
            name = t.value; self.pos += 1
            if self.at(TK.LPAREN):
                self.eat(TK.LPAREN)
                args = []
                while not self.at(TK.RPAREN):
                    args.append(self.parse_expr())
                    if not self.at(TK.RPAREN): self.eat(TK.COMMA)
                self.eat(TK.RPAREN)
                return Call(name, args, line=t.line)
            return Ident(name, line=t.line)
        if t.kind == TK.LPAREN:
            self.eat(TK.LPAREN); e = self.parse_expr(); self.eat(TK.RPAREN); return e
        raise ParseError(f"Строка {t.line}: неожиданный токен {t.kind!r} ({t.value!r})")

# ═══════════════════════════════════════════════════════════════════════════════
#  ГЕНЕРАТОР КОДА
# ═══════════════════════════════════════════════════════════════════════════════

BUILTINS = {
    'read_char', 'write_char',
    'str_len', 'str_get', 'str_set', 'str_set_len',
}

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
        self.vars = {}        # name → (fp_offset, type_name, is_const)

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
        self.name      = name
        self.params    = params    # list of (name, type_name)
        self.ret_type  = ret_type

class CodeGen:
    def __init__(self):
        # Код
        self.code      = bytearray()
        self.fixups    = []        # (offset_in_code, label_name)
        self.labels    = {}        # label_name → code offset

        # Данные
        self.data      = bytearray()
        self.str_cache = {}        # строка → dmem offset

        # Текущая функция
        self.fn_info    = None
        self.scope      = None
        self.fp_offset  = 0        # следующий свободный слот фрейма (отрицательный)
        self.frame_size = 0        # итоговый размер фрейма (знаем в конце)

        # Функции верхнего уровня
        self.fns = {}              # name → FnInfo

        # Управление циклами
        self.loop_end_stack   = []   # метки break
        self.loop_cont_stack  = []   # метки continue

        self._lbl_cnt = 0

        # Встроенная runtime-библиотека
        self._runtime_labels = {}    # будет заполнено в emit_runtime

    # ── Вспомогательные ───────────────────────────────────────────────────────

    def _lbl(self, prefix='L'):
        self._lbl_cnt += 1
        return f"{prefix}_{self._lbl_cnt}"

    def _here(self): return len(self.code)

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
            struct.pack_into('>I', self.code, off, self.labels[lbl])
        self.fixups.clear()

    # ── Данные ────────────────────────────────────────────────────────────────

    def _intern_str(self, s: str) -> int:
        """Добавить Pascal-строку в dmem (если ещё нет) и вернуть адрес."""
        if s in self.str_cache:
            return self.str_cache[s]
        addr = len(self.data)
        words = [len(s)] + [ord(c) for c in s]
        self.data.extend(struct.pack(f'>{len(words)}I', *words))
        self.str_cache[s] = addr
        return addr

    def _reserve_buf(self, n_chars: int) -> int:
        """Зарезервировать буфер для строки (1 слово длины + n_chars слов)."""
        addr = len(self.data)
        self.data.extend(b'\x00' * (1 + n_chars) * 4)
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

    # ── Встроенная рантайм-библиотека ─────────────────────────────────────────
    # Генерируется один раз перед всеми пользовательскими функциями.

    def emit_runtime(self):
        """
        write_char(D0) → void
        read_char()    → D0
        str_len(A0)    → D0
        str_get(A0,D0) → D0   (A0=строка, D0=индекс)
        str_set(A0,D0,D1)     (A0=строка, D0=индекс, D1=символ)
        str_set_len(A0,D0)    (A0=строка, D0=длина)
        print_str_rt(A0)      внутренняя: печатает Pascal-строку
        read_line_rt(A0,D0)   внутренняя: читает строку, макс D0 символов → длина в D0
        """

        # ── write_char(D0) ────────────────────────────────────────────────
        self._label('__rt_write_char')
        self._emit(_ei(Op.MOVEA, Imm(IO_OUT), A(5)))
        self._emit(_ei(Op.MOVE,  D(0), Ind(5)))
        self._emit(_rts())

        # ── read_char() → D0 ─────────────────────────────────────────────
        self._label('__rt_read_char')
        self._emit(_ei(Op.MOVEA, Imm(IO_IN), A(5)))
        self._emit(_ei(Op.MOVE,  Ind(5), D(0)))
        self._emit(_rts())

        # ── str_len(A0) → D0 ─────────────────────────────────────────────
        self._label('__rt_str_len')
        self._emit(_ei(Op.MOVE, Ind(0), D(0)))   # D0 = mem[A0] = length word
        self._emit(_rts())

        # ── str_get(A0, D0) → D0  addr = A0 + (D0+1)*4 ──────────────────
        self._label('__rt_str_get')
        self._emit(_link(FP, -8))
        self._emit(_ei(Op.MOVE, D(1), Disp(FP, -4)))
        # A1 = A0 + 4 + D0*4
        self._emit(_ei(Op.MOVEA, A(0), A(1)))
        self._emit(_ei(Op.ADD, Imm(4), A(1)))      # skip length word
        self._emit(_ei(Op.MOVE, D(0), D(1)))
        self._emit(_ei(Op.MUL, Imm(4), D(1)))
        self._emit(_ei(Op.ADD, D(1), A(1)))
        self._emit(_ei(Op.MOVE, Ind(1), D(0)))    # D0 = char
        self._emit(_ei(Op.MOVE, Disp(FP, -4), D(1)))
        self._emit(_unlk(FP))
        self._emit(_rts())

        # ── str_set(A0, D0, D1)  mem[A0+4+D0*4] = D1 ────────────────────
        self._label('__rt_str_set')
        self._emit(_link(FP, -8))
        self._emit(_ei(Op.MOVE, D(2), Disp(FP, -4)))
        self._emit(_ei(Op.MOVEA, A(0), A(1)))
        self._emit(_ei(Op.ADD, Imm(4), A(1)))
        self._emit(_ei(Op.MOVE, D(0), D(2)))
        self._emit(_ei(Op.MUL, Imm(4), D(2)))
        self._emit(_ei(Op.ADD, D(2), A(1)))
        self._emit(_ei(Op.MOVE, D(1), Ind(1)))
        self._emit(_ei(Op.MOVE, Disp(FP, -4), D(2)))
        self._emit(_unlk(FP))
        self._emit(_rts())

        # ── str_set_len(A0, D0) ───────────────────────────────────────────
        self._label('__rt_str_set_len')
        self._emit(_ei(Op.MOVE, D(0), Ind(0)))
        self._emit(_rts())

        # ── print_str_rt(A0) ──────────────────────────────────────────────
        self._label('__rt_print_str')
        self._emit(_link(FP, -16))
        self._emit(_ei(Op.MOVE, D(1), Disp(FP, -4)))
        self._emit(_ei(Op.MOVE, D(2), Disp(FP, -8)))
        self._emit(_ei(Op.MOVEA, A(0), A(1)))
        self._emit(_ei(Op.MOVE, Post(1), D(1)))   # D1 = len; A1 += 4
        self._emit(_ei(Op.MOVE, Imm(0), D(2)))    # i = 0
        lp = self._lbl('ps_loop')
        dn = self._lbl('ps_done')
        self._label(lp)
        self._emit(_ei(Op.CMP, D(1), D(2)))
        self._branch(Op.BGE, dn)
        self._emit(_ei(Op.MOVE, Post(1), D(0)))
        self._jsr('__rt_write_char')
        self._emit(_ei(Op.ADD, Imm(1), D(2)))
        self._jmp(lp)
        self._label(dn)
        self._emit(_ei(Op.MOVE, Disp(FP, -4), D(1)))
        self._emit(_ei(Op.MOVE, Disp(FP, -8), D(2)))
        self._emit(_unlk(FP))
        self._emit(_rts())

        # ── read_line_rt(A0, D0=maxlen) → D0=actual_len ──────────────────
        self._label('__rt_read_line')
        self._emit(_link(FP, -16))
        self._emit(_ei(Op.MOVE, D(1), Disp(FP, -4)))
        self._emit(_ei(Op.MOVE, D(2), Disp(FP, -8)))
        self._emit(_ei(Op.MOVE, D(0), D(1)))       # D1 = maxlen
        self._emit(_ei(Op.MOVEA, A(0), A(1)))
        self._emit(_ei(Op.ADD, Imm(4), A(1)))      # A1 = A0+4 (chars start)
        self._emit(_ei(Op.MOVE, Imm(0), D(2)))     # i = 0
        rl = self._lbl('rl_loop')
        rs = self._lbl('rl_store')
        self._label(rl)
        self._emit(_ei(Op.CMP, D(1), D(2)))
        self._branch(Op.BGE, rs)
        self._jsr('__rt_read_char')
        self._emit(_ei(Op.CMP, Imm(0xFFFFFFFF), D(0)))
        self._branch(Op.BEQ, rs)
        self._emit(_ei(Op.CMP, Imm(10), D(0)))    # '\n'
        self._branch(Op.BEQ, rs)
        self._emit(_ei(Op.MOVE, D(0), Post(1)))
        self._emit(_ei(Op.ADD, Imm(1), D(2)))
        self._jmp(rl)
        self._label(rs)
        self._emit(_ei(Op.MOVE, D(2), Ind(0)))    # store length
        self._emit(_ei(Op.MOVE, D(2), D(0)))      # return value
        self._emit(_ei(Op.MOVE, Disp(FP, -4), D(1)))
        self._emit(_ei(Op.MOVE, Disp(FP, -8), D(2)))
        self._emit(_unlk(FP))
        self._emit(_rts())

    # ── Компиляция программы ─────────────────────────────────────────────────

    def compile_program(self, prog: Program):
        # Первый проход: собрать информацию о функциях
        for d in prog.decls:
            if isinstance(d, FnDecl):
                params = [(p.name, p.typ.name) for p in d.params]
                self.fns[d.name] = FnInfo(d.name, params, d.ret.name)

        # Рантайм-библиотека идёт первой
        self.emit_runtime()

        # Второй проход: кодогенерация
        for d in prog.decls:
            if isinstance(d, FnDecl):
                self.compile_fn(d)
            else:
                raise CompileError(f"Глобальные var-объявления не поддерживаются (строка {d.line})")

        self._fixup()

    def compile_fn(self, fn: FnDecl):
        self.fn_info   = self.fns[fn.name]
        self.scope     = Scope()
        self.fp_offset = 0

        self._label(fn.name)

        # Шаг 1: аллоцировать слоты параметров (fp_offset движется вниз)
        d_idx = 0
        a_idx = 0
        param_slots = []
        for pname, ptyp in self.fn_info.params:
            off = self._alloc_local(pname, ptyp)
            if ptyp == 'str':
                param_slots.append(('a', a_idx, off))
                a_idx += 1
            else:
                param_slots.append(('d', d_idx, off))
                d_idx += 1

        # Шаг 2: emit link(placeholder) — размер фрейма узнаем после тела
        link_off = self._here()
        self._emit(_link(FP, 0))

        # Шаг 3: сразу после link сохраняем параметры из регистров во фрейм.
        # Никакого splice — код идёт в правильном порядке сразу.
        for kind, reg_idx, off in param_slots:
            if kind == 'a':
                self._emit(_ei(Op.MOVE, A(reg_idx), Disp(FP, off)))
            else:
                self._emit(_ei(Op.MOVE, D(reg_idx), Disp(FP, off)))

        # Шаг 4: компилировать тело функции
        self.compile_block(fn.body, fn.ret.name)

        # Шаг 5: неявный return для void-функций
        if fn.ret.name == 'void':
            self._emit(_unlk(FP))
            if fn.name == 'main':
                self._emit(_halt())
            else:
                self._emit(_rts())

        # Шаг 6: пропатчить link правильным размером фрейма
        frame = (-self.fp_offset + 3) & ~3
        struct.pack_into('>I', self.code, link_off + 4, (-frame) & 0xFFFFFFFF)

        self.fn_info   = None
        self.scope     = None
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
            self.compile_expr(stmt.expr)   # результат в D0/A0, игнорируем
        elif isinstance(stmt, Block):
            self.compile_block(stmt, ret_type)
        else:
            raise CompileError(f"Неизвестный оператор: {type(stmt).__name__}")

    def compile_var_decl(self, decl: VarDecl):
        off = self._alloc_local(decl.name, decl.typ.name, decl.is_const)
        if decl.init is not None:
            self.compile_expr(decl.init)
            # результат в D0 (int/bool) или A0 (str)
            if decl.typ.name == 'str':
                self._emit(_ei(Op.MOVE, A(0), Disp(FP, off)))
            else:
                self._emit(_ei(Op.MOVE, D(0), Disp(FP, off)))
        else:
            if decl.typ.name == 'str':
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
        if typ == 'str':
            self._emit(_ei(Op.MOVE, A(0), Disp(FP, off)))
        else:
            self._emit(_ei(Op.MOVE, D(0), Disp(FP, off)))

    def compile_if(self, stmt: IfStmt, ret_type):
        lbl_else = self._lbl('else')
        lbl_end  = self._lbl('fi')

        self.compile_expr(stmt.cond)               # результат в D0
        self._emit(_ei(Op.CMP, Imm(0), D(0)))
        self._branch(Op.BEQ, lbl_else)             # if false → else

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
        lbl_top  = self._lbl('wh_top')
        lbl_end  = self._lbl('wh_end')
        self.loop_end_stack.append(lbl_end)
        self.loop_cont_stack.append(lbl_top)

        self._label(lbl_top)
        self.compile_expr(stmt.cond)
        self._emit(_ei(Op.CMP, Imm(0), D(0)))
        self._branch(Op.BEQ, lbl_end)

        self.compile_block(stmt.body, ret_type)
        self._jmp(lbl_top)
        self._label(lbl_end)

        self.loop_end_stack.pop()
        self.loop_cont_stack.pop()

    def compile_for(self, stmt: ForStmt, ret_type):
        lbl_top  = self._lbl('for_top')
        lbl_cont = self._lbl('for_cont')
        lbl_end  = self._lbl('for_end')
        self.loop_end_stack.append(lbl_end)
        self.loop_cont_stack.append(lbl_cont)

        # Инициализация в отдельном scope
        self.scope = Scope(self.scope)
        if stmt.init:
            self.compile_stmt(stmt.init, ret_type)

        self._label(lbl_top)
        if stmt.cond:
            self.compile_expr(stmt.cond)
            self._emit(_ei(Op.CMP, Imm(0), D(0)))
            self._branch(Op.BEQ, lbl_end)

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
        if self.fn_info and self.fn_info.name == 'main':
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
            if typ == 'str':
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
            return 'bool'
        if isinstance(lit.value, int):
            self._emit(_ei(Op.MOVE, Imm(lit.value), D(0)))
            return 'int'
        if isinstance(lit.value, str):
            addr = self._intern_str(lit.value)
            self._emit(_ei(Op.MOVEA, Imm(addr), A(0)))
            return 'str'
        raise CompileError(f"Неизвестный тип литерала: {type(lit.value)}")

    def _compile_ident(self, ident: Ident):
        info = self._var(ident.name)
        off, typ, _ = info
        if typ == 'str':
            self._emit(_ei(Op.MOVEA, Disp(FP, off), A(0)))
        else:
            self._emit(_ei(Op.MOVE, Disp(FP, off), D(0)))
        return typ

    def _compile_unop(self, expr: UnOp):
        t = self.compile_expr(expr.operand)
        if expr.op == '-':
            # D0 = 0 - D0
            self._emit(_ei(Op.MOVE, D(0), D(1)))
            self._emit(_ei(Op.MOVE, Imm(0), D(0)))
            self._emit(_ei(Op.SUB, D(1), D(0)))
            return 'int'
        if expr.op == '!':
            lbl_t = self._lbl('not_t')
            lbl_e = self._lbl('not_e')
            self._emit(_ei(Op.CMP, Imm(0), D(0)))
            self._branch(Op.BEQ, lbl_t)
            self._emit(_ei(Op.MOVE, Imm(0), D(0)))
            self._jmp(lbl_e)
            self._label(lbl_t)
            self._emit(_ei(Op.MOVE, Imm(1), D(0)))
            self._label(lbl_e)
            return 'bool'
        raise CompileError(f"Неизвестный унарный оператор: {expr.op}")

    def _compile_binop(self, expr: BinOp):
        op = expr.op

        # Короткое замыкание для && и ||
        if op == '&&':
            lbl_false = self._lbl('and_f')
            lbl_end   = self._lbl('and_e')
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
            return 'bool'

        if op == '||':
            lbl_true = self._lbl('or_t')
            lbl_end  = self._lbl('or_e')
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
            return 'bool'

        # Вычислить левую часть → D0, сохранить, вычислить правую → D0
        # Потом применить операцию.
        # Для экономии регистров: left → D1, right → D0
        t_left = self.compile_expr(expr.left)

        # Для str сравнение не поддерживается
        if t_left == 'str':
            raise CompileError(f"Операция {op!r} не поддерживается для строк")

        # Сохранить левый результат в D2 (не трогается правой стороной в простых случаях)
        # Используем стек для безопасности при вложенных выражениях
        self._emit(_ei(Op.MOVE, D(0), Pre(SP)))    # push D0

        t_right = self.compile_expr(expr.right)    # right → D0

        self._emit(_ei(Op.MOVE, Post(SP), D(1)))   # pop → D1  (left)
        # теперь: D1 = left, D0 = right

        arith = {'+': Op.ADD, '-': Op.SUB, '*': Op.MUL, '/': Op.DIV, '%': None}
        cmp_ops = {'==', '!=', '<', '<=', '>', '>='}

        if op in arith and op != '%':
            if op == '/':
                # D1 / D0  → D1
                self._emit(_ei(Op.DIV, D(0), D(1)))
                self._emit(_ei(Op.MOVE, D(1), D(0)))
            else:
                # D1 op D0 → D1  (for sub: D1 - D0)
                self._emit(_ei(arith[op], D(0), D(1)))
                self._emit(_ei(Op.MOVE, D(1), D(0)))
            return 'int'

        if op == '%':
            # a % b = a - (a/b)*b
            self._emit(_ei(Op.MOVE, D(1), D(2)))   # D2 = a
            self._emit(_ei(Op.MOVE, D(0), D(3)))   # D3 = b
            self._emit(_ei(Op.DIV,  D(3), D(2)))   # D2 = a/b
            self._emit(_ei(Op.MUL,  D(3), D(2)))   # D2 = (a/b)*b
            self._emit(_ei(Op.MOVE, D(1), D(0)))   # D0 = a
            self._emit(_ei(Op.SUB,  D(2), D(0)))   # D0 = a - (a/b)*b
            return 'int'

        if op in cmp_ops:
            # CMP: dst - src; нам нужно D1 - D0
            self._emit(_ei(Op.CMP, D(0), D(1)))    # flags for D1 - D0
            lbl_t = self._lbl('cmp_t')
            lbl_e = self._lbl('cmp_e')
            branch_map = {
                '==': Op.BEQ, '!=': Op.BNE,
                '<':  Op.BLT, '<=': Op.BLE,
                '>':  Op.BGT, '>=': Op.BGE,
            }
            self._branch(branch_map[op], lbl_t)
            self._emit(_ei(Op.MOVE, Imm(0), D(0)))
            self._jmp(lbl_e)
            self._label(lbl_t)
            self._emit(_ei(Op.MOVE, Imm(1), D(0)))
            self._label(lbl_e)
            return 'bool'

        raise CompileError(f"Неизвестный бинарный оператор: {op!r}")

    def _compile_call(self, call: Call):
        name = call.name

        # ── Встроенные функции ────────────────────────────────────────────
        if name == 'read_char':
            self._jsr('__rt_read_char')
            return 'int'

        if name == 'write_char':
            if len(call.args) != 1:
                raise CompileError(f"write_char требует 1 аргумент")
            self.compile_expr(call.args[0])
            self._jsr('__rt_write_char')
            return 'void'

        if name == 'str_len':
            self.compile_expr(call.args[0])   # A0 = строка
            self._jsr('__rt_str_len')
            return 'int'

        if name == 'str_get':
            # str_get(s, i) → D0
            # A0=s, D0=i
            self.compile_expr(call.args[0])              # A0 = s
            self._emit(_ei(Op.MOVE, A(0), Pre(SP)))      # push A0
            self.compile_expr(call.args[1])              # D0 = i
            self._emit(_ei(Op.MOVEA, Post(SP), A(0)))    # pop A0
            self._jsr('__rt_str_get')
            return 'int'

        if name == 'str_set':
            # str_set(s, i, c) → void
            # A0=s, D0=i, D1=c
            self.compile_expr(call.args[0])              # A0 = s
            self._emit(_ei(Op.MOVE, A(0), Pre(SP)))      # push A0
            self.compile_expr(call.args[1])              # D0 = i
            self._emit(_ei(Op.MOVE, D(0), Pre(SP)))      # push D0
            self.compile_expr(call.args[2])              # D0 = c
            self._emit(_ei(Op.MOVE, D(0), D(1)))         # D1 = c
            self._emit(_ei(Op.MOVE, Post(SP), D(0)))     # pop D0 = i
            self._emit(_ei(Op.MOVEA, Post(SP), A(0)))    # pop A0 = s
            self._jsr('__rt_str_set')
            return 'void'

        if name == 'str_set_len':
            self.compile_expr(call.args[0])              # A0 = s
            self._emit(_ei(Op.MOVE, A(0), Pre(SP)))      # push A0
            self.compile_expr(call.args[1])              # D0 = n
            self._emit(_ei(Op.MOVEA, Post(SP), A(0)))    # pop A0
            self._jsr('__rt_str_set_len')
            return 'void'

        # ── Пользовательские функции ──────────────────────────────────────
        if name not in self.fns:
            raise CompileError(f"Неизвестная функция: {name!r} (строка {call.line})")

        fn = self.fns[name]
        if len(call.args) != len(fn.params):
            raise CompileError(
                f"Функция {name!r}: ожидается {len(fn.params)} аргументов, "
                f"передано {len(call.args)} (строка {call.line})")

        # Вычислить аргументы и разложить по регистрам.
        # Стратегия: вычислить все на стек, потом загрузить в регистры.
        # Это безопасно для вложенных вызовов.
        for arg in call.args:
            t = self.compile_expr(arg)
            if t == 'str':
                self._emit(_ei(Op.MOVE, A(0), Pre(SP)))
            else:
                self._emit(_ei(Op.MOVE, D(0), Pre(SP)))

        # Загрузить со стека в регистры (в обратном порядке)
        d_idx = sum(1 for _, ptyp in fn.params if ptyp != 'str') - 1
        a_idx = sum(1 for _, ptyp in fn.params if ptyp == 'str') - 1
        for _, ptyp in reversed(fn.params):
            if ptyp == 'str':
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
        out = StringIO()
        offset = 0
        code = bytes(self.code)
        # обратная карта меток
        lbl_by_addr = {}
        for name, addr in self.labels.items():
            lbl_by_addr.setdefault(addr, []).append(name)

        while offset < len(code):
            if offset in lbl_by_addr:
                for n in lbl_by_addr[offset]:
                    out.write(f"; {n}:\n")
            mnem, size = _decode(code, offset)
            hx = code[offset:offset+size].hex().upper()
            out.write(f"{offset} - {hx} - {mnem}\n")
            offset += size
        return out.getvalue()


# ── Встроенный дизассемблер ───────────────────────────────────────────────────

def _decode(data, offset):
    if offset + 4 > len(data):
        return "???", 4
    w = struct.unpack_from('>I', data, offset)[0]
    op = (w >> 24) & 0xFF
    sm = (w >> 20) & 0xF
    dm = (w >> 16) & 0xF
    sr = (w >> 12) & 0xF
    dr = (w >>  8) & 0xF
    sz = '.b' if (w >> 7) & 1 else '.l'
    cur = offset + 4

    def rd():
        nonlocal cur
        v = struct.unpack_from('>I', data, cur)[0]
        cur += 4; return v

    def rds():
        nonlocal cur
        v = struct.unpack_from('>i', data, cur)[0]
        cur += 4; return v

    def op_str(mode, reg):
        rn = _rn(reg)
        if mode == AM.IMMED:    return f"#{rd()}"
        if mode == AM.MEM_DISP:
            d = rds(); return f"{d}({rn})"
        if mode == AM.MEM_IDX:
            d = rds(); xr = rd(); return f"{d}({rn},{_rn(xr)})"
        if mode == AM.NONE:     return ""
        return {AM.REG_D:rn, AM.REG_A:rn,
                AM.MEM_IND:f"({rn})", AM.MEM_POST:f"({rn})+",
                AM.MEM_PRE:f"-({rn})"}[mode]

    branch_ops = {
        Op.JMP:'jmp', Op.JSR:'jsr', Op.BEQ:'beq', Op.BNE:'bne',
        Op.BLT:'blt', Op.BGT:'bgt', Op.BLE:'ble', Op.BGE:'bge',
        Op.BMI:'bmi', Op.BPL:'bpl', Op.BCC:'bcc', Op.BCS:'bcs',
        Op.BVC:'bvc', Op.BVS:'bvs', Op.BRA:'bra',
    }
    if op in branch_ops:
        tgt = rd()
        return f"{branch_ops[op]} @{tgt}", cur - offset

    op_names = {Op.MOVE:'move', Op.MOVEA:'movea', Op.ADD:'add', Op.SUB:'sub',
                Op.MUL:'mul',  Op.DIV:'div',    Op.CMP:'cmp',   Op.AND:'and',
                Op.OR:'or',    Op.XOR:'xor',    Op.NOT:'not',   Op.ASL:'asl',
                Op.ASR:'asr', Op.LSL:'lsl',     Op.LSR:'lsr',   Op.LINK:'link',
                Op.UNLK:'unlk', Op.RTS:'rts',   Op.HALT:'halt'}
    if op == Op.HALT: return "halt", 4
    if op == Op.RTS:  return "rts",  4
    if op == Op.UNLK:
        s = op_str(sm, sr); return f"unlk {s}", cur - offset

    oname = op_names.get(op, f"op{op:02X}")
    s = op_str(sm, sr)
    d = op_str(dm, dr)
    if d: return f"{oname}{sz} {s}, {d}", cur - offset
    return  f"{oname}{sz} {s}", cur - offset


# ═══════════════════════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ═══════════════════════════════════════════════════════════════════════════════

def compile_file(src_path: str, out_dir: str):
    stem = os.path.splitext(os.path.basename(src_path))[0]
    os.makedirs(out_dir, exist_ok=True)

    with open(src_path, encoding='utf-8') as f:
        src = f.read()

    # Лексический анализ
    try:
        tokens = lex(src)
    except LexError as e:
        print(f"Ошибка лексера: {e}", file=sys.stderr); sys.exit(1)

    # Синтаксический анализ
    try:
        parser = Parser(tokens)
        ast = parser.parse_program()
    except ParseError as e:
        print(f"Ошибка парсера: {e}", file=sys.stderr); sys.exit(1)

    # Вывод AST
    ast_path = os.path.join(out_dir, stem + '.ast')
    with open(ast_path, 'w', encoding='utf-8') as f:
        f.write(ast.pretty())
        f.write('\n')
    print(f"AST:    {ast_path}")

    # Кодогенерация
    try:
        cg = CodeGen()
        cg.compile_program(ast)
    except CompileError as e:
        print(f"Ошибка компиляции: {e}", file=sys.stderr); sys.exit(1)

    code = bytes(cg.code)
    data = bytes(cg.data)

    # Выходные файлы
    imem_path   = os.path.join(out_dir, stem + '.imem')
    dmem_path   = os.path.join(out_dir, stem + '.dmem')
    labels_path = os.path.join(out_dir, stem + '.labels')
    lst_path    = os.path.join(out_dir, stem + '.lst')

    with open(imem_path, 'wb') as f: f.write(code)
    with open(dmem_path, 'wb') as f: f.write(data)

    with open(labels_path, 'w') as f:
        for name, addr in sorted(cg.labels.items(), key=lambda x: x[1]):
            f.write(f"{addr:6d}  {name}\n")

    with open(lst_path, 'w') as f:
        f.write(cg.listing())

    print(f"imem:   {imem_path}  ({len(code)} байт)")
    print(f"dmem:   {dmem_path}  ({len(data)} байт)")
    print(f"labels: {labels_path}")
    print(f"lst:    {lst_path}")

    if 'main' in cg.labels:
        print(f"\nТочка входа main: {cg.labels['main']}")
    else:
        print("\n[!] Функция main не найдена", file=sys.stderr)


def main():
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print(f"Использование: {sys.argv[0]} <source.jl> [<output_dir>]")
        print(f"  output_dir по умолчанию: {DEFAULT_OUT_DIR!r}")
        sys.exit(1)
    src_path = sys.argv[1]
    out_dir  = sys.argv[2] if len(sys.argv) == 3 else DEFAULT_OUT_DIR
    compile_file(src_path, out_dir)


if __name__ == '__main__':
    main()