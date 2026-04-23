// double_precision.jl  —  64-битная арифметика на 32-битной машине
//
// Представление: 64-bit = (hi: int32, lo: int32)
//   значение = hi * 2^32 + lo_unsigned
//   hi — знаковая часть; lo интерпретируется беззнаново.
//
// Ключевые приёмы работы со знаковыми int32 как с беззнаковыми:
//
//   lo16(x): младшие 16 бит — через x % 65536 + коррекция знака.
//   hi16(x): старшие 16 бит — через lo16((x - lo16(x)) / 65536).
//     Деление (x - lo16(x)) / 65536 всегда кратно 65536 → без потери точности.
//
//   carry при сложении: ult(a+b, a) — если (a+b) <_u a, был перенос.
//   ult(a,b): беззнаковое <. Если знаковые биты разные — у кого 1, тот больше.
//
//   Умножение 32×32→64: разбиваем на 4 байта (byte0..byte3, 0..255).
//     ai*bj < 256^2 = 65536 — не переполняет int32.
//     Накапливаем в acc[8], пропагируем переносы.
//
//   Вывод (hi,lo) в десятичном: делим на 10 итерационно.
//     (rhi*2^32 + lo_real) mod 10 = (rhi*6 + lo_real_mod10) mod 10
//     При lo<0: lo_real = lo + 2^32, lo_real mod 10 = (lo mod 10 + 6) mod 10.
//     При lo<0: lo_real / 10: используем (lo + 6) вместо lo и +429496729.

// ── Беззнаковое сравнение ─────────────────────────────────────────────────────
fn ult(a: int, b: int) : int {
    var sa: int = 0;  var sb: int = 0;
    if (a < 0) { sa = 1; }
    if (b < 0) { sb = 1; }
    if (sa != sb) { return sb; }
    if (a < b)    { return 1; }
    return 0;
}

// ── Беззнаковые части слова ───────────────────────────────────────────────────
// lo16: всегда корректен (% с коррекцией знака)
fn lo16(x: int) : int {
    var r: int = x % 65536;
    if (r < 0) { r = r + 65536; }
    return r;
}

// hi16: (x - lo16(x)) делится на 65536 без остатка; затем lo16 для беззнаковости
fn hi16(x: int) : int {
    var l: int = lo16(x);
    var r: int = (x - l) / 65536;
    r = r % 65536;
    if (r < 0) { r = r + 65536; }
    return r;
}

// Байты через lo16/hi16 (0..255 каждый)
fn byte0(x: int) : int { return lo16(x) % 256; }
fn byte1(x: int) : int { return lo16(x) / 256; }
fn byte2(x: int) : int { return hi16(x) % 256; }
fn byte3(x: int) : int { return hi16(x) / 256; }

// ── Сложение ──────────────────────────────────────────────────────────────────
fn dp_add_lo(ahi: int, alo: int, bhi: int, blo: int) : int {
    return alo + blo;
}
fn dp_add_hi(ahi: int, alo: int, bhi: int, blo: int) : int {
    var carry: int = ult(alo + blo, alo);
    return ahi + bhi + carry;
}

// ── Вычитание ─────────────────────────────────────────────────────────────────
fn dp_sub_lo(ahi: int, alo: int, bhi: int, blo: int) : int {
    return alo - blo;
}
fn dp_sub_hi(ahi: int, alo: int, bhi: int, blo: int) : int {
    var borrow: int = ult(alo, blo);
    return ahi - bhi - borrow;
}

