#!/usr/bin/env python3
"""
emulator.py — потактовый эмулятор M68k-inspired Harvard ISA для языка JavaLight (JL).

Загружает бинарные файлы, сгенерированные compiler.py, и исполняет программу.
"""

import argparse
import logging
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "asm"))
from config import DEFAULT_OUT_DIR, DEFAULT_TRACE_LIMIT, IO_IN, IO_OUT
from isa import AM, Op

# ═══════════════════════════════════════════════════════════════════════════════
#  CPU
# ═══════════════════════════════════════════════════════════════════════════════


class CPU:
    """Потактовая модель процессора."""

    def __init__(
        self,
        code: bytes,
        data: bytes,
        input_tokens: list = None,
        trace: bool = False,
        max_cycles: int = 500_000,
        stdin_source=None,
    ):
        """
        code         — байты образа памяти команд (imem)
        data         — байты образа памяти данных (dmem)
        input_tokens — список кодов входных символов (int)
        trace        — записывать ли построчный журнал тактов
        max_cycles   — предельное число тактов до принудительной остановки
        stdin_source — файлоподобный объект для ленивого чтения ввода
                       (используется когда input_tokens исчерпан);
                       если None и input_tokens пуст — возвращает EOF.
        """
        self.imem = bytearray(code)
        self.dmem = bytearray(max(len(data), 0x20000))  # минимум 128 KB
        self.dmem[: len(data)] = data

        self.D = [0] * 8  # регистры данных D0–D7
        self.A = [0] * 8  # адресные регистры A0–A7
        self.A[7] = 0x0001E000  # начальный SP (внутри dmem)

        self.PC = 0
        self.SR = {"N": 0, "Z": 0, "V": 0, "C": 0}

        self.input_tokens = list(input_tokens or [])
        self.stdin_source = stdin_source  # ленивый источник ввода
        self.output_tokens: list[int] = []

        self.trace = trace
        self.max_cycles = max_cycles
        self.cycle = 0
        self.halted = False
        self.trace_log: list[str] = []

    # ── утилиты ──────────────────────────────────────────────────────────────

    def _u32(self, v):
        return int(v) & 0xFFFFFFFF

    def _s32(self, v):
        v = int(v) & 0xFFFFFFFF
        return v if v < 0x80000000 else v - 0x100000000

    def _set_nz(self, result, sz=False):
        mask = 0xFF if sz else 0xFFFFFFFF
        sign = 0x80 if sz else 0x80000000
        r = result & mask
        self.SR["Z"] = 1 if r == 0 else 0
        self.SR["N"] = 1 if (r & sign) else 0

    # ── выборка из imem ───────────────────────────────────────────────────────

    def _fetch(self):
        if self.PC + 4 > len(self.imem):
            raise RuntimeError(f"PC={self.PC:#010x} за пределами imem")
        v = struct.unpack_from(">I", self.imem, self.PC)[0]
        self.PC += 4
        self.cycle += 1
        return v

    # ── доступ к dmem ─────────────────────────────────────────────────────────

    def _mread(self, addr):
        addr = self._u32(addr)
        if addr == IO_IN:
            # Сначала брать из буфера токенов
            if self.input_tokens:
                return self.input_tokens.pop(0)
            # Буфер пуст — попробовать прочитать символ из stdin_source
            if self.stdin_source is not None:
                ch = self.stdin_source.read(1)
                if ch:
                    # Сразу отразить введённый символ в вывод (эхо для вывода программы)
                    return ord(ch)
            return 0xFFFFFFFF  # EOF
        if addr == IO_OUT:
            return 0
        if addr + 4 <= len(self.dmem):
            return struct.unpack_from(">I", self.dmem, addr)[0]
        return 0

    def _mwrite(self, addr, val):
        addr, val = self._u32(addr), self._u32(val)
        if addr == IO_OUT:
            self.output_tokens.append(val)
            return
        if addr + 4 <= len(self.dmem):
            struct.pack_into(">I", self.dmem, addr, val)

    def _push(self, v):
        self.A[7] = self._u32(self.A[7] - 4)
        self._mwrite(self.A[7], v)

    def _pop(self):
        v = self._mread(self.A[7])
        self.A[7] = self._u32(self.A[7] + 4)
        return v

    # ── эффективные адреса ────────────────────────────────────────────────────

    def _ea_read(self, mode, reg, sz=False):
        step = 1 if sz else 4
        if mode == AM.REG_D:
            return self.D[reg]
        if mode == AM.REG_A:
            return self.A[reg - 8]
        if mode == AM.IMMED:
            return self._fetch()
        if mode == AM.MEM_IND:
            return self._mread(self.A[reg - 8])
        if mode == AM.MEM_POST:
            v = self._mread(self.A[reg - 8])
            self.A[reg - 8] = self._u32(self.A[reg - 8] + step)
            return v
        if mode == AM.MEM_PRE:
            self.A[reg - 8] = self._u32(self.A[reg - 8] - step)
            return self._mread(self.A[reg - 8])
        if mode == AM.MEM_DISP:
            return self._mread(self.A[reg - 8] + self._s32(self._fetch()))
        if mode == AM.MEM_IDX:
            d = self._s32(self._fetch())
            xr = self._fetch()
            xv = self.D[xr] if xr < 8 else self.A[xr - 8]
            return self._mread(self.A[reg - 8] + d + xv)
        raise RuntimeError(f"Неизвестный режим EA (read): {mode}")

    def _ea_write(self, mode, reg, val, sz=False):
        step = 1 if sz else 4
        val = self._u32(val)
        if mode == AM.REG_D:
            self.D[reg] = (self.D[reg] & 0xFFFFFF00 | val & 0xFF) if sz else val
        elif mode == AM.REG_A:
            self.A[reg - 8] = val
        elif mode == AM.MEM_IND:
            self._mwrite(self.A[reg - 8], val)
        elif mode == AM.MEM_POST:
            self._mwrite(self.A[reg - 8], val)
            self.A[reg - 8] = self._u32(self.A[reg - 8] + step)
        elif mode == AM.MEM_PRE:
            self.A[reg - 8] = self._u32(self.A[reg - 8] - step)
            self._mwrite(self.A[reg - 8], val)
        elif mode == AM.MEM_DISP:
            self._mwrite(self.A[reg - 8] + self._s32(self._fetch()), val)
        elif mode == AM.MEM_IDX:
            d = self._s32(self._fetch())
            xr = self._fetch()
            xv = self.D[xr] if xr < 8 else self.A[xr - 8]
            self._mwrite(self.A[reg - 8] + d + xv, val)
        else:
            raise RuntimeError(f"Неизвестный режим EA (write): {mode}")

    def _ea_addr(self, mode, reg):
        if mode == AM.MEM_IND:
            return self._u32(self.A[reg - 8])
        if mode == AM.MEM_DISP:
            return self._u32(self.A[reg - 8] + self._s32(self._fetch()))
        if mode == AM.MEM_IDX:
            d = self._s32(self._fetch())
            xr = self._fetch()
            xv = self.D[xr] if xr < 8 else self.A[xr - 8]
            return self._u32(self.A[reg - 8] + d + xv)
        return None

    def _rmw(self, dm, dr, fn, sz=False):
        if dm == AM.REG_D:
            old = self.D[dr]
            new = fn(old)
            self.D[dr] = self._u32(new)
        elif dm == AM.REG_A:
            old = self.A[dr - 8]
            new = fn(old)
            self.A[dr - 8] = self._u32(new)
        else:
            addr = self._ea_addr(dm, dr)
            old = self._mread(addr)
            new = fn(old)
            self._mwrite(addr, new)
        return old, new

    # ── один шаг ─────────────────────────────────────────────────────────────

    def step(self) -> bool:
        """Исполнить одну инструкцию. Возвращает False если процессор остановлен."""
        if self.halted:
            return False
        pc0 = self.PC
        word0 = self._fetch()
        op = (word0 >> 24) & 0xFF
        sm = AM((word0 >> 20) & 0xF)
        dm = AM((word0 >> 16) & 0xF)
        sr = (word0 >> 12) & 0xF
        dr = (word0 >> 8) & 0xF
        sz = bool((word0 >> 7) & 1)
        mnem = self._exec(op, sm, dm, sr, dr, sz)

        if self.trace:
            fl = "".join(f"{k}{v}" for k, v in self.SR.items())
            r = (
                f"D0={self.D[0]:08X} D1={self.D[1]:08X} "
                f"D2={self.D[2]:08X} D3={self.D[3]:08X} "
                f"A6={self.A[6]:08X} A7={self.A[7]:08X} [{fl}]"
            )
            self.trace_log.append(f"{pc0:5d} | cy={self.cycle:7d} | {mnem:<36s}| {r}")
        self.cycle += 1
        return not self.halted

    def _exec(self, op, sm, dm, sr, dr, sz) -> str:
        if op == Op.HALT:
            self.halted = True
            return "halt"

        if op == Op.RTS:
            self.PC = self._pop()
            return "rts"

        if op == Op.LINK:
            d = self._s32(self._fetch())
            an = dr - 8
            self._push(self.A[an])
            self.A[an] = self.A[7]
            self.A[7] = self._u32(self.A[7] + d)
            return f"link A{an}, #{d}"

        if op == Op.UNLK:
            an = sr - 8
            self.A[7] = self.A[an]
            self.A[an] = self._pop()
            return f"unlk A{an}"

        BRANCHES = (
            Op.JMP,
            Op.JSR,
            Op.BEQ,
            Op.BNE,
            Op.BLT,
            Op.BGT,
            Op.BLE,
            Op.BGE,
            Op.BMI,
            Op.BPL,
            Op.BCC,
            Op.BCS,
            Op.BVC,
            Op.BVS,
            Op.BRA,
        )
        if op in BRANCHES:
            tgt = self._fetch()
            f = self.SR
            taken = {
                Op.JMP: True,
                Op.JSR: True,
                Op.BEQ: f["Z"] == 1,
                Op.BNE: f["Z"] == 0,
                Op.BLT: f["N"] != f["V"],
                Op.BGT: f["Z"] == 0 and f["N"] == f["V"],
                Op.BLE: f["Z"] == 1 or f["N"] != f["V"],
                Op.BGE: f["N"] == f["V"],
                Op.BMI: f["N"] == 1,
                Op.BPL: f["N"] == 0,
                Op.BCC: f["C"] == 0,
                Op.BCS: f["C"] == 1,
                Op.BVC: f["V"] == 0,
                Op.BVS: f["V"] == 1,
                Op.BRA: True,
            }[op]
            if op == Op.JSR:
                self._push(self.PC)
            if taken:
                self.PC = tgt
            names = {
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
            return f"{names[op]} @{tgt} {'T' if taken else 'F'}"

        if op == Op.MOVE:
            v = self._ea_read(sm, sr, sz)
            self._ea_write(dm, dr, v, sz)
            self._set_nz(v, sz)
            return f"move.{'b' if sz else 'l'}"

        if op == Op.MOVEA:
            self.A[dr - 8] = self._u32(self._ea_read(sm, sr, sz))
            return f"movea -> A{dr-8}"

        if op == Op.ADD:
            src = self._ea_read(sm, sr, sz)
            old, new = self._rmw(dm, dr, lambda x: x + src, sz)
            self.SR["C"] = 1 if self._u32(new) < self._u32(old) else 0
            self._set_nz(new, sz)
            return "add"

        if op == Op.SUB:
            src = self._ea_read(sm, sr, sz)
            old, new = self._rmw(dm, dr, lambda x: x - src, sz)
            self.SR["C"] = 1 if self._u32(src) > self._u32(old) else 0
            self._set_nz(new, sz)
            return "sub"

        if op == Op.MUL:
            src = self._s32(self._ea_read(sm, sr, sz))
            res = self._s32(self.D[dr]) * src
            self.D[dr] = self._u32(res)
            self._set_nz(res, sz)
            return "mul"

        if op == Op.DIV:
            src = self._s32(self._ea_read(sm, sr, sz))
            if src == 0:
                raise RuntimeError("Деление на ноль")
            res = int(self._s32(self.D[dr]) / src)
            self.D[dr] = self._u32(res)
            self._set_nz(res, sz)
            return "div"

        if op == Op.CMP:
            src = self._s32(self._ea_read(sm, sr, sz))
            if dm == AM.REG_D:
                dv = self._s32(self.D[dr])
            elif dm == AM.REG_A:
                dv = self._s32(self.A[dr - 8])
            else:
                dv = self._s32(self._mread(self._ea_addr(dm, dr)))
            res = dv - src
            self.SR["Z"] = 1 if res == 0 else 0
            self.SR["N"] = 1 if res < 0 else 0
            self.SR["V"] = 1 if ((dv ^ src) < 0 and (dv ^ res) < 0) else 0
            m = 0xFF if sz else 0xFFFFFFFF
            self.SR["C"] = 1 if (self._u32(dv) & m) < (self._u32(src) & m) else 0
            return "cmp"

        if op == Op.AND:
            src = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self.D[dr] & src)
            self._set_nz(self.D[dr], sz)
            return "and"
        if op == Op.OR:
            src = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self.D[dr] | src)
            self._set_nz(self.D[dr], sz)
            return "or"
        if op == Op.XOR:
            src = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self.D[dr] ^ src)
            self._set_nz(self.D[dr], sz)
            return "xor"
        if op == Op.NOT:
            self.D[sr] = self._u32(~self.D[sr])
            self._set_nz(self.D[sr], sz)
            return "not"
        if op == Op.ASL:
            cnt = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self.D[dr] << cnt)
            self._set_nz(self.D[dr], sz)
            return "asl"
        if op == Op.ASR:
            cnt = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self._s32(self.D[dr]) >> cnt)
            self._set_nz(self.D[dr], sz)
            return "asr"
        if op == Op.LSL:
            cnt = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self.D[dr] << cnt)
            self._set_nz(self.D[dr], sz)
            return "lsl"
        if op == Op.LSR:
            cnt = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self.D[dr] >> cnt)
            self._set_nz(self.D[dr], sz)
            return "lsr"

        raise RuntimeError(f"Неизвестный опкод {op:#04x} @ PC-несколько")

    # ── запуск ───────────────────────────────────────────────────────────────

    def run(self, start_pc: int = 0):
        """Запустить выполнение с адреса start_pc до halt или лимита тактов."""
        self.PC = start_pc
        while not self.halted and self.cycle < self.max_cycles:
            self.step()
        if self.cycle >= self.max_cycles and not self.halted:
            print(
                f"[EMU] Достигнут лимит тактов ({self.max_cycles}). " f"Используйте --max-cycles для увеличения.",
                file=sys.stderr,
            )

    # ── состояние ────────────────────────────────────────────────────────────

    def dump_state(self) -> str:
        lines = ["=== Состояние процессора ==="]
        for i in range(8):
            lines.append(f"  D{i}={self.D[i]:08X} ({self._s32(self.D[i]):12d})   " f"A{i}={self.A[i]:08X}")
        sr = " ".join(f"{k}={v}" for k, v in self.SR.items())
        lines.append(f"  PC={self.PC:08X}  SR=[{sr}]  Тактов={self.cycle}")
        return "\n".join(lines)

    def output_str(self) -> str:
        """Вывод программы в виде строки (непечатные → [0xNN])."""
        return "".join(chr(t) if 32 <= t <= 126 or t in (9, 10, 13) else f"[0x{t:02x}]" for t in self.output_tokens)


