"""
emulator.py — потактовый эмулятор M68k-inspired Harvard ISA
"""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "asm"))
from isa import AM, Op

IO_IN  = 0xFFFF0000
IO_OUT = 0xFFFF0004


class CPU:
    def __init__(self, code: bytes, data: bytes,
                 input_tokens: list = None,
                 trace=True, max_cycles=500_000):
        self.imem = bytearray(code)
        self.dmem = bytearray(max(len(data), 0x20000))   # 128KB достаточно
        self.dmem[:len(data)] = data

        self.D = [0] * 8
        self.A = [0] * 8
        self.A[7] = 0x0001E000

        self.PC = 0
        self.SR = {"N": 0, "Z": 0, "V": 0, "C": 0}

        self.input_tokens  = list(input_tokens or [])
        self.output_tokens = []
        self.trace       = trace
        self.max_cycles  = max_cycles
        self.cycle       = 0
        self.halted      = False
        self.trace_log   = []

    def _u32(self, v): return int(v) & 0xFFFFFFFF
    def _s32(self, v):
        v = int(v) & 0xFFFFFFFF
        return v if v < 0x80000000 else v - 0x100000000

    def _set_nz(self, result, sz=False):
        mask = 0xFF if sz else 0xFFFFFFFF
        sign = 0x80 if sz else 0x80000000
        r = result & mask
        self.SR["Z"] = 1 if r == 0 else 0
        self.SR["N"] = 1 if (r & sign) else 0

    def _fetch(self):
        if self.PC + 4 > len(self.imem):
            raise RuntimeError(f"PC out of bounds: {self.PC:#010x}")
        v = struct.unpack_from(">I", self.imem, self.PC)[0]
        self.PC += 4
        self.cycle += 1
        return v

    def _mread(self, addr):
        addr = self._u32(addr)
        if addr == IO_IN:
            return self.input_tokens.pop(0) if self.input_tokens else 0xFFFFFFFF
        if addr == IO_OUT:
            return 0
        if addr + 4 <= len(self.dmem):
            return struct.unpack_from(">I", self.dmem, addr)[0]
        return 0

    def _mwrite(self, addr, val):
        addr = self._u32(addr)
        val  = self._u32(val)
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

    def _ea_read(self, mode, reg, sz=False):
        step = 1 if sz else 4
        if mode == AM.REG_D:  return self.D[reg]
        if mode == AM.REG_A:  return self.A[reg - 8]
        if mode == AM.IMMED:  return self._fetch()
        if mode == AM.MEM_IND:  return self._mread(self.A[reg - 8])
        if mode == AM.MEM_POST:
            v = self._mread(self.A[reg - 8])
            self.A[reg - 8] = self._u32(self.A[reg - 8] + step)
            return v
        if mode == AM.MEM_PRE:
            self.A[reg - 8] = self._u32(self.A[reg - 8] - step)
            return self._mread(self.A[reg - 8])
        if mode == AM.MEM_DISP:
            disp = self._s32(self._fetch())
            return self._mread(self.A[reg - 8] + disp)
        if mode == AM.MEM_IDX:
            disp = self._s32(self._fetch())
            xreg = self._fetch()
            xval = self.D[xreg] if xreg < 8 else self.A[xreg - 8]
            return self._mread(self.A[reg - 8] + disp + xval)
        raise RuntimeError(f"Unknown EA mode {mode} in read")

    def _ea_write(self, mode, reg, val, sz=False):
        step = 1 if sz else 4
        val = self._u32(val)
        if mode == AM.REG_D:
            if sz:
                self.D[reg] = (self.D[reg] & 0xFFFFFF00) | (val & 0xFF)
            else:
                self.D[reg] = val
            return
        if mode == AM.REG_A:
            self.A[reg - 8] = val; return
        if mode == AM.MEM_IND:
            self._mwrite(self.A[reg - 8], val); return
        if mode == AM.MEM_POST:
            self._mwrite(self.A[reg - 8], val)
            self.A[reg - 8] = self._u32(self.A[reg - 8] + step); return
        if mode == AM.MEM_PRE:
            self.A[reg - 8] = self._u32(self.A[reg - 8] - step)
            self._mwrite(self.A[reg - 8], val); return
        if mode == AM.MEM_DISP:
            disp = self._s32(self._fetch())
            self._mwrite(self.A[reg - 8] + disp, val); return
        if mode == AM.MEM_IDX:
            disp = self._s32(self._fetch())
            xreg = self._fetch()
            xval = self.D[xreg] if xreg < 8 else self.A[xreg - 8]
            self._mwrite(self.A[reg - 8] + disp + xval, val); return
        raise RuntimeError(f"Unknown EA mode {mode} in write")

    def _ea_addr(self, mode, reg):
        """Вернуть адрес памяти для dst, потребляя extra words из imem."""
        if mode == AM.MEM_IND:
            return self._u32(self.A[reg - 8])
        if mode == AM.MEM_DISP:
            disp = self._s32(self._fetch())
            return self._u32(self.A[reg - 8] + disp)
        if mode == AM.MEM_IDX:
            disp = self._s32(self._fetch())
            xreg = self._fetch()
            xval = self.D[xreg] if xreg < 8 else self.A[xreg - 8]
            return self._u32(self.A[reg - 8] + disp + xval)
        return None

    def _rmw(self, dm, dr, fn, sz=False):
        """Read-modify-write: применить fn(old) к dst, вернуть (old, new, res_for_flags)."""
        if dm == AM.REG_D:
            old = self.D[dr]; new = fn(old); self.D[dr] = self._u32(new)
        elif dm == AM.REG_A:
            old = self.A[dr - 8]; new = fn(old); self.A[dr - 8] = self._u32(new)
        else:
            addr = self._ea_addr(dm, dr)
            old  = self._mread(addr); new = fn(old); self._mwrite(addr, new)
        return old, new

    def step(self):
        if self.halted:
            return False
        pc0   = self.PC
        word0 = self._fetch()
        op = (word0 >> 24) & 0xFF
        sm = AM((word0 >> 20) & 0xF)
        dm = AM((word0 >> 16) & 0xF)
        sr = (word0 >> 12) & 0xF
        dr = (word0 >>  8) & 0xF
        sz = bool((word0 >> 7) & 1)
        mnem = self._exec(op, sm, dm, sr, dr, sz)

        if self.trace:
            fl = "".join(f"{k}{v}" for k, v in self.SR.items())
            r  = (f"D0={self.D[0]:08X} D1={self.D[1]:08X} D2={self.D[2]:08X} "
                  f"D3={self.D[3]:08X} A6={self.A[6]:08X} A7={self.A[7]:08X} [{fl}]")
            self.trace_log.append(f"{pc0:5d} | cy={self.cycle:7d} | {mnem:<36s}| {r}")
        self.cycle += 1
        return not self.halted

    def _exec(self, op, sm, dm, sr, dr, sz):
        if op == Op.HALT:
            self.halted = True
            return "halt"

        if op == Op.RTS:
            self.PC = self._pop()
            return "rts"

        if op == Op.LINK:
            disp = self._s32(self._fetch())
            an   = dr - 8
            self._push(self.A[an])
            self.A[an] = self.A[7]
            self.A[7]  = self._u32(self.A[7] + disp)
            return f"link A{an}, #{disp}"

        if op == Op.UNLK:
            an = sr - 8
            self.A[7]  = self.A[an]
            self.A[an] = self._pop()
            return f"unlk A{an}"

        BRANCH_OPS = (Op.JMP, Op.JSR, Op.BEQ, Op.BNE, Op.BLT, Op.BGT,
                      Op.BLE, Op.BGE, Op.BMI, Op.BPL, Op.BCC, Op.BCS,
                      Op.BVC, Op.BVS, Op.BRA)
        if op in BRANCH_OPS:
            target = self._fetch()
            f = self.SR
            taken = {
                Op.JMP: True,
                Op.JSR: True,
                Op.BEQ: f["Z"] == 1,
                Op.BNE: f["Z"] == 0,
                Op.BLT: f["N"] != f["V"],
                Op.BGT: f["Z"] == 0 and f["N"] == f["V"],
                Op.BLE: f["Z"] == 1 or  f["N"] != f["V"],
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
                self.PC = target
            names = {Op.JMP:"jmp", Op.JSR:"jsr", Op.BEQ:"beq", Op.BNE:"bne",
                     Op.BLT:"blt", Op.BGT:"bgt", Op.BLE:"ble", Op.BGE:"bge",
                     Op.BMI:"bmi", Op.BPL:"bpl", Op.BCC:"bcc", Op.BCS:"bcs",
                     Op.BVC:"bvc", Op.BVS:"bvs", Op.BRA:"bra"}
            return f"{names[op]} @{target} {'T' if taken else 'F'}"

        if op == Op.MOVE:
            v = self._ea_read(sm, sr, sz)
            self._ea_write(dm, dr, v, sz)
            self._set_nz(v, sz)
            return f"move.{'b' if sz else 'l'}"

        if op == Op.MOVEA:
            v = self._ea_read(sm, sr, sz)
            self.A[dr - 8] = self._u32(v)
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
            dst = self._s32(self.D[dr])
            res = dst * src
            self.D[dr] = self._u32(res)
            self._set_nz(res, sz)
            return "mul"

        if op == Op.DIV:
            src = self._s32(self._ea_read(sm, sr, sz))
            if src == 0:
                raise RuntimeError("Division by zero")
            dst = self._s32(self.D[dr])
            res = int(dst / src)
            self.D[dr] = self._u32(res)
            self._set_nz(res, sz)
            return "div"

        if op == Op.CMP:
            src   = self._s32(self._ea_read(sm, sr, sz))
            if dm == AM.REG_D:
                dst_v = self._s32(self.D[dr])
            elif dm == AM.REG_A:
                dst_v = self._s32(self.A[dr - 8])
            else:
                addr  = self._ea_addr(dm, dr)
                dst_v = self._s32(self._mread(addr))
            res = dst_v - src
            self.SR["Z"] = 1 if res == 0 else 0
            self.SR["N"] = 1 if res < 0  else 0
            self.SR["V"] = 1 if ((dst_v ^ src) < 0 and (dst_v ^ res) < 0) else 0
            mask = 0xFF if sz else 0xFFFFFFFF
            self.SR["C"] = 1 if (self._u32(dst_v) & mask) < (self._u32(src) & mask) else 0
            return "cmp"

        if op == Op.AND:
            src = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self.D[dr] & src)
            self._set_nz(self.D[dr], sz); return "and"
        if op == Op.OR:
            src = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self.D[dr] | src)
            self._set_nz(self.D[dr], sz); return "or"
        if op == Op.XOR:
            src = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self.D[dr] ^ src)
            self._set_nz(self.D[dr], sz); return "xor"
        if op == Op.NOT:
            self.D[sr] = self._u32(~self.D[sr])
            self._set_nz(self.D[sr], sz); return "not"
        if op == Op.ASL:
            cnt = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self.D[dr] << cnt); self._set_nz(self.D[dr], sz); return "asl"
        if op == Op.ASR:
            cnt = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self._s32(self.D[dr]) >> cnt); self._set_nz(self.D[dr], sz); return "asr"
        if op == Op.LSL:
            cnt = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self.D[dr] << cnt); self._set_nz(self.D[dr], sz); return "lsl"
        if op == Op.LSR:
            cnt = self._ea_read(sm, sr, sz)
            self.D[dr] = self._u32(self.D[dr] >> cnt); self._set_nz(self.D[dr], sz); return "lsr"

        raise RuntimeError(f"Unknown opcode {op:#04x}")

    def run(self, start_pc=0):
        self.PC = start_pc
        while not self.halted and self.cycle < self.max_cycles:
            self.step()
        if self.cycle >= self.max_cycles and not self.halted:
            print(f"[EMU] Max cycles ({self.max_cycles}) reached!", file=sys.stderr)

    def dump_state(self):
        lines = ["=== CPU State ==="]
        for i in range(8):
            lines.append(f"  D{i}={self.D[i]:08X} ({self._s32(self.D[i]):12d})  "
                         f"A{i}={self.A[i]:08X}")
        sr = " ".join(f"{k}={v}" for k, v in self.SR.items())
        lines.append(f"  PC={self.PC:08X}  SR=[{sr}]  Cycles={self.cycle}")
        return "\n".join(lines)