// ── Умножение 32×32 → 64 ──────────────────────────────────────────────────────
fn mul_accumulate(a: int, b: int, acc: str) : void {
    var a0: int = byte0(a);  var a1: int = byte1(a);
    var a2: int = byte2(a);  var a3: int = byte3(a);
    var b0: int = byte0(b);  var b1: int = byte1(b);
    var b2: int = byte2(b);  var b3: int = byte3(b);
    str_set(acc, 0, str_get(acc,0) + a0*b0);
    str_set(acc, 1, str_get(acc,1) + a0*b1 + a1*b0);
    str_set(acc, 2, str_get(acc,2) + a0*b2 + a1*b1 + a2*b0);
    str_set(acc, 3, str_get(acc,3) + a0*b3 + a1*b2 + a2*b1 + a3*b0);
    str_set(acc, 4, str_get(acc,4) + a1*b3 + a2*b2 + a3*b1);
    str_set(acc, 5, str_get(acc,5) + a2*b3 + a3*b2);
    str_set(acc, 6, str_get(acc,6) + a3*b3);
}
fn propagate(acc: str) : void {
    var i: int = 0;
    while (i < 7) {
        var v: int = str_get(acc, i);
        var carry: int = v / 256;
        var rem: int = v % 256;
        if (rem < 0) { rem = rem + 256; }
        str_set(acc, i, rem);
        str_set(acc, i+1, str_get(acc, i+1) + carry);
        i = i + 1;
    }
}
fn acc_lo(acc: str) : int {
    return str_get(acc,0) + str_get(acc,1)*256
         + str_get(acc,2)*65536 + str_get(acc,3)*16777216;
}
fn acc_hi(acc: str) : int {
    return str_get(acc,4) + str_get(acc,5)*256
         + str_get(acc,6)*65536 + str_get(acc,7)*16777216;
}
fn abs32(x: int) : int { if (x < 0) { return 0 - x; } return x; }
fn neg_lo(hi: int, lo: int) : int { return 0 - lo; }
fn neg_hi(hi: int, lo: int) : int {
    var b: int = 0;
    if (lo != 0) { b = 1; }
    return 0 - hi - b;
}

fn dp_mul_lo(a: int, b: int) : int {
    var neg: int = 0;
    if (a < 0) { neg = 1 - neg; }
    if (b < 0) { neg = 1 - neg; }
    var acc: str;
    var i: int = 0;
    while (i < 8) { str_set(acc, i, 0); i = i + 1; }
    str_set_len(acc, 8);
    mul_accumulate(abs32(a), abs32(b), acc);
    propagate(acc);
    var lo: int = acc_lo(acc);
    var hi: int = acc_hi(acc);
    if (neg == 1) { return neg_lo(hi, lo); }
    return lo;
}
fn dp_mul_hi(a: int, b: int) : int {
    var neg: int = 0;
    if (a < 0) { neg = 1 - neg; }
    if (b < 0) { neg = 1 - neg; }
    var acc: str;
    var i: int = 0;
    while (i < 8) { str_set(acc, i, 0); i = i + 1; }
    str_set_len(acc, 8);
    mul_accumulate(abs32(a), abs32(b), acc);
    propagate(acc);
    var lo: int = acc_lo(acc);
    var hi: int = acc_hi(acc);
    if (neg == 1) { return neg_hi(hi, lo); }
    return hi;
}

// ── Вывод десятичный ──────────────────────────────────────────────────────────
// dp_mod10(hi, lo): (hi*2^32 + lo_real) mod 10
//   rhi = |hi % 10|
//   lo_real mod 10 = (lo%10 + (lo<0 ? 6 : 0)) mod 10   [т.к. 2^32 mod 10 = 6]
//   total = (rhi*6 + lo_real_mod10) mod 10
fn dp_mod10(hi: int, lo: int) : int {
    var rhi: int = hi % 10;
    if (rhi < 0) { rhi = rhi + 10; }
    var lo_mod: int = lo % 10;
    if (lo < 0) {
        lo_mod = (lo_mod + 6) % 10;
        if (lo_mod < 0) { lo_mod = lo_mod + 10; }
    }
    if (lo_mod < 0) { lo_mod = lo_mod + 10; }
    var t: int = (rhi * 6 + lo_mod) % 10;
    if (t < 0) { t = t + 10; }
    return t;
}

// floor-деление на 10 (округление к -∞)
fn floor_div10(x: int) : int {
    var q: int = x / 10;
    var r: int = x % 10;
    if (r < 0) { q = q - 1; }
    return q;
}

