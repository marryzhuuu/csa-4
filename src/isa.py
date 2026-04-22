"""
M68k-inspired ISA — Binary Encoding
=====================================

Instruction word layout (32 bits):
  [31..24] opcode   (8 bits)
  [23..20] src_mode (4 bits)
  [19..16] dst_mode (4 bits)
  [15..12] src_reg  (4 bits)   register index (D0-D7 = 0-7, A0-A7 = 8-15)
  [11.. 8] dst_reg  (4 bits)
  [ 7]     size     (1 bit)    0=long(32), 1=byte(8)
  [ 6.. 0] reserved/extra (7 bits, used for displacement index reg etc.)

Addressing modes (4 bits):
  0x0  REG_D      — data register direct
  0x1  REG_A      — address register direct
  0x2  IMMED      — immediate (next word follows)
  0x3  MEM_IND    — (An)
  0x4  MEM_POST   — (An)+
  0x5  MEM_PRE    — -(An)
  0x6  MEM_DISP   — d(An)        extra word: signed 32-bit displacement
  0x7  MEM_IDX    — d(An,Xn)     extra word: displacement; extra nibbles: index reg
  0xF  NONE       — operand not used

Variable-length instructions:
  base word (4 bytes)
  + immediate value if src/dst mode == IMMED  (+4 bytes)
  + immediate value if other side is also IMM (+4 bytes)
  + displacement word if MEM_DISP or MEM_IDX  (+4 bytes)
  + index-reg word if MEM_IDX                 (+4 bytes)
  + branch target if branch/jmp/jsr           (+4 bytes in extra slot)

Opcodes:
"""

import struct
from enum import IntEnum


# ── Opcodes ──────────────────────────────────────────────────────────────────
class Op(IntEnum):
    MOVE   = 0x01
    MOVEA  = 0x02
    ADD    = 0x10
    SUB    = 0x11
    MUL    = 0x12
    DIV    = 0x13
    CMP    = 0x14
    AND    = 0x20
    OR     = 0x21
    XOR    = 0x22
    NOT    = 0x23
    ASL    = 0x30
    ASR    = 0x31
    LSL    = 0x32
    LSR    = 0x33
    JMP    = 0x40
    JSR    = 0x41
    RTS    = 0x42
    BEQ    = 0x50
    BNE    = 0x51
    BLT    = 0x52
    BGT    = 0x53
    BLE    = 0x54
    BGE    = 0x55
    BMI    = 0x56
    BPL    = 0x57
    BCC    = 0x58
    BCS    = 0x59
    BVC    = 0x5A
    BVS    = 0x5B
    BRA    = 0x5C
    LINK   = 0x60
    UNLK   = 0x61
    HALT   = 0xFF

OP_NAMES = {v: k for k, v in Op.__members__.items()}

# ── Addressing modes ──────────────────────────────────────────────────────────
class AM(IntEnum):
    REG_D    = 0x0
    REG_A    = 0x1
    IMMED    = 0x2
    MEM_IND  = 0x3
    MEM_POST = 0x4
    MEM_PRE  = 0x5
    MEM_DISP = 0x6
    MEM_IDX  = 0x7
    NONE     = 0xF

AM_NAMES = {v: k for k, v in AM.__members__.items()}

# ── Register encoding ─────────────────────────────────────────────────────────
def reg_num(name: str) -> int:
    """D0-D7 → 0-7,  A0-A7 → 8-15"""
    name = name.upper()
    if name.startswith("D"):
        return int(name[1:])
    if name.startswith("A"):
        return 8 + int(name[1:])
    raise ValueError(f"Unknown register: {name}")

def reg_name(n: int) -> str:
    if n < 8:
        return f"D{n}"
    return f"A{n-8}"

# ── Operand descriptor ────────────────────────────────────────────────────────
class Operand:
    """Parsed operand with addressing mode and optional extras."""
    def __init__(self, mode, reg=0, imm=0, disp=0, idx_reg=0):
        self.mode    = AM(mode)
        self.reg     = reg      # register number (0-15)
        self.imm     = imm      # immediate value
        self.disp    = disp     # displacement
        self.idx_reg = idx_reg  # index register number

    def extra_words(self):
        """Return list of 4-byte ints to append after the base word."""
        if self.mode == AM.IMMED:
            return [self.imm & 0xFFFFFFFF]
        if self.mode == AM.MEM_DISP:
            return [self.disp & 0xFFFFFFFF]
        if self.mode == AM.MEM_IDX:
            return [self.disp & 0xFFFFFFFF, self.idx_reg]
        return []

    def mnemonic(self):
        m = self.mode
        rn = reg_name(self.reg)
        if m == AM.REG_D:   return rn
        if m == AM.REG_A:   return rn
        if m == AM.IMMED:   return f"#{self.imm}"
        if m == AM.MEM_IND: return f"({rn})"
        if m == AM.MEM_POST:return f"({rn})+"
        if m == AM.MEM_PRE: return f"-({rn})"
        if m == AM.MEM_DISP:return f"{self.disp}({rn})"
        if m == AM.MEM_IDX: return f"{self.disp}({rn},{reg_name(self.idx_reg)})"
        if m == AM.NONE:    return ""
        return "?"

NONE_OP = Operand(AM.NONE)