# ═══════════════════════════════════════════════════════════════════════════════
#  Вспомогательные функции для CLI
# ═══════════════════════════════════════════════════════════════════════════════


def load_labels(path: str) -> dict[str, int]:
    """Читает .labels файл → {имя_метки: адрес}."""
    labels: dict[str, int] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                parts = line.split()
                if len(parts) >= 2:
                    labels[parts[1]] = int(parts[0])
    return labels


def locate_files(prog_name: str, search_dirs: list[str]) -> tuple[str, str, str]:
    """
    Найти .imem, .dmem, .labels для программы prog_name.
    search_dirs — список директорий для поиска (перебираются по очереди).
    Возвращает (imem_path, dmem_path, labels_path).
    """
    stem = os.path.splitext(os.path.basename(prog_name))[0]
    for d in search_dirs:
        imem = os.path.join(d, stem + ".imem")
        dmem = os.path.join(d, stem + ".dmem")
        labels = os.path.join(d, stem + ".labels")
        if os.path.exists(imem) and os.path.exists(dmem) and os.path.exists(labels):
            return imem, dmem, labels
    # Если не нашли по директориям — попробовать как прямой путь/префикс
    imem = prog_name + ".imem"
    dmem = prog_name + ".dmem"
    labels = prog_name + ".labels"
    if os.path.exists(imem):
        return imem, dmem, labels
    raise FileNotFoundError(f"Не найдены файлы программы '{stem}' ни в одной из директорий: " + ", ".join(search_dirs))