// dp_div10_lo(hi, lo): lo-часть (hi*2^32 + lo_real) / 10
//   = |hi%10| * 429496729 + floor((rhi*6 + lo_adj) / 10)
// При lo>=0: lo_adj = lo
// При lo<0:  lo_adj = lo+6, добавляем ещё 429496729  [2^32/10 целая часть]
fn dp_div10_lo(hi: int, lo: int) : int {
    var rhi: int = hi % 10;
    if (rhi < 0) { rhi = rhi + 10; }
    var base: int = rhi * 429496729;
    if (lo >= 0) {
        return base + floor_div10(rhi * 6 + lo);
    } else {
        return base + 429496729 + floor_div10(rhi * 6 + lo + 6);
    }
}
fn dp_div10_hi(hi: int, lo: int) : int { return hi / 10; }

fn print_u64(hi: int, lo: int) : void {
    if (hi == 0 && lo == 0) { write_char(48); return; }
    var d:   int = dp_mod10(hi, lo);
    var qhi: int = dp_div10_hi(hi, lo);
    var qlo: int = dp_div10_lo(hi, lo);
    if (qhi != 0 || qlo != 0) { print_u64(qhi, qlo); }
    write_char(48 + d);
}
fn print_i64(hi: int, lo: int) : void {
    if (hi < 0) {
        write_char(45);
        print_u64(neg_hi(hi, lo), neg_lo(hi, lo));
    } else {
        print_u64(hi, lo);
    }
}

// ── Вывод hex ─────────────────────────────────────────────────────────────────
fn print_nibble(n: int) : void {
    if (n < 10) { write_char(48 + n); }
    else        { write_char(55 + n); }
}
fn print_hex16(v: int) : void {
    print_nibble(v / 4096);
    print_nibble((v / 256) % 16);
    print_nibble((v / 16) % 16);
    print_nibble(v % 16);
}
fn print_hex32(x: int) : void {
    print_hex16(hi16(x));
    print_hex16(lo16(x));
}
fn print_hex64(hi: int, lo: int) : void {
    write_char(48); write_char(120);
    print_hex32(hi); print_hex32(lo);
}

fn print_str(s: str) : void {
    var i: int = 0;  var n: int = str_len(s);
    while (i < n) { write_char(str_get(s, i)); i = i + 1; }
}
fn print_int(x: int) : void {
    if (x < 0) { write_char(45); x = 0 - x; }
    if (x == 0) { write_char(48); return; }
    if (x >= 10) { print_int(x / 10); }
    write_char(48 + x % 10);
}