# ── Instruction encoding ──────────────────────────────────────────────────────
def encode_instr(opcode: Op, src: Operand, dst: Operand,
                 size_byte=False) -> bytes:
    """
    Encode one instruction into bytes.
    Base word:  [opcode:8][src_mode:4][dst_mode:4][src_reg:4][dst_reg:4][size:1][extra_lo:7]
    Followed by extra words for src then dst.
    """
    word0 = (
        (int(opcode)       << 24) |
        (int(src.mode)     << 20) |
        (int(dst.mode)     << 16) |
        (src.reg           << 12) |
        (dst.reg           <<  8) |
        ((1 if size_byte else 0) << 7)
    )
    parts = [struct.pack(">I", word0)]
    for w in src.extra_words():
        parts.append(struct.pack(">I", w & 0xFFFFFFFF))
    for w in dst.extra_words():
        parts.append(struct.pack(">I", w & 0xFFFFFFFF))
    return b"".join(parts)

def encode_branch(opcode: Op, target_addr: int) -> bytes:
    """Branch/jmp/jsr: base word + 4-byte absolute target."""
    word0 = (int(opcode) << 24) | (int(AM.IMMED) << 20) | (int(AM.NONE) << 16)
    return struct.pack(">II", word0, target_addr & 0xFFFFFFFF)

def encode_halt() -> bytes:
    word0 = (int(Op.HALT) << 24) | (int(AM.NONE) << 20) | (int(AM.NONE) << 16)
    return struct.pack(">I", word0)

def encode_rts() -> bytes:
    word0 = (int(Op.RTS) << 24) | (int(AM.NONE) << 20) | (int(AM.NONE) << 16)
    return struct.pack(">I", word0)

def encode_link(areg: int, disp: int) -> bytes:
    """link An, #disp"""
    src = Operand(AM.IMMED, imm=disp)
    dst = Operand(AM.REG_A, reg=areg)
    return encode_instr(Op.LINK, src, dst)

def encode_unlk(areg: int) -> bytes:
    src = Operand(AM.REG_A, reg=areg)
    return encode_instr(Op.UNLK, src, NONE_OP)

# ── Decode (for debug output) ─────────────────────────────────────────────────
def decode_instr(data: bytes, offset: int):
    """
    Decode instruction at byte offset.
    Returns (mnemonic_str, bytes_consumed).
    """
    if offset + 4 > len(data):
        return "???", 1
    word0 = struct.unpack_from(">I", data, offset)[0]
    opcode   = (word0 >> 24) & 0xFF
    src_mode = AM((word0 >> 20) & 0xF)
    dst_mode = AM((word0 >> 16) & 0xF)
    src_reg  = (word0 >> 12) & 0xF
    dst_reg  = (word0 >>  8) & 0xF
    size_b   = bool((word0 >> 7) & 1)
    sz       = ".b" if size_b else ".l"

    cursor = offset + 4

    def read_word():
        nonlocal cursor
        v = struct.unpack_from(">I", data, cursor)[0]
        cursor += 4
        return v

    def decode_op(mode, reg):
        nonlocal cursor
        if mode == AM.IMMED:
            val = read_word()
            return f"#{val}", val
        if mode == AM.MEM_DISP:
            d = struct.unpack_from(">i", data, cursor)[0]
            cursor += 4
            return f"{d}({reg_name(reg)})", d
        if mode == AM.MEM_IDX:
            d   = struct.unpack_from(">i", data, cursor)[0]; cursor += 4
            ir  = read_word()
            return f"{d}({reg_name(reg)},{reg_name(ir)})", d
        if mode == AM.NONE:
            return "", 0
        simple = {
            AM.REG_D:    reg_name(reg),
            AM.REG_A:    reg_name(reg),
            AM.MEM_IND:  f"({reg_name(reg)})",
            AM.MEM_POST: f"({reg_name(reg)})+",
            AM.MEM_PRE:  f"-({reg_name(reg)})",
        }
        return simple[mode], 0

    op_name = OP_NAMES.get(opcode, f"op{opcode:02X}")

    # branch / jmp / jsr have target as IMMED in src
    if opcode in (Op.JMP, Op.JSR, Op.BEQ, Op.BNE, Op.BLT, Op.BGT,
                  Op.BLE, Op.BGE, Op.BMI, Op.BPL, Op.BCC, Op.BCS,
                  Op.BVC, Op.BVS, Op.BRA):
        target = read_word()
        return f"{op_name} @{target}", cursor - offset

    if opcode == Op.HALT:
        return "halt", 4
    if opcode == Op.RTS:
        return "rts", 4
    if opcode == Op.UNLK:
        src_s, _ = decode_op(src_mode, src_reg)
        return f"unlk {src_s}", cursor - offset

    src_s, _ = decode_op(src_mode, src_reg)
    dst_s, _ = decode_op(dst_mode, dst_reg)

    if dst_s:
        return f"{op_name}{sz} {src_s}, {dst_s}", cursor - offset
    return f"{op_name}{sz} {src_s}", cursor - offset

def disassemble(code: bytes, base=0) -> list[tuple[int,str,str]]:
    """Returns list of (addr, hexcode, mnemonic)."""
    result = []
    offset = 0
    while offset < len(code):
        addr = base + offset
        mnem, size = decode_instr(code, offset)
        hex_bytes = code[offset:offset+size].hex().upper()
        result.append((addr, hex_bytes, mnem))
        offset += size
    return result

def write_debug(code: bytes, path: str, base=0):
    rows = disassemble(code, base)
    with open(path, "w") as f:
        for addr, hx, mn in rows:
            f.write(f"{addr} - {hx} - {mn}\n")