def tokens_from_string(s: str) -> list[int]:
    """Преобразовать строку в список кодов символов."""
    return [ord(c) for c in s]


def tokens_from_file(path: str) -> list[int]:
    """Прочитать файл ввода как поток токенов."""
    with open(path, encoding="utf-8") as f:
        return [ord(c) for c in f.read()]


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI — точка входа
# ═══════════════════════════════════════════════════════════════════════════════

DESCRIPTION = """\
emulator.py — потактовый эмулятор M68k-inspired Harvard ISA (JavaLight / JL).

Загружает бинарные файлы, созданные compiler.py, и исполняет программу.
Файлы <prog>.imem, <prog>.dmem, <prog>.labels ищутся в директории --dir
(по умолчанию совпадает с DEFAULT_OUT_DIR из config.py).
"""

EPILOG = """\
Примеры использования:
  # Запустить hello_user_name, ввод с stdin
  python emulator.py hello_user_name

  # Передать ввод строкой
  python emulator.py hello_user_name --input "Alice"

  # Передать ввод из файла
  python emulator.py sort --input-file data/numbers.txt

  # Программа в нестандартной директории
  python emulator.py hello_user_name --dir build/

  # Включить трассировку (показать первые 100 тактов)
  python emulator.py sort --input "5 3 1 4 1 5 " --trace

  # Трассировка с ограничением числа выводимых строк
  python emulator.py sort --input "3 9 2 7 " --trace --trace-limit 50

  # Показать состояние регистров после завершения
  python emulator.py hello_user_name --input "Bob" --dump

  # Сохранить трассировку в файл
  python emulator.py sort --input "4 5 3 1 2 " --trace --trace-out sort.trace

  # Задать лимит тактов (по умолчанию 500000)
  python emulator.py sort --max-cycles 1000000

Формат файлов (создаются compiler.py):
  <prog>.imem     бинарный образ памяти команд
  <prog>.dmem     бинарный образ памяти данных
  <prog>.labels   таблица меток (адрес → имя)

Константы (из config.py):
  IO_IN  = {io_in:#010x}   адрес порта ввода  в dmem
  IO_OUT = {io_out:#010x}  адрес порта вывода в dmem
""".format(io_in=IO_IN, io_out=IO_OUT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emulator.py",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Обязательный аргумент ──────────────────────────────────────────────
    parser.add_argument(
        "prog_name",
        metavar="<prog_name>",
        help=(
            "Имя программы (без расширения) или путь к ней. "
            "Эмулятор ищет <prog_name>.imem / .dmem / .labels "
            "в директории --dir."
        ),
    )

    # ── Ввод ──────────────────────────────────────────────────────────────
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--input",
        "-i",
        metavar="STRING",
        default=None,
        help=(
            "Строка входных данных (передаётся как поток токенов). "
            "Символ \\n добавляется автоматически в конец, "
            "если строка не заканчивается переводом строки. "
            "Без этого ключа и без --input-file ввод читается со stdin."
        ),
    )
    input_group.add_argument(
        "--input-file",
        "-f",
        metavar="FILE",
        default=None,
        help="Файл с входными данными (читается целиком как поток токенов).",
    )

    # ── Директория с бинарниками ──────────────────────────────────────────
    parser.add_argument(
        "--dir",
        "-d",
        metavar="DIR",
        default=DEFAULT_OUT_DIR,
        help=(f"Директория, где искать .imem / .dmem / .labels " f"(по умолчанию: {DEFAULT_OUT_DIR!r} из config.py)."),
    )

    # ── Трассировка ───────────────────────────────────────────────────────
    parser.add_argument(
        "--trace",
        "-t",
        action="store_true",
        default=False,
        help="Включить построчную трассировку тактов.",
    )
    parser.add_argument(
        "--trace-limit",
        metavar="N",
        type=int,
        default=DEFAULT_TRACE_LIMIT,
        help=(
            f"Максимальное число строк трассировки, выводимых на экран (по умолчанию: {DEFAULT_TRACE_LIMIT}). Не влияет на --trace-out."
        ),
    )
    parser.add_argument(
        "--trace-out",
        metavar="FILE",
        default=None,
        help="Сохранить полную трассировку в файл (все такты, без лимита).",
    )

    # ── Дамп состояния ────────────────────────────────────────────────────
    parser.add_argument(
        "--dump",
        action="store_true",
        default=False,
        help="Показать состояние регистров после завершения программы.",
    )

    # ── Лимит тактов ─────────────────────────────────────────────────────
    parser.add_argument(
        "--max-cycles",
        metavar="N",
        type=int,
        default=500_000,
        help="Максимальное число тактов до принудительной остановки (по умолчанию: 500000).",
    )

    return parser


