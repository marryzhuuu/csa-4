#!/usr/bin/env python3
"""
emulator.py — потактовый эмулятор M68k-inspired Harvard ISA для языка JavaLight (JL).

Архитектура модели:
  DataPath    — тракт данных (пассивный): imem, dmem, регистровый файл, ALU, IO-порты.
                Предоставляет примитивы чтения/записи, не содержит логики управления.
  ControlUnit — блок управления (активный): декодирует ISA-инструкции, управляет
                DataPath через микропрограммы. Каждая ISA-инструкция реализована как
                последовательность микрокоманд, хранящихся в отдельной μROM.
                Моделирование выполняется с точностью до такта: каждый такт —
                одна микрокоманда.

Загружает бинарные файлы, сгенерированные compiler.py, и исполняет программу.
"""

import argparse
import logging
import os
import struct
import sys
from dataclasses import dataclass
from typing import Callable, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "asm"))
from config import DEFAULT_OUT_DIR, DEFAULT_TRACE_LIMIT, IO_IN, IO_OUT
from isa import AM, Op

# ═══════════════════════════════════════════════════════════════════════════════
#  Микрокоманды — уровень микропрограмм
# ═══════════════════════════════════════════════════════════════════════════════
#
# Каждая микрокоманда описывает одно атомарное действие, выполняемое за 1 такт.
# ControlUnit последовательно исполняет список микрокоманд (μROM[opcode]).
#
# Иерархия микрокоманд:
#
#   MicroOp (базовый)
#   ├── μFetch          — прочитать следующее слово из imem → IR (instruction register)
#   ├── μReadSrc        — прочитать операнд источника → src_val (с учётом режима адресации)
#   ├── μReadDst        — прочитать операнд приёмника → dst_val
#   ├── μALU            — выполнить ALU-операцию над src_val и dst_val → alu_result
#   ├── μWriteDst       — записать alu_result в приёмник (регистр или dmem)
#   ├── μSetFlags       — обновить флаги SR по результату ALU
#   ├── μBranch         — условный/безусловный переход (изменить PC если условие)
#   ├── μPush           — положить значение на стек
#   ├── μPop            — снять значение со стека
#   ├── μLinkFrame      — создать стековый фрейм (LINK)
#   ├── μUnlkFrame      — уничтожить стековый фрейм (UNLK)
#   └── μHalt           — остановить процессор


class MicroOp:
    """Базовый класс микрокоманды."""

    def execute(self, cu: "ControlUnit") -> None:
        raise NotImplementedError


@dataclass
class μFetch(MicroOp):
    """
    Прочитать слово из imem[PC] → cu.IR; PC += 4.
    Используется для: базового слова инструкции и каждого extra-слова.
    target: куда сохранить прочитанное ('IR', 'src_extra', 'dst_extra', 'tgt_addr')
    """

    target: str = "IR"

    def execute(self, cu):
        val = cu.dp.imem_read(cu.dp.PC)
        cu.dp.PC = (cu.dp.PC + 4) & 0xFFFFFFFF
        setattr(cu, self.target, val)


@dataclass
class μDecodeSrc(MicroOp):
    """
    Декодировать поля srcM/srcR из IR.
    Вычислить EA источника и прочитать значение → cu.src_val.
    Для IMMED — читает extra-слово из imem (ещё один такт fetch).
    Для MEM_DISP — читает смещение из imem, затем dmem.
    """

    def execute(self, cu):
        cu.src_val = cu.dp.ea_read(cu.src_mode, cu.src_reg, cu.size_byte)


@dataclass
class μDecodeDst(MicroOp):
    """
    Прочитать значение приёмника → cu.dst_val (для RMW-операций).
    Для регистровых режимов читает напрямую из регистрового файла.
    """

    def execute(self, cu):
        dm, dr = cu.dst_mode, cu.dst_reg
        if dm == AM.REG_D:
            cu.dst_val = cu.dp.D[dr]
        elif dm == AM.REG_A:
            cu.dst_val = cu.dp.A[dr - 8]
        else:
            # Для MEM_* приёмника в ADD/SUB — читаем через EA (PC уже за src)
            cu.dst_val = cu.dp.ea_read(dm, dr, cu.size_byte)


@dataclass
class μALU(MicroOp):
    """
    Выполнить ALU-операцию: cu.alu_result = op(cu.dst_val, cu.src_val).
    op_fn — лямбда (dst, src) → result.
    update_flags — если True, после вычисления вызвать μSetFlags.
    carry_fn — опциональная функция для вычисления флага C.
    overflow_fn — опциональная функция для флага V.
    """

    op_fn: Callable
    update_flags: bool = True
    carry_fn: Optional[Callable] = None
    overflow_fn: Optional[Callable] = None

    def execute(self, cu):
        cu.alu_result = cu.dp.u32(self.op_fn(cu.dst_val, cu.src_val))
        if self.update_flags:
            cu.dp.set_nz(cu.alu_result, cu.size_byte)
            if self.carry_fn:
                cu.dp.SR["C"] = self.carry_fn(cu.dst_val, cu.src_val, cu.alu_result)
            if self.overflow_fn:
                cu.dp.SR["V"] = self.overflow_fn(cu.dst_val, cu.src_val, cu.alu_result)


