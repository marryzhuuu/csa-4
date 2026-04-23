"""
hello_user_name.asm  —  ассемблерная программа «приветствие»
=============================================================

Регистровые соглашения:
  A7  — стек
  A6  — frame pointer (link/unlk)
  A0  — адрес строки / буфера (общий адресный рег)
  A1  — дополнительный адресный рег
  D0  — возвращаемое значение / временный
  D1  — счётчик / временный
  D2  — временный
  D3  — временный

Memory-mapped IO (в памяти ДАННЫХ):
  0xFFFF0000  — порт ввода  (read_char): читать = получить символ (-1 если EOF)
  0xFFFF0004  — порт вывода (write_char): записать = вывести символ

Гарвардская архитектура:
  Память команд — отдельный образ (program ROM), адреса начинаются с 0.
  Память данных — отдельный образ, адреса начинаются с 0.
  Статические строки и буферы — в памяти данных.

Карта памяти данных:
  0x0000  "What is your name?\\n"  — Pascal-строка (слово длины + слова символов)
  0x0054  "Hello, "               — Pascal-строка
  0x0070  "!\\n"                  — Pascal-строка
  0x0080  "Hello, stranger!\\n"   — Pascal-строка
  0x00C8  name_buf[0..63]         — буфер для имени (64 слова + слово длины = 65 слов)
           (слово длины находится по адресу 0x00C8, символы с 0x00CC)

Функции (метки в памяти КОМАНД):
  0: main
  main вызывает:
    print_str(A0)       — печатает Pascal-строку; не сохраняет регистры
    read_line(A0, D0)   — читает строку в буфер; возвращает длину в D0
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import struct

from isa import (
    AM,
    Op,
    Operand,
    encode_branch,
    encode_halt,
    encode_instr,
    encode_link,
    encode_rts,
    encode_unlk,
    write_debug,
)


# ── helpers ───────────────────────────────────────────────────────────────────
def D(n):
    return Operand(AM.REG_D, reg=n)


def A(n):
    return Operand(AM.REG_A, reg=8 + n)


def Imm(v):
    return Operand(AM.IMMED, imm=v & 0xFFFFFFFF)


def Ind(n):
    return Operand(AM.MEM_IND, reg=8 + n)


def Post(n):
    return Operand(AM.MEM_POST, reg=8 + n)


def Pre(n):
    return Operand(AM.MEM_PRE, reg=8 + n)


def Disp(n, d):
    return Operand(AM.MEM_DISP, reg=8 + n, disp=d)


IO_IN = 0xFFFF0000  # порт ввода
IO_OUT = 0xFFFF0004  # порт вывода


def ei(op, src, dst, sz=False):
    return encode_instr(op, src, dst, size_byte=sz)


# ── Константы данных ──────────────────────────────────────────────────────────
# Каждая Pascal-строка: [int32 length][int32 char0][int32 char1]...
def pascal_str(s):
    words = [len(s)]
    words += [ord(c) for c in s]
    return struct.pack(f">{len(words)}I", *words)


STR_PROMPT = "What is your name?\n"  # @ 0x0000
STR_HELLO = "Hello, "  # @ 0x0054
STR_EXCL = "!\n"  # @ 0x0070  (offset = (19+1)*4=80=0x50? let's compute)
STR_STRANGER = "Hello, stranger!\n"  # @ after


def build_data_segment():
    data = bytearray()

    def align4(b):
        while len(b) % 4:
            b += b"\x00"

    offsets = {}

    def add(name, s):
        offsets[name] = len(data)
        data.extend(pascal_str(s))

    add("prompt", STR_PROMPT)  # "What is your name?\n"
    add("hello", STR_HELLO)  # "Hello, "
    add("excl", STR_EXCL)  # "!\n"
    add("stranger", STR_STRANGER)  # "Hello, stranger!\n"

    # name_buf: 1 слово длины + 64 слова символов = 65*4 = 260 bytes
    offsets["name_buf"] = len(data)
    data.extend(b"\x00" * 65 * 4)

    return bytes(data), offsets


DATA, DATA_OFF = build_data_segment()

# ── Кодогенерация (машинный код) ─────────────────────────────────────────────
# Мы строим инструкции в список, потом вычисляем адреса меток,
# потом фиксируем адреса переходов (backpatching).


class Assembler:
    def __init__(self):
        self.code = bytearray()
        self.labels = {}  # name -> byte offset in code
        self.fixups = []  # (offset_in_code, label_name) — адрес перехода

    def here(self):
        return len(self.code)

    def label(self, name):
        self.labels[name] = self.here()

    def emit(self, b: bytes):
        self.code.extend(b)

    def emit_move(self, src, dst, sz=False):
        self.emit(ei(Op.MOVE, src, dst, sz))

    def emit_movea(self, src, areg_n):
        self.emit(ei(Op.MOVEA, src, A(areg_n)))

    def emit_add(self, src, dst, sz=False):
        self.emit(ei(Op.ADD, src, dst, sz))

    def emit_sub(self, src, dst, sz=False):
        self.emit(ei(Op.SUB, src, dst, sz))

    def emit_mul(self, src, dst, sz=False):
        self.emit(ei(Op.MUL, src, dst, sz))

    def emit_div(self, src, dst, sz=False):
        self.emit(ei(Op.DIV, src, dst, sz))

    def emit_cmp(self, src, dst, sz=False):
        self.emit(ei(Op.CMP, src, dst, sz))

    def emit_branch(self, op, label):
        off = self.here()
        # placeholder target = 0, fixed up later
        b = encode_branch(op, 0)
        self.code.extend(b)
        # target word is at off+4
        self.fixups.append((off + 4, label))

    def emit_jsr(self, label):
        off = self.here()
        b = encode_branch(Op.JSR, 0)
        self.code.extend(b)
        self.fixups.append((off + 4, label))

    def emit_jmp(self, label):
        off = self.here()
        b = encode_branch(Op.JMP, 0)
        self.code.extend(b)
        self.fixups.append((off + 4, label))

    def emit_rts(self):
        self.emit(encode_rts())

    def emit_halt(self):
        self.emit(encode_halt())

    def emit_link(self, areg_n, disp):
        self.emit(encode_link(8 + areg_n, disp))

    def emit_unlk(self, areg_n):
        self.emit(encode_unlk(8 + areg_n))

    def fixup(self):
        for off, name in self.fixups:
            target = self.labels[name]
            struct.pack_into(">I", self.code, off, target)

    def assemble(self):
        self.fixup()
        return bytes(self.code)


# ── Программа hello_user_name ─────────────────────────────────────────────────
#
# На уровне ассемблера реализуем:
#   print_str(A0)       — A0 указывает на Pascal-строку в памяти данных
#   read_line(A0)       — читает в буфер по A0, возвращает длину в D0
#   main()
#
# Встроенные операции IO:
#   Чтение символа:  movea.l #IO_IN, A5 ; move.l (A5), D0   → символ в D0 (-1=EOF)
#   Запись символа:  movea.l #IO_OUT, A5 ; move.l D0, (A5)


def assemble_hello():
    asm = Assembler()

    # ── print_str(A0) ──────────────────────────────────────────────────────
    # A0 → Pascal-строка
    # Использует D1 (counter), D2 (char), A1 (current ptr)
    # Сохраняет A0 через A1 для обхода строки
    asm.label("print_str")
    asm.emit_link(6, -16)  # link A6, -16  (save D1,D2,A1)
    # сохранить регистры
    asm.emit_move(D(1), Disp(6, -4))
    asm.emit_move(D(2), Disp(6, -8))
    asm.emit_movea(A(0), 1)  # A1 = A0 (ptr to str)
    # D1 = length word
    asm.emit_move(Post(1), D(1))  # D1 = mem[A1], A1+=4
    # D2 = 0 (counter i)
    asm.emit_move(Imm(0), D(2))

    asm.label("print_str_loop")
    asm.emit_cmp(D(1), D(2))  # cmp D1, D2  → D2-D1
    asm.emit_branch(Op.BGE, "print_str_done")  # if i >= len: done

    # read char from string
    asm.emit_move(Post(1), D(0))  # D0 = mem[A1]; A1+=4

    # write to IO port
    asm.emit_movea(Imm(IO_OUT), 5)  # A5 = IO_OUT
    asm.emit_move(D(0), Ind(5))  # mem[A5] = D0

    asm.emit_add(Imm(1), D(2))  # i++
    asm.emit_jmp("print_str_loop")

    asm.label("print_str_done")
    asm.emit_move(Disp(6, -4), D(1))  # restore D1
    asm.emit_move(Disp(6, -8), D(2))  # restore D2
    asm.emit_unlk(6)
    asm.emit_rts()

    # ── read_line(A0) → D0=length ─────────────────────────────────────────
    # A0 → буфер (слово длины + символы)
    # Читает до '\n' или EOF, пишет длину в слово[0]
    # Портит: D1(i), D2(char), A1(ptr), A5(IO)
    asm.label("read_line")
    asm.emit_link(6, -16)
    asm.emit_move(D(1), Disp(6, -4))
    asm.emit_move(D(2), Disp(6, -8))
    asm.emit_movea(A(0), 1)  # A1 = A0
    asm.emit_add(Imm(4), A(1))  # A1 += 4 (skip length word, point to chars)
    asm.emit_move(Imm(0), D(1))  # i = 0

    asm.label("read_line_loop")
    asm.emit_cmp(Imm(64), D(1))  # i < 64?
    asm.emit_branch(Op.BGE, "read_line_store")

    # read char from IO
    asm.emit_movea(Imm(IO_IN), 5)
    asm.emit_move(Ind(5), D(2))  # D2 = IO_IN

    # check EOF (-1 = 0xFFFFFFFF)
    asm.emit_cmp(Imm(0xFFFFFFFF), D(2))
    asm.emit_branch(Op.BEQ, "read_line_store")

    # check '\n' (10)
    asm.emit_cmp(Imm(10), D(2))
    asm.emit_branch(Op.BEQ, "read_line_store")

    # store char
    asm.emit_move(D(2), Post(1))  # mem[A1]=D2; A1+=4
    asm.emit_add(Imm(1), D(1))  # i++
    asm.emit_jmp("read_line_loop")

    asm.label("read_line_store")
    # write length word to buf[0]
    asm.emit_move(D(1), Ind(0))  # mem[A0] = i
    asm.emit_move(D(1), D(0))  # return value = i

    asm.emit_move(Disp(6, -4), D(1))
    asm.emit_move(Disp(6, -8), D(2))
    asm.emit_unlk(6)
    asm.emit_rts()

    # ── main ───────────────────────────────────────────────────────────────
    asm.label("main")
    asm.emit_link(6, 0)

    # print_str("What is your name?\n")
    asm.emit_movea(Imm(DATA_OFF["prompt"]), 0)
    asm.emit_jsr("print_str")

    # read_line(name_buf)
    asm.emit_movea(Imm(DATA_OFF["name_buf"]), 0)
    asm.emit_jsr("read_line")

    # if D0 == 0: stranger branch
    asm.emit_cmp(Imm(0), D(0))
    asm.emit_branch(Op.BEQ, "say_stranger")

    # print "Hello, "
    asm.emit_movea(Imm(DATA_OFF["hello"]), 0)
    asm.emit_jsr("print_str")

    # print name_buf
    asm.emit_movea(Imm(DATA_OFF["name_buf"]), 0)
    asm.emit_jsr("print_str")

    # print "!\n"
    asm.emit_movea(Imm(DATA_OFF["excl"]), 0)
    asm.emit_jsr("print_str")
    asm.emit_jmp("main_done")

    asm.label("say_stranger")
    asm.emit_movea(Imm(DATA_OFF["stranger"]), 0)
    asm.emit_jsr("print_str")

    asm.label("main_done")
    asm.emit_unlk(6)
    asm.emit_halt()

    return asm.assemble(), asm.labels


if __name__ == "__main__":
    import os

    out_dir = os.path.join(os.path.dirname(__file__), "..", "debug")
    os.makedirs(out_dir, exist_ok=True)

    code, labels = assemble_hello()

    # binary code image
    with open(os.path.join(out_dir, "hello_user_name.bin"), "wb") as f:
        f.write(code)

    # binary data image
    with open(os.path.join(out_dir, "hello_user_name.data.bin"), "wb") as f:
        f.write(DATA)

    # debug listing
    write_debug(code, os.path.join(out_dir, "hello_user_name.lst"))

    # labels map
    with open(os.path.join(out_dir, "hello_user_name.labels"), "w") as f:
        for name, addr in sorted(labels.items(), key=lambda x: x[1]):
            f.write(f"{addr:6d}  {name}\n")

    print("=== hello_user_name assembled ===")
    print(f"Code size : {len(code)} bytes")
    print(f"Data size : {len(DATA)} bytes")
    print("\nLabels:")
    for name, addr in sorted(labels.items(), key=lambda x: x[1]):
        print(f"  {addr:4d}  {name}")
    print("\nData offsets:")
    for k, v in DATA_OFF.items():
        print(f"  0x{v:04X}  {k}")
