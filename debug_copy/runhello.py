#!/usr/bin/env python3
"""
run_hello.py — запуск программы hello_user_name на эмуляторе M68k.

Использование:
    python run_hello.py                      # ввод с клавиатуры
    python run_hello.py --input "Alice"      # ввод из аргумента
    python run_hello.py --input ""           # пустой ввод (stranger)
    python run_hello.py --trace              # с трассировкой тактов
    python run_hello.py --input "Bob" --trace

Необходимые файлы (в той же папке что и скрипт, или укажите путь):
    hello_user_name.bin        — образ памяти команд
    hello_user_name.data.bin   — образ памяти данных
    hello_user_name.labels     — таблица меток (для поиска точки входа main)
"""

import sys
import os
import struct
import argparse
from enum import IntEnum


# ── ISA ──────────────────────────────────────────────────────────────────────

class Op(IntEnum):
    MOVE = 0x01; MOVEA = 0x02
    ADD = 0x10;  SUB = 0x11;  MUL = 0x12;  DIV = 0x13;  CMP = 0x14
    AND = 0x20;  OR  = 0x21;  XOR = 0x22;  NOT = 0x23
    ASL = 0x30;  ASR = 0x31;  LSL = 0x32;  LSR = 0x33
    JMP = 0x40;  JSR = 0x41;  RTS = 0x42
    BEQ = 0x50;  BNE = 0x51;  BLT = 0x52;  BGT = 0x53
    BLE = 0x54;  BGE = 0x55;  BMI = 0x56;  BPL = 0x57
    BCC = 0x58;  BCS = 0x59;  BVC = 0x5A;  BVS = 0x5B;  BRA = 0x5C
    LINK = 0x60; UNLK = 0x61
    HALT = 0xFF

class AM(IntEnum):
    REG_D = 0x0; REG_A = 0x1; IMMED = 0x2
    MEM_IND = 0x3; MEM_POST = 0x4; MEM_PRE = 0x5
    MEM_DISP = 0x6; MEM_IDX = 0x7; NONE = 0xF

IO_IN  = 0xFFFF0000
IO_OUT = 0xFFFF0004


# ── Эмулятор ─────────────────────────────────────────────────────────────────