@dataclass
class μALU_CMP(MicroOp):
    """
    CMP: вычислить dst - src, установить все флаги, результат не сохранять.
    """

    def execute(self, cu):
        dp = cu.dp
        dv = dp.s32(cu.dst_val)
        sv = dp.s32(cu.src_val)
        res = dv - sv
        dp.SR["Z"] = 1 if res == 0 else 0
        dp.SR["N"] = 1 if res < 0 else 0
        dp.SR["V"] = 1 if ((dv ^ sv) < 0 and (dv ^ res) < 0) else 0
        m = 0xFF if cu.size_byte else 0xFFFFFFFF
        dp.SR["C"] = 1 if (dp.u32(dv) & m) < (dp.u32(sv) & m) else 0
        cu.alu_result = None  # не записывать в приёмник


@dataclass
class μWriteDst(MicroOp):
    """
    Записать cu.alu_result в приёмник (регистр или dmem).
    Пропускается если alu_result is None (например после CMP).
    """

    def execute(self, cu):
        if cu.alu_result is not None:
            cu.dp.ea_write(cu.dst_mode, cu.dst_reg, cu.alu_result, cu.size_byte)


@dataclass
class μWriteA(MicroOp):
    """
    MOVEA: записать cu.src_val в адресный регистр dst_reg (без изменения флагов).
    """

    def execute(self, cu):
        cu.dp.A[cu.dst_reg - 8] = cu.dp.u32(cu.src_val)


@dataclass
class μSetFlags(MicroOp):
    """Обновить N, Z по cu.alu_result. Используется отдельно если не встроено в μALU."""

    def execute(self, cu):
        cu.dp.set_nz(cu.alu_result, cu.size_byte)


@dataclass
class μBranch(MicroOp):
    """
    Условный/безусловный переход.
    tgt_attr — имя атрибута cu, содержащего адрес перехода (обычно 'tgt_addr').
    cond_fn — лямбда(SR) → bool; если None — безусловный.
    push_pc — если True, перед переходом сохранить текущий PC на стек (JSR).
    """

    tgt_attr: str = "tgt_addr"
    cond_fn: Optional[Callable] = None
    push_pc: bool = False

    def execute(self, cu):
        taken = (self.cond_fn is None) or self.cond_fn(cu.dp.SR)
        if self.push_pc:
            cu.dp.push(cu.dp.PC)
        if taken:
            cu.dp.PC = getattr(cu, self.tgt_attr)
        cu.branch_taken = taken


@dataclass
class μPush(MicroOp):
    """
    Положить значение src_attr (атрибут cu или dp) на стек.
    src_attr: 'src_val' | 'alu_result' | имя регистра A0..A7
    """

    src_attr: str = "src_val"

    def execute(self, cu):
        if self.src_attr.startswith("A"):
            idx = int(self.src_attr[1:])
            val = cu.dp.A[idx]
        elif self.src_attr.startswith("D"):
            idx = int(self.src_attr[1:])
            val = cu.dp.D[idx]
        else:
            val = getattr(cu, self.src_attr)
        cu.dp.push(val)


@dataclass
class μPop(MicroOp):
    """
    Снять значение со стека → dst_attr (атрибут cu или dp).
    dst_attr: 'A0'..'A7' | 'D0'..'D7' | 'PC' | имя атрибута cu
    """

    dst_attr: str = "alu_result"

    def execute(self, cu):
        val = cu.dp.pop()
        if self.dst_attr == "PC":
            cu.dp.PC = val
        elif self.dst_attr.startswith("A"):
            cu.dp.A[int(self.dst_attr[1:])] = val
        elif self.dst_attr.startswith("D"):
            cu.dp.D[int(self.dst_attr[1:])] = val
        else:
            setattr(cu, self.dst_attr, val)


@dataclass
class μLinkFrame(MicroOp):
    """
    LINK An, #d:
      SP -= 4; mem[SP] = An;   (сохранить старый FP)
      An = SP;                  (новый FP = текущий SP)
      SP += d                   (d отрицательное — зарезервировать локальные)
    dst_reg содержит номер An (8-15).
    src_val содержит смещение d (знаковое).
    """

    def execute(self, cu):
        an = cu.dst_reg - 8
        d = cu.dp.s32(cu.src_val)
        cu.dp.push(cu.dp.A[an])
        cu.dp.A[an] = cu.dp.A[7]
        cu.dp.A[7] = cu.dp.u32(cu.dp.A[7] + d)


@dataclass
class μUnlkFrame(MicroOp):
    """
    UNLK An:
      SP = An;
      An = mem[SP]; SP += 4
    src_reg содержит номер An (0-7, без смещения 8).
    """

    def execute(self, cu):
        an = cu.src_reg - 8  # src_reg in ISA is 8-15 for A-registers
        cu.dp.A[7] = cu.dp.A[an]
        cu.dp.A[an] = cu.dp.pop()


@dataclass
class μHalt(MicroOp):
    """Установить флаг остановки процессора."""

    def execute(self, cu):
        cu.dp.halted = True