def run(target_code: str, target_data: str, target_labels: str, input_stream: str) -> None:
    try:
        code = open(target_code, "rb").read()
        data = open(target_data, "rb").read()
        labels = load_labels(target_labels)
        tokens = tokens_from_file(input_stream)
        start_pc = labels["main"]

    except OSError as e:
        print(f"Ошибка чтения файла: {e}", file=sys.stderr)
        sys.exit(1)

    cpu = CPU(code, data, tokens, True)
    cpu.run(start_pc=start_pc)
    print("=== Вывод программы ===")
    print(cpu.output_str(), end="")

    # ── Трассировка в логгер ──────────────────────────────────────────────
    # logging.debug("\n")
    total = len(cpu.trace_log)
    limit = DEFAULT_TRACE_LIMIT
    shown = min(limit, total)
    logging.debug(f"=== Трассировка (первые {shown} из {total} команд) ===")
    logging.debug(f"  {'addr':>5} | {'cycle':>7} | {'мнемоника':<36}| регистры")
    logging.debug("  " + "-" * 108)
    for line in cpu.trace_log[:limit]:
        logging.debug(f" {line}")
    if total > limit:
        logging.debug(f"  ... ещё {total - limit} команд (используйте --trace-out для полного журнала)")


def main():
    parser = build_parser()
    args = parser.parse_args()

    # ── Найти файлы программы ─────────────────────────────────────────────
    search_dirs = [args.dir]
    # Если указан путь с разделителем — добавить его директорию
    prog_dir = os.path.dirname(args.prog_name)
    if prog_dir and prog_dir not in search_dirs:
        search_dirs.insert(0, prog_dir)

    try:
        imem_path, dmem_path, labels_path = locate_files(args.prog_name, search_dirs)
    except FileNotFoundError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)

    # ── Загрузить бинарники ───────────────────────────────────────────────
    try:
        code = open(imem_path, "rb").read()
        data = open(dmem_path, "rb").read()
        labels = load_labels(labels_path)
    except OSError as e:
        print(f"Ошибка чтения файла: {e}", file=sys.stderr)
        sys.exit(1)

    # ── Получить точку входа ──────────────────────────────────────────────
    if "main" not in labels:
        print("Ошибка: метка 'main' не найдена в .labels файле.", file=sys.stderr)
        sys.exit(1)
    start_pc = labels["main"]

    # ── Подготовить входные токены ────────────────────────────────────────
    stdin_source = None  # None = ввод только из буфера токенов
    if args.input is not None:
        tokens = tokens_from_string(args.input)
        # Добавить \n если не заканчивается им (удобно для интерактивных программ)
        if not tokens or tokens[-1] != ord("\n"):
            tokens.append(ord("\n"))
    elif args.input_file is not None:
        try:
            tokens = tokens_from_file(args.input_file)
        except OSError as e:
            print(f"Ошибка чтения файла ввода: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # Интерактивный режим: ввод будет читаться лениво в момент
        # обращения программы к IO_IN — символ за символом из stdin.
        tokens = []
        stdin_source = sys.stdin

    # ── Информация о запуске ──────────────────────────────────────────────
    stem = os.path.splitext(os.path.basename(imem_path))[0]
    print(f"[EMU] Программа : {stem}")
    print(f"[EMU] imem      : {imem_path}  ({len(code)} байт)")
    print(f"[EMU] dmem      : {dmem_path}  ({len(data)} байт)")
    print(f"[EMU] Вход main : {start_pc}")
    if stdin_source is None:
        print(f"[EMU] Ввод      : {len(tokens)} токен(ов) (буфер)")
    else:
        print("[EMU] Ввод      : интерактивный stdin")
    if args.trace:
        print(f"[EMU] Трассировка включена (лимит вывода: {args.trace_limit})")
    print()

    # ── Запуск ────────────────────────────────────────────────────────────
    cpu = CPU(
        code,
        data,
        tokens,
        trace=args.trace or (args.trace_out is not None),
        max_cycles=args.max_cycles,
        stdin_source=stdin_source,
    )
    cpu.run(start_pc=start_pc)

    # ── Вывод программы ───────────────────────────────────────────────────
    print("=== Вывод программы ===")
    print(cpu.output_str(), end="")
    if cpu.output_tokens and cpu.output_tokens[-1] != ord("\n"):
        print()  # добавить перевод строки если его нет

    # ── Состояние регистров ───────────────────────────────────────────────
    if args.dump:
        print()
        print(cpu.dump_state())

    # ── Трассировка на экран ──────────────────────────────────────────────
    if args.trace and cpu.trace_log:
        print()
        total = len(cpu.trace_log)
        limit = args.trace_limit
        shown = min(limit, total)
        print(f"=== Трассировка (первые {shown} из {total} команд) ===")
        print(f"  {'addr':>5} | {'cycle':>7} | {'мнемоника':<36}| регистры")
        print("  " + "-" * 108)
        for line in cpu.trace_log[:limit]:
            print(" ", line)
        if total > limit:
            print(f"  ... ещё {total - limit} команд (используйте --trace-out для полного журнала)")

    # ── Трассировка в файл ────────────────────────────────────────────────
    if args.trace_out:
        try:
            with open(args.trace_out, "w", encoding="utf-8") as f:
                f.write(f"# Программа: {stem}\n")
                f.write(f"# imem: {imem_path}\n")
                f.write(f"# Тактов всего: {cpu.cycle}\n")
                f.write(f"# {'addr':>5} | {'cycle':>7} | {'мнемоника':<36}| регистры\n")
                f.write("# " + "-" * 108 + "\n")
                for line in cpu.trace_log:
                    f.write(line + "\n")
            print(f"\n[EMU] Трассировка сохранена: {args.trace_out} ({len(cpu.trace_log)} строк)")
        except OSError as e:
            print(f"Ошибка записи трассировки: {e}", file=sys.stderr)

    # ── Итог ─────────────────────────────────────────────────────────────
    print(f"\n[EMU] Завершено за {cpu.cycle} тактов.")


if __name__ == "__main__":
    main()