class CPU:
    def __init__(self, code, data, input_tokens, trace=False, max_cycles=500_000):
        self.imem = bytearray(code)
        self.dmem = bytearray(max(len(data), 0x20000))
        self.dmem[:len(data)] = data

        self.D = [0] * 8
        self.A = [0] * 8
        self.A[7] = 0x0001E000          # стек

        self.PC = 0
        self.SR = {'N': 0, 'Z': 0, 'V': 0, 'C': 0}

        self.input_tokens  = list(input_tokens)
        self.output_tokens = []
        self.trace         = trace
        self.max_cycles    = max_cycles
        self.cycle         = 0
        self.halted        = False
        self.trace_log     = []

    def _u32(self, v): return int(v) & 0xFFFFFFFF
    def _s32(self, v):
        v = int(v) & 0xFFFFFFFF
        return v if v < 0x80000000 else v - 0x100000000

    def _nz(self, r, sz=False):
        m, s = (0xFF, 0x80) if sz else (0xFFFFFFFF, 0x80000000)
        self.SR['Z'] = 1 if (r & m) == 0 else 0
        self.SR['N'] = 1 if (r & s) else 0

    def _fetch(self):
        if self.PC + 4 > len(self.imem):
            raise RuntimeError(f"PC={self.PC:#010x} за пределами imem")
        v = struct.unpack_from('>I', self.imem, self.PC)[0]
        self.PC += 4
        self.cycle += 1
        return v

    def _mr(self, addr):
        addr = self._u32(addr)
        if addr == IO_IN:
            return self.input_tokens.pop(0) if self.input_tokens else 0xFFFFFFFF
        if addr == IO_OUT:
            return 0
        return struct.unpack_from('>I', self.dmem, addr)[0] if addr + 4 <= len(self.dmem) else 0

    def _mw(self, addr, val):
        addr, val = self._u32(addr), self._u32(val)
        if addr == IO_OUT:
            self.output_tokens.append(val)
        elif addr + 4 <= len(self.dmem):
            struct.pack_into('>I', self.dmem, addr, val)

    def _push(self, v):
        self.A[7] = self._u32(self.A[7] - 4)
        self._mw(self.A[7], v)

    def _pop(self):
        v = self._mr(self.A[7])
        self.A[7] = self._u32(self.A[7] + 4)
        return v

    def _read(self, mode, reg, sz=False):
        step = 1 if sz else 4
        if mode == AM.REG_D:   return self.D[reg]
        if mode == AM.REG_A:   return self.A[reg - 8]
        if mode == AM.IMMED:   return self._fetch()
        if mode == AM.MEM_IND: return self._mr(self.A[reg - 8])
        if mode == AM.MEM_POST:
            v = self._mr(self.A[reg - 8])
            self.A[reg - 8] = self._u32(self.A[reg - 8] + step)
            return v
        if mode == AM.MEM_PRE:
            self.A[reg - 8] = self._u32(self.A[reg - 8] - step)
            return self._mr(self.A[reg - 8])
        if mode == AM.MEM_DISP:
            return self._mr(self.A[reg - 8] + self._s32(self._fetch()))
        if mode == AM.MEM_IDX:
            d = self._s32(self._fetch()); xr = self._fetch()
            xv = self.D[xr] if xr < 8 else self.A[xr - 8]
            return self._mr(self.A[reg - 8] + d + xv)
        raise RuntimeError(f"Неизвестный режим чтения {mode}")

    def _write(self, mode, reg, val, sz=False):
        step = 1 if sz else 4
        val = self._u32(val)
        if mode == AM.REG_D:
            self.D[reg] = (self.D[reg] & 0xFFFFFF00 | val & 0xFF) if sz else val
        elif mode == AM.REG_A:   self.A[reg - 8] = val
        elif mode == AM.MEM_IND: self._mw(self.A[reg - 8], val)
        elif mode == AM.MEM_POST:
            self._mw(self.A[reg - 8], val)
            self.A[reg - 8] = self._u32(self.A[reg - 8] + step)
        elif mode == AM.MEM_PRE:
            self.A[reg - 8] = self._u32(self.A[reg - 8] - step)
            self._mw(self.A[reg - 8], val)
        elif mode == AM.MEM_DISP:
            self._mw(self.A[reg - 8] + self._s32(self._fetch()), val)
        elif mode == AM.MEM_IDX:
            d = self._s32(self._fetch()); xr = self._fetch()
            xv = self.D[xr] if xr < 8 else self.A[xr - 8]
            self._mw(self.A[reg - 8] + d + xv, val)
        else:
            raise RuntimeError(f"Неизвестный режим записи {mode}")

    def _addr(self, mode, reg):
        if mode == AM.MEM_IND:  return self._u32(self.A[reg - 8])
        if mode == AM.MEM_DISP: return self._u32(self.A[reg - 8] + self._s32(self._fetch()))
        if mode == AM.MEM_IDX:
            d = self._s32(self._fetch()); xr = self._fetch()
            xv = self.D[xr] if xr < 8 else self.A[xr - 8]
            return self._u32(self.A[reg - 8] + d + xv)
        return None

    def _rmw(self, dm, dr, fn, sz=False):
        if dm == AM.REG_D:
            old = self.D[dr]; new = fn(old); self.D[dr] = self._u32(new)
        elif dm == AM.REG_A:
            old = self.A[dr - 8]; new = fn(old); self.A[dr - 8] = self._u32(new)
        else:
            a = self._addr(dm, dr); old = self._mr(a); new = fn(old); self._mw(a, new)
        return old, new

    def step(self):
        if self.halted:
            return False
        pc0 = self.PC
        w = self._fetch()
        op = (w >> 24) & 0xFF; sm = AM((w >> 20) & 0xF); dm = AM((w >> 16) & 0xF)
        sr = (w >> 12) & 0xF;  dr = (w >>  8) & 0xF;     sz = bool((w >> 7) & 1)
        mn = self._exec(op, sm, dm, sr, dr, sz)
        if self.trace:
            fl = ''.join(f"{k}{v}" for k, v in self.SR.items())
            r = f"D0={self.D[0]:08X} D1={self.D[1]:08X} D2={self.D[2]:08X} A6={self.A[6]:08X} A7={self.A[7]:08X} [{fl}]"
            self.trace_log.append(f"{pc0:5d} | cy={self.cycle:6d} | {mn:<36s}| {r}")
        self.cycle += 1
        return not self.halted

    def _exec(self, op, sm, dm, sr, dr, sz):
        if op == Op.HALT:
            self.halted = True; return "halt"

        if op == Op.RTS:
            self.PC = self._pop(); return "rts"

        if op == Op.LINK:
            d = self._s32(self._fetch()); an = dr - 8
            self._push(self.A[an]); self.A[an] = self.A[7]; self.A[7] = self._u32(self.A[7] + d)
            return f"link A{an}, #{d}"

        if op == Op.UNLK:
            an = sr - 8; self.A[7] = self.A[an]; self.A[an] = self._pop()
            return f"unlk A{an}"

        BRANCHES = (Op.JMP, Op.JSR, Op.BEQ, Op.BNE, Op.BLT, Op.BGT,
                    Op.BLE, Op.BGE, Op.BMI, Op.BPL, Op.BCC, Op.BCS,
                    Op.BVC, Op.BVS, Op.BRA)
        if op in BRANCHES:
            tgt = self._fetch(); f = self.SR
            taken = {Op.JMP:True, Op.JSR:True, Op.BEQ:f['Z']==1, Op.BNE:f['Z']==0,
                     Op.BLT:f['N']!=f['V'], Op.BGT:f['Z']==0 and f['N']==f['V'],
                     Op.BLE:f['Z']==1 or f['N']!=f['V'], Op.BGE:f['N']==f['V'],
                     Op.BMI:f['N']==1, Op.BPL:f['N']==0, Op.BCC:f['C']==0,
                     Op.BCS:f['C']==1, Op.BVC:f['V']==0, Op.BVS:f['V']==1, Op.BRA:True}[op]
            if op == Op.JSR: self._push(self.PC)
            if taken: self.PC = tgt
            names = {Op.JMP:'jmp',Op.JSR:'jsr',Op.BEQ:'beq',Op.BNE:'bne',Op.BLT:'blt',
                     Op.BGT:'bgt',Op.BLE:'ble',Op.BGE:'bge',Op.BMI:'bmi',Op.BPL:'bpl',
                     Op.BCC:'bcc',Op.BCS:'bcs',Op.BVC:'bvc',Op.BVS:'bvs',Op.BRA:'bra'}
            return f"{names[op]} @{tgt} {'T' if taken else 'F'}"

        if op == Op.MOVE:
            v = self._read(sm, sr, sz); self._write(dm, dr, v, sz); self._nz(v, sz); return "move"
        if op == Op.MOVEA:
            self.A[dr - 8] = self._u32(self._read(sm, sr, sz)); return "movea"

        if op == Op.ADD:
            src = self._read(sm, sr, sz)
            old, new = self._rmw(dm, dr, lambda x: x + src, sz)
            self.SR['C'] = 1 if self._u32(new) < self._u32(old) else 0
            self._nz(new, sz); return "add"

        if op == Op.SUB:
            src = self._read(sm, sr, sz)
            old, new = self._rmw(dm, dr, lambda x: x - src, sz)
            self.SR['C'] = 1 if self._u32(src) > self._u32(old) else 0
            self._nz(new, sz); return "sub"

        if op == Op.MUL:
            src = self._s32(self._read(sm, sr, sz))
            res = self._s32(self.D[dr]) * src; self.D[dr] = self._u32(res)
            self._nz(res, sz); return "mul"

        if op == Op.DIV:
            src = self._s32(self._read(sm, sr, sz))
            if src == 0: raise RuntimeError("Деление на ноль")
            res = int(self._s32(self.D[dr]) / src); self.D[dr] = self._u32(res)
            self._nz(res, sz); return "div"

        if op == Op.CMP:
            src = self._s32(self._read(sm, sr, sz))
            dv = self._s32(self.D[dr] if dm == AM.REG_D else
                           self.A[dr-8] if dm == AM.REG_A else
                           self._mr(self._addr(dm, dr)))
            res = dv - src
            self.SR['Z'] = 1 if res == 0 else 0; self.SR['N'] = 1 if res < 0 else 0
            self.SR['V'] = 1 if ((dv ^ src) < 0 and (dv ^ res) < 0) else 0
            m = 0xFF if sz else 0xFFFFFFFF
            self.SR['C'] = 1 if (self._u32(dv) & m) < (self._u32(src) & m) else 0
            return "cmp"

        if op == Op.AND:
            src=self._read(sm,sr,sz); self.D[dr]=self._u32(self.D[dr]&src); self._nz(self.D[dr],sz); return "and"
        if op == Op.OR:
            src=self._read(sm,sr,sz); self.D[dr]=self._u32(self.D[dr]|src); self._nz(self.D[dr],sz); return "or"
        if op == Op.XOR:
            src=self._read(sm,sr,sz); self.D[dr]=self._u32(self.D[dr]^src); self._nz(self.D[dr],sz); return "xor"
        if op == Op.NOT:
            self.D[sr]=self._u32(~self.D[sr]); self._nz(self.D[sr],sz); return "not"
        if op == Op.ASL:
            cnt=self._read(sm,sr,sz); self.D[dr]=self._u32(self.D[dr]<<cnt); self._nz(self.D[dr],sz); return "asl"
        if op == Op.ASR:
            cnt=self._read(sm,sr,sz); self.D[dr]=self._u32(self._s32(self.D[dr])>>cnt); self._nz(self.D[dr],sz); return "asr"
        if op == Op.LSL:
            cnt=self._read(sm,sr,sz); self.D[dr]=self._u32(self.D[dr]<<cnt); self._nz(self.D[dr],sz); return "lsl"
        if op == Op.LSR:
            cnt=self._read(sm,sr,sz); self.D[dr]=self._u32(self.D[dr]>>cnt); self._nz(self.D[dr],sz); return "lsr"

        raise RuntimeError(f"Неизвестный опкод 0x{op:02X} по PC-8")

    def run(self, start_pc=0):
        self.PC = start_pc
        while not self.halted and self.cycle < self.max_cycles:
            self.step()
        if self.cycle >= self.max_cycles and not self.halted:
            print(f"[!] Достигнут лимит тактов ({self.max_cycles})", file=sys.stderr)

    def output_str(self):
        return ''.join(
            chr(t) if 32 <= t <= 126 or t in (9, 10, 13) else f'[0x{t:02x}]'
            for t in self.output_tokens
        )

    def dump(self):
        lines = ["=== Состояние процессора ==="]
        for i in range(8):
            lines.append(f"  D{i}={self.D[i]:08X} ({self._s32(self.D[i]):12d})   "
                         f"A{i}={self.A[i]:08X}")
        fl = ' '.join(f"{k}={v}" for k, v in self.SR.items())
        lines.append(f"  PC={self.PC:08X}  SR=[{fl}]  Тактов={self.cycle}")
        return '\n'.join(lines)


