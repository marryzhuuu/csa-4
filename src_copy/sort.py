"""
sort.asm  — ассемблерная программа «сортировка + статистика»
=============================================================

Функции:
  read_int()        → D0          Читает целое число из потока (ASCII)
  write_int(D0)                   Печатает целое со знаком
  write_char(D0)                  Пишет один символ
  bubble_sort(A0,D0)              Сортирует массив в памяти данных
  main()                          Точка входа

Карта памяти данных:
  0x0000  "Sorted: \n"  — Pascal-строка для вывода заголовка
  0x0030  "Sum: \n"
  0x0050  "Avg: \n"
  0x0070  arr[0..63]    — массив int32 (максимум 64 элемента)
          (arr[i] находится по адресу 0x0070 + i*4)

Соглашение вызовов (как в hello_user_name):
  D0 — результат/аргумент
  A0 — адресный аргумент (массив)
  A6 — frame pointer
  A7 — stack pointer
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


def Idx(n, d, xn):
    return Operand(AM.MEM_IDX, reg=8 + n, disp=d, idx_reg=xn)


IO_IN = 0xFFFF0000
IO_OUT = 0xFFFF0004


def ei(op, src, dst, sz=False):
    return encode_instr(op, src, dst, size_byte=sz)


def pascal_str(s):
    words = [len(s)]
    words += [ord(c) for c in s]
    return struct.pack(f">{len(words)}I", *words)


def build_data():
    data = bytearray()
    offsets = {}

    def add(name, s):
        offsets[name] = len(data)
        data.extend(pascal_str(s))

    add("str_sorted", "Sorted: ")
    add("str_sum", "Sum: ")
    add("str_avg", "Avg: ")
    add("str_nl", "\n")
    add("str_sp", " ")
    # arr: 64 элемента * 4 байта = 256 байт
    offsets["arr"] = len(data)
    data.extend(b"\x00" * 64 * 4)
    return bytes(data), offsets


DATA, DATA_OFF = build_data()


class Assembler:
    def __init__(self):
        self.code = bytearray()
        self.labels = {}
        self.fixups = []

    def here(self):
        return len(self.code)

    def label(self, name):
        self.labels[name] = self.here()

    def emit(self, b):
        self.code.extend(b)

    def emit_move(self, src, dst, sz=False):
        self.emit(ei(Op.MOVE, src, dst, sz))

    def emit_movea(self, src, an):
        self.emit(ei(Op.MOVEA, src, A(an)))

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

    def emit_rts(self):
        self.emit(encode_rts())

    def emit_halt(self):
        self.emit(encode_halt())

    def emit_link(self, an, d):
        self.emit(encode_link(8 + an, d))

    def emit_unlk(self, an):
        self.emit(encode_unlk(8 + an))

    def emit_branch(self, op, label):
        off = self.here()
        self.code.extend(encode_branch(op, 0))
        self.fixups.append((off + 4, label))

    def emit_jsr(self, label):
        off = self.here()
        self.code.extend(encode_branch(Op.JSR, 0))
        self.fixups.append((off + 4, label))

    def emit_jmp(self, label):
        off = self.here()
        self.code.extend(encode_branch(Op.JMP, 0))
        self.fixups.append((off + 4, label))

    def fixup(self):
        for off, name in self.fixups:
            struct.pack_into(">I", self.code, off, self.labels[name])

    def assemble(self):
        self.fixup()
        return bytes(self.code)


def assemble_sort():
    asm = Assembler()

    # ── write_char(D0) ────────────────────────────────────────────────────
    # Записывает один символ из D0 в IO_OUT.
    asm.label("write_char")
    asm.emit_movea(Imm(IO_OUT), 5)
    asm.emit_move(D(0), Ind(5))
    asm.emit_rts()

    # ── read_int() → D0 ───────────────────────────────────────────────────
    # Алгоритм:
    #   1. Пропустить пробелы/переводы строк
    #   2. Проверить знак '-'
    #   3. Накапливать цифры: result = result*10 + (c-'0')
    #   4. Применить знак
    # Регистры: D0=result, D1=char, D2=negative_flag
    # Сохраняем через стек (link/unlk)
    asm.label("read_int")
    asm.emit_link(6, -16)
    asm.emit_move(D(1), Disp(6, -4))
    asm.emit_move(D(2), Disp(6, -8))
    asm.emit_move(D(3), Disp(6, -12))

    asm.emit_move(Imm(0), D(0))  # result = 0
    asm.emit_move(Imm(0), D(2))  # negative = false

    # skip whitespace
    asm.label("read_int_skip")
    asm.emit_movea(Imm(IO_IN), 5)
    asm.emit_move(Ind(5), D(1))  # D1 = char
    # cmp ' '
    asm.emit_cmp(Imm(32), D(1))
    asm.emit_branch(Op.BEQ, "read_int_skip")
    # cmp '\n'
    asm.emit_cmp(Imm(10), D(1))
    asm.emit_branch(Op.BEQ, "read_int_skip")
    # cmp '\r'
    asm.emit_cmp(Imm(13), D(1))
    asm.emit_branch(Op.BEQ, "read_int_skip")

    # check '-'
    asm.emit_cmp(Imm(45), D(1))  # '-'
    asm.emit_branch(Op.BNE, "read_int_digits")
    asm.emit_move(Imm(1), D(2))  # negative = true
    asm.emit_movea(Imm(IO_IN), 5)
    asm.emit_move(Ind(5), D(1))  # read next char

    asm.label("read_int_digits")
    # while '0' <= D1 <= '9'
    asm.emit_cmp(Imm(48), D(1))  # D1 - 48
    asm.emit_branch(Op.BLT, "read_int_done")
    asm.emit_cmp(Imm(57), D(1))
    asm.emit_branch(Op.BGT, "read_int_done")

    # result = result * 10 + (D1 - 48)
    asm.emit_mul(Imm(10), D(0))
    asm.emit_sub(Imm(48), D(1))
    asm.emit_add(D(1), D(0))

    asm.emit_movea(Imm(IO_IN), 5)
    asm.emit_move(Ind(5), D(1))
    asm.emit_jmp("read_int_digits")

    asm.label("read_int_done")
    # if negative: D0 = 0 - D0
    asm.emit_cmp(Imm(0), D(2))
    asm.emit_branch(Op.BEQ, "read_int_ret")
    asm.emit_move(Imm(0), D(3))
    asm.emit_sub(D(0), D(3))
    asm.emit_move(D(3), D(0))

    asm.label("read_int_ret")
    asm.emit_move(Disp(6, -4), D(1))
    asm.emit_move(Disp(6, -8), D(2))
    asm.emit_move(Disp(6, -12), D(3))
    asm.emit_unlk(6)
    asm.emit_rts()

    # ── write_int(D0) ─────────────────────────────────────────────────────
    # Рекурсивный вывод цифр.
    # Сохраняем D0 через стек (link).
    asm.label("write_int")
    asm.emit_link(6, -8)
    asm.emit_move(D(0), Disp(6, -4))
    asm.emit_move(D(1), Disp(6, -8))

    # if D0 < 0: print '-'; D0 = -D0
    asm.emit_cmp(Imm(0), D(0))
    asm.emit_branch(Op.BGE, "write_int_pos")
    asm.emit_move(Imm(45), D(0))  # '-'
    asm.emit_jsr("write_char")
    asm.emit_move(Disp(6, -4), D(0))  # restore D0
    asm.emit_move(Imm(0), D(1))
    asm.emit_sub(D(0), D(1))
    asm.emit_move(D(1), D(0))
    asm.emit_move(D(0), Disp(6, -4))  # save updated D0

    asm.label("write_int_pos")
    # if D0 == 0: print '0'; return
    asm.emit_cmp(Imm(0), D(0))
    asm.emit_branch(Op.BNE, "write_int_nonzero")
    asm.emit_move(Imm(48), D(0))
    asm.emit_jsr("write_char")
    asm.emit_jmp("write_int_done")

    asm.label("write_int_nonzero")
    # if D0 >= 10: recurse with D0/10
    asm.emit_cmp(Imm(10), D(0))
    asm.emit_branch(Op.BLT, "write_int_digit")
    asm.emit_move(D(0), D(1))
    asm.emit_div(Imm(10), D(1))
    # save remainder: D0 % 10 = D0 - (D0/10)*10
    asm.emit_move(D(1), D(0))  # D0 = quotient
    asm.emit_jsr("write_int")  # recurse
    asm.emit_move(Disp(6, -4), D(0))  # restore original D0

    # compute D0 % 10
    asm.emit_move(D(0), D(1))
    asm.emit_div(Imm(10), D(1))
    asm.emit_mul(Imm(10), D(1))
    asm.emit_sub(D(1), D(0))  # D0 = D0 - (D0/10)*10

    asm.label("write_int_digit")
    asm.emit_add(Imm(48), D(0))  # '0' + digit
    asm.emit_jsr("write_char")

    asm.label("write_int_done")
    asm.emit_move(Disp(6, -4), D(0))
    asm.emit_move(Disp(6, -8), D(1))
    asm.emit_unlk(6)
    asm.emit_rts()

    # ── print_pascal_str(A0) ──────────────────────────────────────────────
    # Печатает Pascal-строку на которую указывает A0
    asm.label("print_str")
    asm.emit_link(6, -16)
    asm.emit_move(D(1), Disp(6, -4))
    asm.emit_move(D(2), Disp(6, -8))
    asm.emit_movea(A(0), 1)  # A1 = A0
    asm.emit_move(Post(1), D(1))  # D1 = length; A1 += 4
    asm.emit_move(Imm(0), D(2))  # i = 0

    asm.label("print_str_loop")
    asm.emit_cmp(D(1), D(2))
    asm.emit_branch(Op.BGE, "print_str_done")
    asm.emit_move(Post(1), D(0))  # D0 = mem[A1]; A1+=4
    asm.emit_jsr("write_char")
    asm.emit_add(Imm(1), D(2))
    asm.emit_jmp("print_str_loop")

    asm.label("print_str_done")
    asm.emit_move(Disp(6, -4), D(1))
    asm.emit_move(Disp(6, -8), D(2))
    asm.emit_unlk(6)
    asm.emit_rts()

    # ── bubble_sort(A0=arr_base, D0=n) ───────────────────────────────────
    # Фрейм (28 байт):
    #   -4(A6)   saved D1
    #   -8(A6)   saved D2
    #   -12(A6)  saved D3
    #   -16(A6)  saved D4
    #   -20(A6)  saved D5
    #   -24(A6)  n-1          (outer limit, не портится inner-loop)
    #   -28(A6)  inner limit  (n-1-i, пересчитывается каждый outer)
    # D1=i, D2=j, D3=tmp, D4=arr[j], D5=arr[j+1], A1=ptr
    asm.label("bubble_sort")
    asm.emit_link(6, -28)
    asm.emit_move(D(1), Disp(6, -4))
    asm.emit_move(D(2), Disp(6, -8))
    asm.emit_move(D(3), Disp(6, -12))
    asm.emit_move(D(4), Disp(6, -16))
    asm.emit_move(D(5), Disp(6, -20))

    asm.emit_move(D(0), D(3))
    asm.emit_sub(Imm(1), D(3))  # D3 = n-1
    asm.emit_move(D(3), Disp(6, -24))  # сохранить n-1 в фрейм

    asm.emit_move(Imm(0), D(1))  # i = 0

    asm.label("bs_outer")
    asm.emit_cmp(Disp(6, -24), D(1))  # cmp (n-1), i → i - (n-1)
    asm.emit_branch(Op.BGE, "bs_done")  # if i >= n-1: done

    asm.emit_move(Imm(0), D(2))  # j = 0
    # inner_limit = n-1-i → сохранить в фрейм
    asm.emit_move(Disp(6, -24), D(3))  # D3 = n-1
    asm.emit_sub(D(1), D(3))  # D3 = n-1-i
    asm.emit_move(D(3), Disp(6, -28))  # inner_limit в фрейм

    asm.label("bs_inner")
    asm.emit_cmp(Disp(6, -28), D(2))  # cmp inner_limit, j → j - limit
    asm.emit_branch(Op.BGE, "bs_inner_done")

    # A1 = A0 + j*4
    asm.emit_movea(A(0), 1)
    asm.emit_move(D(2), D(4))
    asm.emit_mul(Imm(4), D(4))
    asm.emit_add(D(4), A(1))  # A1 = A0 + j*4
    asm.emit_move(Ind(1), D(4))  # D4 = arr[j]
    asm.emit_move(Disp(1, 4), D(5))  # D5 = arr[j+1]

    # if arr[j] <= arr[j+1]: skip swap
    asm.emit_cmp(D(5), D(4))  # D4 - D5
    asm.emit_branch(Op.BLE, "bs_no_swap")

    # swap arr[j] ↔ arr[j+1]
    asm.emit_move(D(5), Ind(1))  # arr[j]   = D5
    asm.emit_move(D(4), Disp(1, 4))  # arr[j+1] = D4

    asm.label("bs_no_swap")
    asm.emit_add(Imm(1), D(2))  # j++
    asm.emit_jmp("bs_inner")

    asm.label("bs_inner_done")
    asm.emit_add(Imm(1), D(1))  # i++
    asm.emit_jmp("bs_outer")

    asm.label("bs_done")
    asm.emit_move(Disp(6, -4), D(1))
    asm.emit_move(Disp(6, -8), D(2))
    asm.emit_move(Disp(6, -12), D(3))
    asm.emit_move(Disp(6, -16), D(4))
    asm.emit_move(Disp(6, -20), D(5))
    asm.emit_unlk(6)
    asm.emit_rts()

    # ── main ──────────────────────────────────────────────────────────────
    # Фрейм (A6):
    #   -4(A6)  n     — количество элементов
    #   -8(A6)  i     — счётчик цикла
    #   -12(A6) sum   — сумма элементов
    #   -16(A6) avg   — среднее
    # A4 = константный указатель на начало массива (не портится функциями)
    # A0 — временный: передаём строки в print_str
    # D0 — аргумент/результат функций
    asm.label("main")
    asm.emit_link(6, -16)

    # n = read_int()
    asm.emit_jsr("read_int")
    asm.emit_move(D(0), Disp(6, -4))  # n → фрейм

    # A4 = &arr[0]  (постоянный указатель на массив, не трогаем)
    asm.emit_movea(Imm(DATA_OFF["arr"]), 4)

    # ── Чтение n элементов ──────────────────────────────────────────────
    asm.emit_move(Imm(0), Disp(6, -8))  # i = 0

    asm.label("read_loop")
    asm.emit_move(Disp(6, -8), D(0))  # D0 = i
    asm.emit_cmp(Disp(6, -4), D(0))  # cmp n, i
    asm.emit_branch(Op.BGE, "read_done")

    asm.emit_jsr("read_int")  # D0 = прочитанное число

    # arr[i] = D0:  addr = A4 + i*4
    asm.emit_move(Disp(6, -8), D(1))  # D1 = i
    asm.emit_mul(Imm(4), D(1))  # D1 = i*4
    asm.emit_movea(A(4), 1)  # A1 = A4
    asm.emit_add(D(1), A(1))  # A1 = A4 + i*4
    asm.emit_move(D(0), Ind(1))  # mem[A1] = D0

    asm.emit_move(Disp(6, -8), D(0))
    asm.emit_add(Imm(1), D(0))
    asm.emit_move(D(0), Disp(6, -8))  # i++
    asm.emit_jmp("read_loop")

    # ── Сортировка ──────────────────────────────────────────────────────
    asm.label("read_done")
    asm.emit_movea(A(4), 0)  # A0 = A4  (bubble_sort ждёт A0)
    asm.emit_move(Disp(6, -4), D(0))  # D0 = n
    asm.emit_jsr("bubble_sort")
    # A0 мог испортиться внутри bubble_sort — восстановим A4 как эталон
    # (A4 не трогается нигде в функциях)

    # ── Вычисление суммы ────────────────────────────────────────────────
    asm.emit_move(Imm(0), Disp(6, -12))  # sum = 0
    asm.emit_move(Imm(0), Disp(6, -8))  # i = 0

    asm.label("sum_loop")
    asm.emit_move(Disp(6, -8), D(0))
    asm.emit_cmp(Disp(6, -4), D(0))
    asm.emit_branch(Op.BGE, "sum_done")

    # D0 = arr[i]
    asm.emit_move(Disp(6, -8), D(1))
    asm.emit_mul(Imm(4), D(1))
    asm.emit_movea(A(4), 1)
    asm.emit_add(D(1), A(1))
    asm.emit_move(Ind(1), D(0))

    asm.emit_add(D(0), Disp(6, -12))  # sum += arr[i]

    asm.emit_move(Disp(6, -8), D(0))
    asm.emit_add(Imm(1), D(0))
    asm.emit_move(D(0), Disp(6, -8))  # i++
    asm.emit_jmp("sum_loop")

    # ── Вычисление среднего ─────────────────────────────────────────────
    asm.label("sum_done")
    asm.emit_move(Imm(0), Disp(6, -16))  # avg = 0
    asm.emit_cmp(Imm(0), Disp(6, -4))
    asm.emit_branch(Op.BEQ, "avg_done")
    asm.emit_move(Disp(6, -12), D(0))  # D0 = sum
    asm.emit_move(Disp(6, -4), D(1))  # D1 = n
    asm.emit_div(D(1), D(0))  # D0 = sum/n
    asm.emit_move(D(0), Disp(6, -16))  # avg = D0

    # ── Вывод "Sorted: " ────────────────────────────────────────────────
    asm.label("avg_done")
    asm.emit_movea(Imm(DATA_OFF["str_sorted"]), 0)
    asm.emit_jsr("print_str")

    # ── Вывод элементов массива ─────────────────────────────────────────
    asm.emit_move(Imm(0), Disp(6, -8))  # i = 0

    asm.label("print_loop")
    asm.emit_move(Disp(6, -8), D(0))
    asm.emit_cmp(Disp(6, -4), D(0))
    asm.emit_branch(Op.BGE, "print_done")

    # D0 = arr[i]
    asm.emit_move(Disp(6, -8), D(1))
    asm.emit_mul(Imm(4), D(1))
    asm.emit_movea(A(4), 1)
    asm.emit_add(D(1), A(1))
    asm.emit_move(Ind(1), D(0))

    asm.emit_jsr("write_int")  # вывести arr[i]

    # пробел если не последний
    asm.emit_move(Disp(6, -8), D(0))
    asm.emit_add(Imm(1), D(0))  # i+1
    asm.emit_cmp(Disp(6, -4), D(0))  # (i+1) >= n ?
    asm.emit_branch(Op.BGE, "no_space")
    asm.emit_move(Imm(32), D(0))
    asm.emit_jsr("write_char")

    asm.label("no_space")
    asm.emit_move(Disp(6, -8), D(0))
    asm.emit_add(Imm(1), D(0))
    asm.emit_move(D(0), Disp(6, -8))  # i++
    asm.emit_jmp("print_loop")

    # ── Вывод "\nSum: <sum>\nAvg: <avg>\n" ─────────────────────────────
    asm.label("print_done")
    asm.emit_move(Imm(10), D(0))
    asm.emit_jsr("write_char")

    asm.emit_movea(Imm(DATA_OFF["str_sum"]), 0)
    asm.emit_jsr("print_str")
    asm.emit_move(Disp(6, -12), D(0))  # sum
    asm.emit_jsr("write_int")
    asm.emit_move(Imm(10), D(0))
    asm.emit_jsr("write_char")

    asm.emit_movea(Imm(DATA_OFF["str_avg"]), 0)
    asm.emit_jsr("print_str")
    asm.emit_move(Disp(6, -16), D(0))  # avg
    asm.emit_jsr("write_int")
    asm.emit_move(Imm(10), D(0))
    asm.emit_jsr("write_char")

    asm.emit_unlk(6)
    asm.emit_halt()

    return asm.assemble(), asm.labels


if __name__ == "__main__":
    out_dir = os.path.join(os.path.dirname(__file__), "..", "debug")
    os.makedirs(out_dir, exist_ok=True)

    code, labels = assemble_sort()

    with open(os.path.join(out_dir, "sort.bin"), "wb") as f:
        f.write(code)
    with open(os.path.join(out_dir, "sort.data.bin"), "wb") as f:
        f.write(DATA)
    write_debug(code, os.path.join(out_dir, "sort.lst"))
    with open(os.path.join(out_dir, "sort.labels"), "w") as f:
        for name, addr in sorted(labels.items(), key=lambda x: x[1]):
            f.write(f"{addr:6d}  {name}\n")

    print("=== sort assembled ===")
    print(f"Code size : {len(code)} bytes")
    print(f"Data size : {len(DATA)} bytes")
    print("\nLabels:")
    for name, addr in sorted(labels.items(), key=lambda x: x[1]):
        print(f"  {addr:4d}  {name}")
    print("\nData offsets:")
    for k, v in DATA_OFF.items():
        print(f"  0x{v:04X}  {k}")