// ── Главная программа ─────────────────────────────────────────────────────────
fn main() : void {
    var ahi: int = 0;  var alo: int = 0;
    var bhi: int = 0;  var blo: int = 0;
    var rhi: int = 0;  var rlo: int = 0;

    // ── Сложение ──────────────────────────────────────────────────────────
    print_str("=== 64-bit Addition ===\n");

    // 2^32 + 2^32 = 2^33 = 8589934592
    ahi=1; alo=0; bhi=1; blo=0;
    rhi=dp_add_hi(ahi,alo,bhi,blo); rlo=dp_add_lo(ahi,alo,bhi,blo);
    print_str("2^32 + 2^32                = "); print_i64(rhi,rlo); print_str("\n");

    // 0xFFFFFFFF + 1 = 2^32 (перенос из lo в hi)
    ahi=0; alo=-1; bhi=0; blo=1;
    rhi=dp_add_hi(ahi,alo,bhi,blo); rlo=dp_add_lo(ahi,alo,bhi,blo);
    print_str("0xFFFFFFFF + 1             = ");
    print_i64(rhi,rlo); print_str("  "); print_hex64(rhi,rlo); print_str("\n");

    // INT64_MAX + 1 = 0x8000000000000000 (переполнение в знаковый минимум)
    ahi=2147483647; alo=-1; bhi=0; blo=1;
    rhi=dp_add_hi(ahi,alo,bhi,blo); rlo=dp_add_lo(ahi,alo,bhi,blo);
    print_str("INT64_MAX + 1              = "); print_hex64(rhi,rlo); print_str("\n");

    // ── Вычитание ─────────────────────────────────────────────────────────
    print_str("\n=== 64-bit Subtraction ===\n");

    // 2^32 - 1 = 4294967295 (заём из hi)
    ahi=1; alo=0; bhi=0; blo=1;
    rhi=dp_sub_hi(ahi,alo,bhi,blo); rlo=dp_sub_lo(ahi,alo,bhi,blo);
    print_str("2^32 - 1                   = ");
    print_i64(rhi,rlo); print_str("  "); print_hex64(rhi,rlo); print_str("\n");

    // 2^33 - 2^32 = 2^32
    ahi=2; alo=0; bhi=1; blo=0;
    rhi=dp_sub_hi(ahi,alo,bhi,blo); rlo=dp_sub_lo(ahi,alo,bhi,blo);
    print_str("2^33 - 2^32                = "); print_i64(rhi,rlo); print_str("\n");

    // 0 - 1 = -1
    ahi=0; alo=0; bhi=0; blo=1;
    rhi=dp_sub_hi(ahi,alo,bhi,blo); rlo=dp_sub_lo(ahi,alo,bhi,blo);
    print_str("0 - 1                      = ");
    print_i64(rhi,rlo); print_str("  "); print_hex64(rhi,rlo); print_str("\n");

    // ── Умножение 32×32 → 64 ─────────────────────────────────────────────
    print_str("\n=== 64-bit Multiplication (32x32->64) ===\n");

    rhi=dp_mul_hi(100000,100000); rlo=dp_mul_lo(100000,100000);
    print_str("100000 * 100000            = "); print_i64(rhi,rlo); print_str("\n");

    rhi=dp_mul_hi(1000000,1000000); rlo=dp_mul_lo(1000000,1000000);
    print_str("1000000 * 1000000          = "); print_i64(rhi,rlo); print_str("\n");

    rhi=dp_mul_hi(2147483647,2); rlo=dp_mul_lo(2147483647,2);
    print_str("2147483647 * 2             = "); print_i64(rhi,rlo); print_str("\n");

    rhi=dp_mul_hi(2147483647,2147483647); rlo=dp_mul_lo(2147483647,2147483647);
    print_str("2147483647^2               = "); print_i64(rhi,rlo); print_str("\n");

    rhi=dp_mul_hi(-1000000,1000000); rlo=dp_mul_lo(-1000000,1000000);
    print_str("-1000000 * 1000000         = "); print_i64(rhi,rlo); print_str("\n");

    rhi=dp_mul_hi(-999,-999); rlo=dp_mul_lo(-999,-999);
    print_str("(-999) * (-999)            = "); print_i64(rhi,rlo); print_str("\n");

    rhi=dp_mul_hi(999999999,999999999); rlo=dp_mul_lo(999999999,999999999);
    print_str("999999999^2                = "); print_i64(rhi,rlo); print_str("\n");

    // ── Цепочка операций: (a+b)*c ────────────────────────────────────────
    print_str("\n=== Chain: (2^32 + 7) * 3 ===\n");
    // A = 2^32 + 7 = (1, 7)
    // A + A + A = 3*A = 3*2^32 + 21 = (3, 21)
    ahi=1; alo=7;
    var sahi: int = dp_add_hi(ahi,alo,ahi,alo);
    var salo: int = dp_add_lo(ahi,alo,ahi,alo);
    var thi: int = dp_add_hi(sahi,salo,ahi,alo);
    var tlo: int = dp_add_lo(sahi,salo,ahi,alo);
    print_str("(2^32+7)*3 = "); print_i64(thi,tlo);
    print_str("  "); print_hex64(thi,tlo); print_str("\n");

    // ── Степени двойки 2^0 .. 2^39 ───────────────────────────────────────
    print_str("\n=== Powers of 2: 2^0 .. 2^39 ===\n");
    ahi=0; alo=1;
    var k: int = 0;
    while (k < 40) {
        print_str("2^"); print_int(k);
        print_str(" = "); print_i64(ahi,alo); print_str("\n");
        var nlo: int = dp_add_lo(ahi,alo,ahi,alo);
        var nhi: int = dp_add_hi(ahi,alo,ahi,alo);
        ahi = nhi; alo = nlo;
        k = k + 1;
    }
}