@dataclass
class μNop(MicroOp):
    """Пустая микрокоманда (для единообразия длин микропрограмм, если нужно)."""

    def execute(self, cu):
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  μROM — память микропрограмм
# ═══════════════════════════════════════════════════════════════════════════════
#
# Словарь: opcode (int) → список микрокоманд (list[MicroOp]).
#
# Число тактов ISA-инструкции = len(μROM[opcode]).
# Формула из ISA: T = (кол-во слов в инструкции) + 1.
# Каждый μFetch соответствует одному слову; финальная микрокоманда — исполнению.
#
# Пример: MOVE.l #42, -8(A6)  →  3 слова → 4 такта
#   Такт 1: μFetch(IR)          — прочитать базовое слово
#   Такт 2: μDecodeSrc          — читает src extra (imm=42), PC+=4
#   Такт 3: μALU + μWriteDst    — читает dst extra (disp=-8), записывает
#   Такт 4: [исполнение]        — уже входит в μDecodeSrc/μALU шагах выше
#
# На практике fetch базового слова — это шаг ControlUnit.tick() ПЕРЕД
# запуском микропрограммы, поэтому μROM содержит микрокоманды начиная
# с обработки extra-слов и завершая записью результата.


@dataclass
class μNotOp(MicroOp):
    """
    NOT: инвертировать D[src_reg], записать обратно туда же.
    src_reg (0-7) — регистр источника и приёмника одновременно.
    """

    def execute(self, cu):
        reg = cu.src_reg  # для NOT src_reg 0-7 (REG_D)
        cu.dp.D[reg] = cu.dp.u32(~cu.dp.D[reg])
        cu.dp.set_nz(cu.dp.D[reg], cu.size_byte)


def _make_urom() -> dict[int, list[MicroOp]]:
    """Построить таблицу микропрограмм для всех ISA-опкодов."""

    # Вспомогательные лямбды для условий ветвлений
    def _cond(op):
        return {
            Op.JMP: lambda sr: True,
            Op.JSR: lambda sr: True,
            Op.BRA: lambda sr: True,
            Op.BEQ: lambda sr: sr["Z"] == 1,
            Op.BNE: lambda sr: sr["Z"] == 0,
            Op.BLT: lambda sr: sr["N"] != sr["V"],
            Op.BGT: lambda sr: sr["Z"] == 0 and sr["N"] == sr["V"],
            Op.BLE: lambda sr: sr["Z"] == 1 or sr["N"] != sr["V"],
            Op.BGE: lambda sr: sr["N"] == sr["V"],
            Op.BMI: lambda sr: sr["N"] == 1,
            Op.BPL: lambda sr: sr["N"] == 0,
            Op.BCC: lambda sr: sr["C"] == 0,
            Op.BCS: lambda sr: sr["C"] == 1,
            Op.BVC: lambda sr: sr["V"] == 0,
            Op.BVS: lambda sr: sr["V"] == 1,
        }[op]

    rom: dict[int, list[MicroOp]] = {}

    # ── HALT ──────────────────────────────────────────────────────────────────
    # Такт 1: fetch базового слова (в tick)
    # Такт 2: halt
    rom[Op.HALT] = [μHalt()]

    # ── RTS ───────────────────────────────────────────────────────────────────
    # Такт 1: fetch; Такт 2: pop PC
    rom[Op.RTS] = [μPop("PC")]

    # ── LINK An, #d ───────────────────────────────────────────────────────────
    # Такт 1: fetch base; Такт 2: fetch extra (d); Такт 3: link
    rom[Op.LINK] = [
        μFetch("src_val"),  # читает смещение d из imem
        μLinkFrame(),
    ]

    # ── UNLK An ───────────────────────────────────────────────────────────────
    # Такт 1: fetch; Такт 2: unlk
    rom[Op.UNLK] = [μUnlkFrame()]

    # ── Ветвления и переходы ─────────────────────────────────────────────────
    # JMP/JSR/B??: Такт 1: fetch; Такт 2: fetch tgt; Такт 3: branch
    for bop in (
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
    ):
        push = bop == Op.JSR
        rom[bop] = [
            μFetch("tgt_addr"),
            μBranch("tgt_addr", _cond(bop), push_pc=push),
        ]

    # ── MOVE src, dst ─────────────────────────────────────────────────────────
    # Такт 1: fetch; Такты 2..N: μDecodeSrc (читает extra src при IMMED/MEM_DISP);
    # Такт N+1: μALU (identity) + μWriteDst (читает extra dst при MEM_DISP)
    # Реализация: μDecodeSrc внутри dp.ea_read() автоматически вызывает imem_read
    # для extra-слов, каждый такой вызов инкрементирует cu.cycle в dp.imem_read.
    rom[Op.MOVE] = [
        μDecodeSrc(),
        μALU(op_fn=lambda d, s: s, update_flags=True),
        μWriteDst(),
    ]

    # ── MOVEA src, An ────────────────────────────────────────────────────────
    rom[Op.MOVEA] = [
        μDecodeSrc(),
        μWriteA(),
    ]

    # ── Арифметика: ADD, SUB, MUL, DIV ───────────────────────────────────────
    rom[Op.ADD] = [
        μDecodeSrc(),
        μDecodeDst(),
        μALU(
            op_fn=lambda d, s: d + s,
            carry_fn=lambda d, s, r: 1 if (r & 0xFFFFFFFF) < (d & 0xFFFFFFFF) else 0,
        ),
        μWriteDst(),
    ]
    rom[Op.SUB] = [
        μDecodeSrc(),
        μDecodeDst(),
        μALU(
            op_fn=lambda d, s: d - s,
            carry_fn=lambda d, s, r: 1 if (s & 0xFFFFFFFF) > (d & 0xFFFFFFFF) else 0,
        ),
        μWriteDst(),
    ]
    rom[Op.MUL] = [
        μDecodeSrc(),
        μDecodeDst(),
        μALU(op_fn=lambda d, s: d * s),
        μWriteDst(),
    ]
    rom[Op.DIV] = [
        μDecodeSrc(),
        μDecodeDst(),
        μALU(op_fn=lambda d, s: int(d / s) if s != 0 else (_ for _ in ()).throw(RuntimeError("Деление на ноль"))),
        μWriteDst(),
    ]

    # ── CMP src, dst ──────────────────────────────────────────────────────────
    rom[Op.CMP] = [
        μDecodeSrc(),
        μDecodeDst(),
        μALU_CMP(),
        # μWriteDst пропускается т.к. alu_result = None после CMP
        μWriteDst(),
    ]

    # ── Логика: AND, OR, XOR ──────────────────────────────────────────────────
    rom[Op.AND] = [μDecodeSrc(), μDecodeDst(), μALU(op_fn=lambda d, s: d & s), μWriteDst()]
    rom[Op.OR] = [μDecodeSrc(), μDecodeDst(), μALU(op_fn=lambda d, s: d | s), μWriteDst()]
    rom[Op.XOR] = [μDecodeSrc(), μDecodeDst(), μALU(op_fn=lambda d, s: d ^ s), μWriteDst()]

    # ── NOT ───────────────────────────────────────────────────────────────────
    # NOT: операнд и результат — src_reg (особенность ISA)
    rom[Op.NOT] = [
        μNotOp(),
    ]

    # ── Сдвиги ────────────────────────────────────────────────────────────────
    rom[Op.ASL] = [μDecodeSrc(), μDecodeDst(), μALU(op_fn=lambda d, s: d << s), μWriteDst()]
    rom[Op.ASR] = [
        μDecodeSrc(),
        μDecodeDst(),
        μALU(op_fn=lambda d, s: (d if d < 0x80000000 else d - 0x100000000) >> s),
        μWriteDst(),
    ]
    rom[Op.LSL] = [μDecodeSrc(), μDecodeDst(), μALU(op_fn=lambda d, s: d << s), μWriteDst()]
    rom[Op.LSR] = [μDecodeSrc(), μDecodeDst(), μALU(op_fn=lambda d, s: d >> s), μWriteDst()]

    return rom