# ── Вспомогательные функции ──────────────────────────────────────────────────

def load_labels(path):
    """Читает файл .labels → dict {имя: адрес}."""
    labels = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                parts = line.split()
                labels[parts[1]] = int(parts[0])
    return labels


def find_file(name, base_dir):
    """Ищет файл рядом со скриптом или в текущей директории."""
    candidates = [
        os.path.join(base_dir, name),
        os.path.join(base_dir, 'programs', name),
        name,
        os.path.join('programs', name),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


# ── Точка входа ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Эмулятор M68k — запуск hello_user_name',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python run_hello.py                        # ввод с клавиатуры
  python run_hello.py --input "Alice"        # передать имя сразу
  python run_hello.py --input ""             # пустой ввод → stranger
  python run_hello.py --input "Bob" --trace  # с трассировкой тактов
  python run_hello.py --input "Bob" --trace --trace-limit 50
        """
    )
    parser.add_argument('--input', '-i', default=None,
                        help='Входная строка (имя пользователя). Без флага — читать с stdin.')
    parser.add_argument('--trace', '-t', action='store_true',
                        help='Печатать трассировку тактов.')
    parser.add_argument('--trace-limit', type=int, default=200, metavar='N',
                        help='Максимум строк трассировки (по умолчанию 200).')
    parser.add_argument('--max-cycles', type=int, default=200_000, metavar='N',
                        help='Лимит тактов (по умолчанию 200000).')
    parser.add_argument('--code',   default=None, help='Путь к .bin  (память команд).')
    parser.add_argument('--data',   default=None, help='Путь к .data.bin (память данных).')
    parser.add_argument('--labels', default=None, help='Путь к .labels.')
    args = parser.parse_args()

    base = os.path.dirname(os.path.abspath(__file__))

    code_path   = args.code   or find_file('hello_user_name.bin',      base)
    data_path   = args.data   or find_file('hello_user_name.data.bin', base)
    labels_path = args.labels or find_file('hello_user_name.labels',   base)

    for path, name in [(code_path, 'hello_user_name.bin'),
                       (data_path, 'hello_user_name.data.bin'),
                       (labels_path, 'hello_user_name.labels')]:
        if path is None:
            print(f"Ошибка: файл '{name}' не найден.", file=sys.stderr)
            print("Укажите путь явно через --code / --data / --labels", file=sys.stderr)
            sys.exit(1)

    code   = open(code_path,   'rb').read()
    data   = open(data_path,   'rb').read()
    labels = load_labels(labels_path)

    if 'main' not in labels:
        print("Ошибка: метка 'main' не найдена в labels-файле.", file=sys.stderr)
        sys.exit(1)

    # Получить входную строку
    if args.input is not None:
        user_input = args.input
    else:
        print("Введите имя (или нажмите Enter для пустого ввода): ", end='', flush=True)
        user_input = sys.stdin.readline().rstrip('\n')

    # Токены: символы строки + завершающий '\n'
    tokens = [ord(c) for c in user_input] + [ord('\n')]

    # Запуск
    cpu = CPU(code, data, tokens, trace=args.trace, max_cycles=args.max_cycles)
    cpu.run(start_pc=labels['main'])

    # Вывод
    print()
    print("=== Вывод программы ===")
    print(cpu.output_str(), end='')

    if args.trace and cpu.trace_log:
        print()
        print(f"\n=== Трассировка (первые {args.trace_limit} из {len(cpu.trace_log)} тактов) ===")
        print(f"  {'addr':>5} | {'cycle':>6} | {'мнемоника':<36}| регистры")
        print("  " + "-" * 110)
        for line in cpu.trace_log[:args.trace_limit]:
            print(" ", line)
        if len(cpu.trace_log) > args.trace_limit:
            print(f"  ... (показано {args.trace_limit} из {len(cpu.trace_log)})")

    print()
    print(cpu.dump())
    print(f"\nПрограмма завершена за {cpu.cycle} тактов.")


if __name__ == '__main__':
    main()