UROM: dict[int, list[MicroOp]] = _make_urom()


# ═══════════════════════════════════════════════════════════════════════════════
#  DataPath — тракт данных (пассивный)
# ═══════════════════════════════════════════════════════════════════════════════


class DataPath:
    """
    Тракт данных процессора. Пассивный компонент: хранит состояние и
    предоставляет примитивы чтения/записи. Не содержит логики управления —
    все операции инициируются ControlUnit через микрокоманды.

    Компоненты:
      imem      — память команд (read-only при исполнении)
      dmem      — память данных (read-write, включает IO-порты)
      D[0..7]   — регистры данных
      A[0..7]   — адресные регистры (A6=FP, A7=SP)
      PC        — программный счётчик
      SR        — регистр флагов {N, Z, V, C}
      halted    — флаг остановки
      IO-буферы — input_tokens, output_tokens, stdin_source
    """

    def __init__(
        self,
        code: bytes,
        data: bytes,
        input_tokens: list = None,
        stdin_source=None,
    ):
        self.imem = bytearray(code)
        self.dmem = bytearray(max(len(data), 0x20000))  # минимум 128 KB
        self.dmem[: len(data)] = data

        # Регистровый файл
        self.D = [0] * 8
        self.A = [0] * 8
        self.A[7] = 0x0001E000  # начальный SP
        self.PC = 0
        self.SR = {"N": 0, "Z": 0, "V": 0, "C": 0}

        # Флаг остановки
        self.halted = False

        # IO
        self.input_tokens = list(input_tokens or [])
        self.stdin_source = stdin_source
        self.output_tokens: list[int] = []

    # ── Утилиты ──────────────────────────────────────────────────────────────

    def u32(self, v: int) -> int:
        return int(v) & 0xFFFFFFFF

    def s32(self, v: int) -> int:
        v = int(v) & 0xFFFFFFFF
        return v if v < 0x80000000 else v - 0x100000000

    def set_nz(self, result: int, sz: bool = False) -> None:
        """Установить флаги N и Z по результату. Сбросить V и C."""
        mask = 0xFF if sz else 0xFFFFFFFF
        sign = 0x80 if sz else 0x80000000
        r = result & mask
        self.SR["Z"] = 1 if r == 0 else 0
        self.SR["N"] = 1 if (r & sign) else 0
        self.SR["V"] = 0
        self.SR["C"] = 0

    # ── Память команд (imem) ─────────────────────────────────────────────────

    def imem_read(self, addr: int) -> int:
        """Прочитать 32-битное слово из imem по адресу addr."""
        if addr + 4 > len(self.imem):
            raise RuntimeError(f"PC={addr:#010x} за пределами imem")
        return struct.unpack_from(">I", self.imem, addr)[0]

    # ── Память данных (dmem) ─────────────────────────────────────────────────

    def dmem_read(self, addr: int) -> int:
        """Прочитать 32-битное слово из dmem. Обрабатывает IO-порты."""
        addr = self.u32(addr)
        if addr == IO_IN:
            if self.input_tokens:
                return self.input_tokens.pop(0)
            if self.stdin_source is not None:
                ch = self.stdin_source.read(1)
                if ch:
                    return ord(ch)
            return 0xFFFFFFFF  # EOF
        if addr == IO_OUT:
            return 0
        if addr + 4 <= len(self.dmem):
            return struct.unpack_from(">I", self.dmem, addr)[0]
        return 0

    def dmem_write(self, addr: int, val: int) -> None:
        """Записать 32-битное слово в dmem. Обрабатывает IO-порты."""
        addr, val = self.u32(addr), self.u32(val)
        if addr == IO_OUT:
            self.output_tokens.append(val)
            return
        if addr + 4 <= len(self.dmem):
            struct.pack_into(">I", self.dmem, addr, val)

    # ── Стек ─────────────────────────────────────────────────────────────────

    def push(self, v: int) -> None:
        self.A[7] = self.u32(self.A[7] - 4)
        self.dmem_write(self.A[7], v)

    def pop(self) -> int:
        v = self.dmem_read(self.A[7])
        self.A[7] = self.u32(self.A[7] + 4)
        return v

    # ── Эффективные адреса (EA) ──────────────────────────────────────────────
    #
    # Методы ea_read / ea_write реализуют все режимы адресации ISA.
    # Для режимов IMMED и MEM_DISP они читают extra-слова из imem через
    # imem_read(PC) и инкрементируют PC — это моделирует fetch extra-слов.
    # Каждый такой вызов imem_read является отдельным тактом с точки зрения
    # ISA (но в модели DataPath это прозрачно — счётчик тактов ведёт CU).

    def _fetch_extra(self) -> int:
        """Прочитать extra-слово из imem, продвинуть PC."""
        v = self.imem_read(self.PC)
        self.PC += 4
        return v

    def ea_read(self, mode, reg: int, sz: bool = False) -> int:
        """Прочитать операнд согласно режиму адресации."""
        step = 1 if sz else 4
        if mode == AM.REG_D:
            return self.D[reg]
        if mode == AM.REG_A:
            return self.A[reg - 8]
        if mode == AM.IMMED:
            return self._fetch_extra()
        if mode == AM.MEM_IND:
            return self.dmem_read(self.A[reg - 8])
        if mode == AM.MEM_POST:
            v = self.dmem_read(self.A[reg - 8])
            self.A[reg - 8] = self.u32(self.A[reg - 8] + step)
            return v
        if mode == AM.MEM_PRE:
            self.A[reg - 8] = self.u32(self.A[reg - 8] - step)
            return self.dmem_read(self.A[reg - 8])
        if mode == AM.MEM_DISP:
            d = self.s32(self._fetch_extra())
            return self.dmem_read(self.A[reg - 8] + d)
        if mode == AM.MEM_IDX:
            d = self.s32(self._fetch_extra())
            xr = self._fetch_extra()
            xv = self.D[xr] if xr < 8 else self.A[xr - 8]
            return self.dmem_read(self.A[reg - 8] + d + xv)
        raise RuntimeError(f"Неизвестный режим EA (read): {mode}")

    def ea_write(self, mode, reg: int, val: int, sz: bool = False) -> None:
        """Записать результат согласно режиму адресации."""
        step = 1 if sz else 4
        val = self.u32(val)
        if mode == AM.REG_D:
            self.D[reg] = (self.D[reg] & 0xFFFFFF00 | val & 0xFF) if sz else val
        elif mode == AM.REG_A:
            self.A[reg - 8] = val
        elif mode == AM.MEM_IND:
            self.dmem_write(self.A[reg - 8], val)
        elif mode == AM.MEM_POST:
            self.dmem_write(self.A[reg - 8], val)
            self.A[reg - 8] = self.u32(self.A[reg - 8] + step)
        elif mode == AM.MEM_PRE:
            self.A[reg - 8] = self.u32(self.A[reg - 8] - step)
            self.dmem_write(self.A[reg - 8], val)
        elif mode == AM.MEM_DISP:
            d = self.s32(self._fetch_extra())
            self.dmem_write(self.A[reg - 8] + d, val)
        elif mode == AM.MEM_IDX:
            d = self.s32(self._fetch_extra())
            xr = self._fetch_extra()
            xv = self.D[xr] if xr < 8 else self.A[xr - 8]
            self.dmem_write(self.A[reg - 8] + d + xv, val)
        else:
            raise RuntimeError(f"Неизвестный режим EA (write): {mode}")

    def ea_read_dst(self, mode, reg: int, sz: bool = False) -> int:
        """
        Прочитать значение приёмника для RMW-операций.
        Для регистровых режимов — без изменения PC.
        """
        if mode == AM.REG_D:
            return self.D[reg]
        if mode == AM.REG_A:
            return self.A[reg - 8]
        # Для памяти — вычислить EA (без сайд-эффектов пост/пред)
        if mode == AM.MEM_IND:
            return self.dmem_read(self.A[reg - 8])
        if mode == AM.MEM_DISP:
            # Смещение уже было прочитано при μDecodeSrc; здесь используем
            # сохранённый EA. В текущей реализации для простоты читаем снова.
            d = self.s32(self._fetch_extra())
            return self.dmem_read(self.A[reg - 8] + d)
        return 0

    # ── Состояние ────────────────────────────────────────────────────────────

    def dump_state(self) -> str:
        lines = ["=== Состояние DataPath ==="]
        for i in range(8):
            lines.append(f"  D{i}={self.D[i]:08X} ({self.s32(self.D[i]):12d})   " f"A{i}={self.A[i]:08X}")
        sr = " ".join(f"{k}={v}" for k, v in self.SR.items())
        lines.append(f"  PC={self.PC:08X}  SR=[{sr}]")
        return "\n".join(lines)

    def output_str(self) -> str:
        return "".join(chr(t) if 32 <= t <= 126 or t in (9, 10, 13) else f"[0x{t:02x}]" for t in self.output_tokens)


# ═══════════════════════════════════════════════════════════════════════════════
#  ControlUnit — блок управления (микропрограммный)
# ═══════════════════════════════════════════════════════════════════════════════


class ControlUnit:
    """
    Микропрограммный блок управления процессора.

    Принцип работы:
      1. tick() читает базовое слово инструкции из imem[PC] (такт fetch).
      2. Декодирует поля opcode, srcM, dstM, srcR, dstR, sz из базового слова.
      3. Находит микропрограмму в UROM[opcode].
      4. Последовательно исполняет микрокоманды — каждая занимает 1 такт.
      5. Записывает строку трассировки (если включена).

    Регистры ControlUnit (промежуточные значения между микрокомандами):
      IR           — instruction register (базовое слово)
      src_mode, dst_mode, src_reg, dst_reg, size_byte — декодированные поля
      src_val      — операнд источника (после μDecodeSrc)
      dst_val      — операнд приёмника (после μDecodeDst)
      alu_result   — результат ALU (после μALU)
      tgt_addr     — адрес перехода (после fetch extra в ветвлениях)
      branch_taken — был ли взят переход (для трассировки)
    """

    def __init__(
        self,
        dp: DataPath,
        trace: bool = False,
        max_cycles: int = 500_000,
    ):
        self.dp = dp
        self.trace = trace
        self.max_cycles = max_cycles
        self.cycle = 0
        self.trace_log: list[str] = []

        # Промежуточные регистры декодирования/исполнения
        self.IR: int = 0
        self.src_mode = AM.NONE
        self.dst_mode = AM.NONE
        self.src_reg: int = 0
        self.dst_reg: int = 0
        self.size_byte: bool = False

        self.src_val: int = 0
        self.dst_val: int = 0
        self.alu_result: Optional[int] = None
        self.tgt_addr: int = 0
        self.branch_taken: bool = False

    # ── Один машинный такт ───────────────────────────────────────────────────

    def tick(self) -> bool:
        """
        Исполнить одну ISA-инструкцию (несколько тактов-микрокоманд).
        Возвращает False если процессор остановлен.
        """
        if self.dp.halted:
            return False

        pc0 = self.dp.PC

        # ── Такт 1: Fetch базового слова ──────────────────────────────────────
        self.IR = self.dp.imem_read(self.dp.PC)
        self.dp.PC += 4
        self.cycle += 1

        # ── Декодирование полей базового слова ────────────────────────────────
        opcode = (self.IR >> 24) & 0xFF
        self.src_mode = AM((self.IR >> 20) & 0xF)
        self.dst_mode = AM((self.IR >> 16) & 0xF)
        self.src_reg = (self.IR >> 12) & 0xF
        self.dst_reg = (self.IR >> 8) & 0xF
        self.size_byte = bool((self.IR >> 7) & 1)

        # Сбросить промежуточные регистры
        self.src_val = 0
        self.dst_val = 0
        self.alu_result = None
        self.tgt_addr = 0
        self.branch_taken = False

        # ── Выборка микропрограммы ────────────────────────────────────────────
        uops = UROM.get(opcode)
        if uops is None:
            raise RuntimeError(f"Неизвестный опкод {opcode:#04x} @ {pc0}")

        # ── Исполнение микрокоманд (каждая = 1 такт) ─────────────────────────
        for uop in uops:
            uop.execute(self)
            self.cycle += 1

        # ── Трассировка ───────────────────────────────────────────────────────
        if self.trace:
            mnem = self._mnem(opcode, pc0)
            fl = "".join(f"{k}{v}" for k, v in self.dp.SR.items())
            r = (
                f"D0={self.dp.D[0]:08X} D1={self.dp.D[1]:08X} "
                f"D2={self.dp.D[2]:08X} D3={self.dp.D[3]:08X} "
                f"A6={self.dp.A[6]:08X} A7={self.dp.A[7]:08X} [{fl}]"
            )
            self.trace_log.append(f"{pc0:5d} | cy={self.cycle:7d} | {mnem:<36s}| {r}")

        return not self.dp.halted

    def _mnem(self, opcode: int, pc0: int) -> str:
        """Сформировать мнемоническую строку для трассировки."""
        branch_names = {
            Op.JMP: "jmp",
            Op.JSR: "jsr",
            Op.BRA: "bra",
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
        }
        op_names = {
            Op.MOVE: "move.l",
            Op.MOVEA: "movea.l",
            Op.ADD: "add.l",
            Op.SUB: "sub.l",
            Op.MUL: "mul.l",
            Op.DIV: "div.l",
            Op.CMP: "cmp.l",
            Op.AND: "and.l",
            Op.OR: "or.l",
            Op.XOR: "xor.l",
            Op.NOT: "not.l",
            Op.ASL: "asl.l",
            Op.ASR: "asr.l",
            Op.LSL: "lsl.l",
            Op.LSR: "lsr.l",
            Op.HALT: "halt",
            Op.RTS: "rts",
            Op.LINK: f"link.l #{self.dp.s32(self.src_val)}, A{self.dst_reg-8}",
            Op.UNLK: f"unlk A{self.src_reg}",
        }
        if opcode in branch_names:
            t = "T" if self.branch_taken else "F"
            return f"{branch_names[opcode]} @{self.tgt_addr} {t}"
        return op_names.get(opcode, f"op{opcode:02X}")

    # ── Запуск ───────────────────────────────────────────────────────────────

    def run(self, start_pc: int = 0) -> None:
        """Запустить выполнение с адреса start_pc до halt или лимита тактов."""
        self.dp.PC = start_pc
        while not self.dp.halted and self.cycle < self.max_cycles:
            self.tick()
        if self.cycle >= self.max_cycles and not self.dp.halted:
            print(
                f"[EMU] Достигнут лимит тактов ({self.max_cycles}). " f"Используйте --max-cycles для увеличения.",
                file=sys.stderr,
            )

    def dump_state(self) -> str:
        lines = ["=== Состояние процессора ==="]
        lines.append(self.dp.dump_state())
        lines.append(f"  Тактов: {self.cycle}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  CPU — фасад для обратной совместимости (объединяет DataPath + ControlUnit)
# ═══════════════════════════════════════════════════════════════════════════════


class CPU:
    """
    Фасад для обратной совместимости с существующими тестами и CLI.
    Делегирует всё к DataPath и ControlUnit.
    """

    def __init__(
        self,
        code: bytes,
        data: bytes,
        input_tokens: list = None,
        trace: bool = False,
        max_cycles: int = 500_000,
        stdin_source=None,
    ):
        self.dp = DataPath(code, data, input_tokens, stdin_source)
        self.cu = ControlUnit(self.dp, trace=trace, max_cycles=max_cycles)

    # Проксирование атрибутов для совместимости
    @property
    def cycle(self):
        return self.cu.cycle

    @property
    def halted(self):
        return self.dp.halted

    @property
    def trace_log(self):
        return self.cu.trace_log

    @property
    def output_tokens(self):
        return self.dp.output_tokens

    @property
    def D(self):
        return self.dp.D

    @property
    def A(self):
        return self.dp.A

    @property
    def SR(self):
        return self.dp.SR

    @property
    def PC(self):
        return self.dp.PC

    def run(self, start_pc: int = 0) -> None:
        self.cu.run(start_pc)

    def dump_state(self) -> str:
        return self.cu.dump_state()

    def output_str(self) -> str:
        return self.dp.output_str()


# ═══════════════════════════════════════════════════════════════════════════════
#  Вспомогательные функции для CLI (без изменений)
# ═══════════════════════════════════════════════════════════════════════════════


def load_labels(path: str) -> dict[str, int]:
    labels: dict[str, int] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                parts = line.split()
                if len(parts) >= 2:
                    labels[parts[1]] = int(parts[0])
    return labels


def locate_files(prog_name: str, search_dirs: list[str]) -> tuple[str, str]:
    stem = os.path.splitext(os.path.basename(prog_name))[0]
    for d in search_dirs:
        imem = os.path.join(d, stem + ".imem")
        dmem = os.path.join(d, stem + ".dmem")
        if os.path.exists(imem) and os.path.exists(dmem):
            return imem, dmem
    imem = prog_name + ".imem"
    dmem = prog_name + ".dmem"
    if os.path.exists(imem):
        return imem, dmem
    raise FileNotFoundError(f"Не найдены файлы программы '{stem}' ни в одной из директорий: " + ", ".join(search_dirs))


def tokens_from_string(s: str) -> list[int]:
    return [ord(c) for c in s]


def tokens_from_file(path: str) -> list[int]:
    with open(path, encoding="utf-8") as f:
        return [ord(c) for c in f.read()]


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI (без изменений)
# ═══════════════════════════════════════════════════════════════════════════════

DESCRIPTION = """\
emulator.py — потактовый микропрограммный эмулятор M68k-inspired Harvard ISA (JavaLight / JL).

Архитектура модели:
  DataPath    — тракт данных: imem, dmem, регистры, ALU, IO-порты
  ControlUnit — микропрограммный блок управления; каждая ISA-инструкция
                выполняется как последовательность микрокоманд из μROM

Загружает бинарные файлы, созданные compiler.py, и исполняет программу.
Точка входа всегда PC=0: компилятор помещает там JMP main.
"""

EPILOG = """\
Примеры:
  python emulator.py hello_user_name
  python emulator.py hello_user_name --input "Alice"
  python emulator.py sort --input-file data/numbers.txt
  python emulator.py sort --input "5 3 1 4 1 5 " --trace
  python emulator.py hello_user_name --input "Bob" --dump
  python emulator.py sort --trace --trace-out sort.trace

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
    parser.add_argument(
        "prog_name", metavar="<prog_name>", help="Имя программы или путь к файлу (расширение игнорируется)."
    )
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--input", "-i", metavar="STRING", default=None, help="Строка входных данных.")
    input_group.add_argument("--input-file", "-f", metavar="FILE", default=None, help="Файл с входными данными.")
    parser.add_argument(
        "--dir",
        "-d",
        metavar="DIR",
        default=DEFAULT_OUT_DIR,
        help=f"Директория с .imem/.dmem (по умолчанию: {DEFAULT_OUT_DIR!r}).",
    )
    parser.add_argument("--trace", "-t", action="store_true", default=False, help="Включить трассировку тактов.")
    parser.add_argument(
        "--trace-limit",
        metavar="N",
        type=int,
        default=DEFAULT_TRACE_LIMIT,
        help=f"Лимит строк трассировки на экран (по умолчанию: {DEFAULT_TRACE_LIMIT}).",
    )
    parser.add_argument("--trace-out", metavar="FILE", default=None, help="Сохранить полную трассировку в файл.")
    parser.add_argument("--dump", action="store_true", default=False, help="Дамп регистров после завершения.")
    parser.add_argument(
        "--max-cycles", metavar="N", type=int, default=500_000, help="Лимит тактов (по умолчанию: 500000)."
    )
    return parser


def run(target_code: str, target_data: str, input_stream: str) -> None:
    """Программный API для golden-тестов."""
    try:
        code = open(target_code, "rb").read()
        data = open(target_data, "rb").read()
        tokens = tokens_from_file(input_stream)
    except OSError as e:
        print(f"Ошибка чтения файла: {e}", file=sys.stderr)
        sys.exit(1)

    cpu = CPU(code, data, tokens, trace=True)
    cpu.run(start_pc=0)
    print("=== Вывод программы ===")
    print(cpu.output_str(), end="")

    total = len(cpu.trace_log)
    limit = DEFAULT_TRACE_LIMIT
    shown = min(limit, total)
    logging.debug(f"=== Трассировка (первые {shown} из {total} команд) ===")
    for line in cpu.trace_log[:limit]:
        logging.debug(f" {line}")


def main():
    parser = build_parser()
    args = parser.parse_args()

    search_dirs = [args.dir]
    prog_dir = os.path.dirname(args.prog_name)
    if prog_dir and prog_dir not in search_dirs:
        search_dirs.insert(0, prog_dir)

    try:
        imem_path, dmem_path = locate_files(args.prog_name, search_dirs)
    except FileNotFoundError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        code = open(imem_path, "rb").read()
        data = open(dmem_path, "rb").read()
    except OSError as e:
        print(f"Ошибка чтения файла: {e}", file=sys.stderr)
        sys.exit(1)

    stdin_source = None
    if args.input is not None:
        tokens = tokens_from_string(args.input)
        if not tokens or tokens[-1] != ord("\n"):
            tokens.append(ord("\n"))
    elif args.input_file is not None:
        try:
            tokens = tokens_from_file(args.input_file)
        except OSError as e:
            print(f"Ошибка чтения файла ввода: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        tokens = []
        stdin_source = sys.stdin

    stem = os.path.splitext(os.path.basename(imem_path))[0]
    print(f"[EMU] Программа : {stem}")
    print(f"[EMU] imem      : {imem_path}  ({len(code)} байт)")
    print(f"[EMU] dmem      : {dmem_path}  ({len(data)} байт)")
    print("[EMU] Точка входа: PC=0 (JMP main)")
    if stdin_source is None:
        print(f"[EMU] Ввод      : {len(tokens)} токен(ов) (буфер)")
    else:
        print("[EMU] Ввод      : интерактивный stdin")
    if args.trace:
        print(f"[EMU] Трассировка включена (лимит: {args.trace_limit})")
    print()

    cpu = CPU(
        code,
        data,
        tokens,
        trace=args.trace or (args.trace_out is not None),
        max_cycles=args.max_cycles,
        stdin_source=stdin_source,
    )
    cpu.run(start_pc=0)

    print("=== Вывод программы ===")
    print(cpu.output_str(), end="")
    if cpu.output_tokens and cpu.output_tokens[-1] != ord("\n"):
        print()

    if args.dump:
        print()
        print(cpu.dump_state())

    if args.trace and cpu.trace_log:
        print()
        total = len(cpu.trace_log)
        limit = args.trace_limit
        shown = min(limit, total)
        print(f"=== Трассировка (первые {shown} из {total} инструкций) ===")
        print(f"  {'addr':>5} | {'cycle':>7} | {'мнемоника':<36}| регистры")
        print("  " + "-" * 108)
        for line in cpu.trace_log[:limit]:
            print(" ", line)
        if total > limit:
            print(f"  ... ещё {total - limit} инструкций")

    if args.trace_out:
        try:
            with open(args.trace_out, "w", encoding="utf-8") as f:
                f.write(f"# Программа: {stem}\n")
                f.write(f"# Тактов всего: {cpu.cycle}\n")
                f.write(f"# {'addr':>5} | {'cycle':>7} | {'мнемоника':<36}| регистры\n")
                f.write("# " + "-" * 108 + "\n")
                for line in cpu.trace_log:
                    f.write(line + "\n")
            print(f"\n[EMU] Трассировка сохранена: {args.trace_out} ({len(cpu.trace_log)} строк)")
        except OSError as e:
            print(f"Ошибка записи трассировки: {e}", file=sys.stderr)

    print(f"\n[EMU] Завершено за {cpu.cycle} тактов.")


if __name__ == "__main__":
    main()
