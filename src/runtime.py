"""
runtime.py — генератор встроенной runtime-библиотеки JavaLight (JL).

Содержит:
  - BUILTINS        : множество имён встроенных функций, видимых из JL-кода
  - RT_CALLS        : граф транзитивных зависимостей (builtin → {__rt_*})
  - collect_calls() : обход AST для сбора вызовов
  - reachable_fns() : BFS от main для определения нужных функций
  - RuntimeEmitter  : класс, генерирующий машинный код rt-функций в CodeGen

Использование из compiler.py:
    from runtime import BUILTINS, RT_CALLS, reachable_fns, RuntimeEmitter

    # анализ достижимости
    reached   = reachable_fns(prog)
    needed_rt = {n for n in reached if n.startswith("__rt_")}

    # генерация кода
    emitter = RuntimeEmitter(codegen)
    emitter.emit(needed_rt)
"""

from config import IO_IN, IO_OUT

# ── ISA-хелперы импортируются из компилятора через параметр codegen ───────────
# RuntimeEmitter получает объект CodeGen и использует его методы:
#   cg._label(name), cg._emit(bytes), cg._branch(op, label),
#   cg._jsr(label),  cg._jmp(label),  cg._lbl(prefix)
# а также ISA-функции через тот же модуль:
#   Op, _ei, _rts, _link, _unlk, Imm, D, A, Ind, Post, Disp, FP
#
# Чтобы не дублировать ISA-код и не создавать циклический импорт,
# RuntimeEmitter принимает codegen и ISA-namespace как параметры __init__.


# ═══════════════════════════════════════════════════════════════════════════════
#  Публичный интерфейс встроенных функций (видны из JL-кода)
# ═══════════════════════════════════════════════════════════════════════════════

BUILTINS: set[str] = {
    "read_char",
    "write_char",
    "str_len",
    "str_get",
    "str_set",
    "str_set_len",
}

# ═══════════════════════════════════════════════════════════════════════════════
#  Граф транзитивных зависимостей для анализа достижимости
# ═══════════════════════════════════════════════════════════════════════════════

# Каждая запись: имя_вызываемого_из_JL → {__rt_*-функции, которые реально нужны}
# Используется в reachable_fns() при BFS обходе.
RT_CALLS: dict[str, set[str]] = {
    # Публичные встроенные → соответствующая rt-реализация
    "write_char": {"__rt_write_char"},
    "read_char": {"__rt_read_char"},
    "str_len": {"__rt_str_len"},
    "str_get": {"__rt_str_get"},
    "str_set": {"__rt_str_set"},
    "str_set_len": {"__rt_str_set_len"},
    # Внутренние rt-функции → их зависимости друг от друга
    "__rt_print_str": {"__rt_write_char"},
    "__rt_read_line": {"__rt_read_char"},
}

# Фиксированный порядок генерации — гарантирует стабильные адреса
# между компиляциями одной и той же программы.
RT_ORDER: list[str] = [
    "__rt_write_char",
    "__rt_read_char",
    "__rt_str_len",
    "__rt_str_get",
    "__rt_str_set",
    "__rt_str_set_len",
    "__rt_print_str",
    "__rt_read_line",
]


# ═══════════════════════════════════════════════════════════════════════════════
#  Анализ достижимости
# ═══════════════════════════════════════════════════════════════════════════════


def collect_calls(node) -> set:
    """Рекурсивно собрать все имена вызываемых функций в AST-узле."""
    # Импортируем AST-классы здесь, чтобы не создавать циклическую зависимость
    # (compiler импортирует runtime, runtime не импортирует compiler).
    # Вместо этого используем duck-typing по атрибутам узлов.
    calls = set()

    cls = type(node).__name__

    if cls == "Call":
        calls.add(node.name)
        for a in node.args:
            calls |= collect_calls(a)
    elif cls == "BinOp":
        calls |= collect_calls(node.left)
        calls |= collect_calls(node.right)
    elif cls == "UnOp":
        calls |= collect_calls(node.operand)
    elif cls == "Assign":
        calls |= collect_calls(node.value)
    elif cls == "ExprStmt":
        calls |= collect_calls(node.expr)
    elif cls == "VarDecl":
        if node.init:
            calls |= collect_calls(node.init)
    elif cls == "ReturnStmt":
        if node.value:
            calls |= collect_calls(node.value)
    elif cls == "IfStmt":
        calls |= collect_calls(node.cond)
        calls |= collect_calls(node.then_)
        if node.else_:
            calls |= collect_calls(node.else_)
    elif cls == "WhileStmt":
        calls |= collect_calls(node.cond)
        calls |= collect_calls(node.body)
    elif cls == "ForStmt":
        if node.init:
            calls |= collect_calls(node.init)
        if node.cond:
            calls |= collect_calls(node.cond)
        if node.step:
            calls |= collect_calls(node.step)
        calls |= collect_calls(node.body)
    elif cls == "Block":
        for s in node.stmts:
            calls |= collect_calls(s)
    elif cls == "FnDecl":
        calls |= collect_calls(node.body)
    elif cls == "Program":
        for d in node.decls:
            calls |= collect_calls(d)

    return calls


def reachable_fns(prog) -> set:
    """
    Вернуть множество всех достижимых имён функций от main.
    Включает пользовательские функции и __rt_* runtime-функции.

    Алгоритм: BFS от 'main'.
    - Для пользовательских функций: переходим по прямым вызовам из тела.
    - Для builtin-имён (write_char и т.д.): добавляем __rt_* из RT_CALLS.
    - Для __rt_*-функций: добавляем их зависимости из RT_CALLS.
    """
    # Собрать карту: имя_пользовательской_функции → множество прямых вызовов
    user_fn_calls: dict[str, set] = {}
    for d in prog.decls:
        if type(d).__name__ == "FnDecl":
            user_fn_calls[d.name] = collect_calls(d.body)

    # BFS от main
    visited: set[str] = set()
    queue: list[str] = ["main"]

    while queue:
        fn = queue.pop()
        if fn in visited:
            continue
        visited.add(fn)

        # Прямые вызовы из тела пользовательской функции
        for callee in user_fn_calls.get(fn, set()):
            if callee not in visited:
                queue.append(callee)

        # Транзитивные runtime-зависимости (builtin → __rt_*, __rt_* → __rt_*)
        for rt_callee in RT_CALLS.get(fn, set()):
            if rt_callee not in visited:
                queue.append(rt_callee)

    return visited


# ═══════════════════════════════════════════════════════════════════════════════
#  Генератор кода runtime-библиотеки
# ═══════════════════════════════════════════════════════════════════════════════


class RuntimeEmitter:
    """
    Генерирует машинный код runtime-функций в переданный CodeGen.

    Параметры __init__:
        cg   — объект CodeGen (из compiler.py)
        isa  — модуль или namespace с ISA-хелперами:
               Op, _ei, _rts, _link, _unlk, Imm, D, A, Ind, Post, Disp, FP

    Использование:
        emitter = RuntimeEmitter(cg, isa_ns)
        emitter.emit(needed_rt_set)
    """

    def __init__(self, cg, isa):
        self._cg = cg
        self._isa = isa

    # ── Делегирование к CodeGen ───────────────────────────────────────────────

    def _label(self, name):
        self._cg._label(name)

    def _emit(self, b):
        self._cg._emit(b)

    def _branch(self, op, label):
        self._cg._branch(op, label)

    def _jsr(self, label):
        self._cg._jsr(label)

    def _jmp(self, label):
        self._cg._jmp(label)

    def _lbl(self, prefix):
        return self._cg._lbl(prefix)

    # ── Доступ к ISA ──────────────────────────────────────────────────────────

    @property
    def _Op(self):
        return self._isa.Op

    @property
    def _ei(self):
        return self._isa._ei

    @property
    def _rts(self):
        return self._isa._rts

    @property
    def _link(self):
        return self._isa._link

    @property
    def _unlk(self):
        return self._isa._unlk

    def _Imm(self, v):
        return self._isa.Imm(v)

    def _D(self, n):
        return self._isa.D(n)

    def _A(self, n):
        return self._isa.A(n)

    def _Ind(self, n):
        return self._isa.Ind(n)

    def _Post(self, n):
        return self._isa.Post(n)

    def _Disp(self, n, d):
        return self._isa.Disp(n, d)

    @property
    def _FP(self):
        return self._isa.FP

    # ── Публичный метод генерации ─────────────────────────────────────────────

    def emit(self, needed: set) -> None:
        """
        Генерировать только те rt-функции, чьи имена входят в needed.
        Порядок генерации фиксирован (RT_ORDER) для стабильности адресов.
        """
        emitters = {
            "__rt_write_char": self._emit_write_char,
            "__rt_read_char": self._emit_read_char,
            "__rt_str_len": self._emit_str_len,
            "__rt_str_get": self._emit_str_get,
            "__rt_str_set": self._emit_str_set,
            "__rt_str_set_len": self._emit_str_set_len,
            "__rt_print_str": self._emit_print_str,
            "__rt_read_line": self._emit_read_line,
        }
        for name in RT_ORDER:
            if name in needed:
                emitters[name]()

    # ── Реализации rt-функций ─────────────────────────────────────────────────

    def _emit_write_char(self):
        """write_char(D0) → void  :  записать символ в порт вывода."""
        Op, ei, rts = self._Op, self._ei, self._rts
        self._label("__rt_write_char")
        self._emit(ei(Op.MOVEA, self._Imm(IO_OUT), self._A(5)))
        self._emit(ei(Op.MOVE, self._D(0), self._Ind(5)))
        self._emit(rts())

    def _emit_read_char(self):
        """read_char() → D0  :  прочитать символ из порта ввода."""
        Op, ei, rts = self._Op, self._ei, self._rts
        self._label("__rt_read_char")
        self._emit(ei(Op.MOVEA, self._Imm(IO_IN), self._A(5)))
        self._emit(ei(Op.MOVE, self._Ind(5), self._D(0)))
        self._emit(rts())

    def _emit_str_len(self):
        """str_len(A0) → D0  :  длина Pascal-строки (слово-префикс)."""
        Op, ei, rts = self._Op, self._ei, self._rts
        self._label("__rt_str_len")
        self._emit(ei(Op.MOVE, self._Ind(0), self._D(0)))
        self._emit(rts())

    def _emit_str_get(self):
        """str_get(A0, D0) → D0  :  символ по индексу D0."""
        Op, ei = self._Op, self._ei
        FP = self._FP
        self._label("__rt_str_get")
        self._emit(self._link(FP, -8))
        self._emit(ei(Op.MOVE, self._D(1), self._Disp(FP, -4)))
        self._emit(ei(Op.MOVEA, self._A(0), self._A(1)))
        self._emit(ei(Op.ADD, self._Imm(4), self._A(1)))  # пропустить length-слово
        self._emit(ei(Op.MOVE, self._D(0), self._D(1)))
        self._emit(ei(Op.MUL, self._Imm(4), self._D(1)))  # D1 = index * 4
        self._emit(ei(Op.ADD, self._D(1), self._A(1)))  # A1 = &str[index]
        self._emit(ei(Op.MOVE, self._Ind(1), self._D(0)))  # D0 = char
        self._emit(ei(Op.MOVE, self._Disp(FP, -4), self._D(1)))
        self._emit(self._unlk(FP))
        self._emit(self._rts())

    def _emit_str_set(self):
        """str_set(A0, D0, D1)  :  установить символ D1 по индексу D0."""
        Op, ei = self._Op, self._ei
        FP = self._FP
        self._label("__rt_str_set")
        self._emit(self._link(FP, -8))
        self._emit(ei(Op.MOVE, self._D(2), self._Disp(FP, -4)))
        self._emit(ei(Op.MOVEA, self._A(0), self._A(1)))
        self._emit(ei(Op.ADD, self._Imm(4), self._A(1)))
        self._emit(ei(Op.MOVE, self._D(0), self._D(2)))
        self._emit(ei(Op.MUL, self._Imm(4), self._D(2)))
        self._emit(ei(Op.ADD, self._D(2), self._A(1)))
        self._emit(ei(Op.MOVE, self._D(1), self._Ind(1)))
        self._emit(ei(Op.MOVE, self._Disp(FP, -4), self._D(2)))
        self._emit(self._unlk(FP))
        self._emit(self._rts())

    def _emit_str_set_len(self):
        """str_set_len(A0, D0)  :  установить length-слово строки."""
        Op, ei, rts = self._Op, self._ei, self._rts
        self._label("__rt_str_set_len")
        self._emit(ei(Op.MOVE, self._D(0), self._Ind(0)))
        self._emit(rts())

    def _emit_print_str(self):
        """__rt_print_str(A0)  :  внутренняя: вывести Pascal-строку."""
        Op, ei = self._Op, self._ei
        FP = self._FP
        self._label("__rt_print_str")
        self._emit(self._link(FP, -16))
        self._emit(ei(Op.MOVE, self._D(1), self._Disp(FP, -4)))
        self._emit(ei(Op.MOVE, self._D(2), self._Disp(FP, -8)))
        self._emit(ei(Op.MOVEA, self._A(0), self._A(1)))
        self._emit(ei(Op.MOVE, self._Post(1), self._D(1)))  # D1 = len; A1 += 4
        self._emit(ei(Op.MOVE, self._Imm(0), self._D(2)))  # i = 0
        lp = self._lbl("ps_loop")
        dn = self._lbl("ps_done")
        self._label(lp)
        self._emit(ei(Op.CMP, self._D(1), self._D(2)))
        self._branch(Op.BGE, dn)
        self._emit(ei(Op.MOVE, self._Post(1), self._D(0)))
        self._jsr("__rt_write_char")
        self._emit(ei(Op.ADD, self._Imm(1), self._D(2)))
        self._jmp(lp)
        self._label(dn)
        self._emit(ei(Op.MOVE, self._Disp(FP, -4), self._D(1)))
        self._emit(ei(Op.MOVE, self._Disp(FP, -8), self._D(2)))
        self._emit(self._unlk(FP))
        self._emit(self._rts())

    def _emit_read_line(self):
        """__rt_read_line(A0, D0=maxlen) → D0=actual_len  :  внутренняя."""
        Op, ei = self._Op, self._ei
        FP = self._FP
        self._label("__rt_read_line")
        self._emit(self._link(FP, -16))
        self._emit(ei(Op.MOVE, self._D(1), self._Disp(FP, -4)))
        self._emit(ei(Op.MOVE, self._D(2), self._Disp(FP, -8)))
        self._emit(ei(Op.MOVE, self._D(0), self._D(1)))  # D1 = maxlen
        self._emit(ei(Op.MOVEA, self._A(0), self._A(1)))
        self._emit(ei(Op.ADD, self._Imm(4), self._A(1)))  # A1 = chars start
        self._emit(ei(Op.MOVE, self._Imm(0), self._D(2)))  # i = 0
        rl = self._lbl("rl_loop")
        rs = self._lbl("rl_store")
        self._label(rl)
        self._emit(ei(Op.CMP, self._D(1), self._D(2)))
        self._branch(Op.BGE, rs)
        self._jsr("__rt_read_char")
        self._emit(ei(Op.CMP, self._Imm(0xFFFFFFFF), self._D(0)))
        self._branch(Op.BEQ, rs)
        self._emit(ei(Op.CMP, self._Imm(10), self._D(0)))  # '\n'
        self._branch(Op.BEQ, rs)
        self._emit(ei(Op.MOVE, self._D(0), self._Post(1)))
        self._emit(ei(Op.ADD, self._Imm(1), self._D(2)))
        self._jmp(rl)
        self._label(rs)
        self._emit(ei(Op.MOVE, self._D(2), self._Ind(0)))  # store length
        self._emit(ei(Op.MOVE, self._D(2), self._D(0)))  # return value
        self._emit(ei(Op.MOVE, self._Disp(FP, -4), self._D(1)))
        self._emit(ei(Op.MOVE, self._Disp(FP, -8), self._D(2)))
        self._emit(self._unlk(FP))
        self._emit(self._rts())


# ═══════════════════════════════════════════════════════════════════════════════
#  InlineEmitter — генерация встроенных операций без вызова функций
# ═══════════════════════════════════════════════════════════════════════════════


class InlineEmitter:
    """
    Генерирует код встроенных операций прямо на месте вызова — без JSR,
    LINK, UNLK, RTS и передачи аргументов через стек вызовов.

    Соглашение о регистрах совпадает с call-режимом:
      read_char()        → результат в D0
      write_char()       → аргумент в D0
      str_len(s)         → A0 = адрес строки → результат в D0
      str_get(s, i)      → A0 = строка, D0 = индекс → результат в D0
      str_set(s, i, c)   → A0 = строка, D0 = индекс, D1 = символ
      str_set_len(s, n)  → A0 = строка, D0 = длина

    Scratch-регистры (могут быть перезаписаны внутри операции):
      A5 — адрес IO-порта  (read_char, write_char)
      A1 — временный указатель  (str_get, str_set)
      D1 — временный  (str_get; сохраняется/восстанавливается через стек)
      D2 — временный  (str_set; сохраняется/восстанавливается через стек)
    """

    def __init__(self, cg, isa):
        self._cg  = cg
        self._isa = isa

    # ── Делегирование к CodeGen ───────────────────────────────────────────────

    def _emit(self, b):        self._cg._emit(b)
    def _lbl(self, prefix):    return self._cg._lbl(prefix)
    def _label(self, name):    self._cg._label(name)
    def _branch(self, op, lbl): self._cg._branch(op, lbl)
    def _jmp(self, lbl):       self._cg._jmp(lbl)

    # ── Доступ к ISA ──────────────────────────────────────────────────────────

    @property
    def _Op(self):   return self._isa.Op
    @property
    def _ei(self):   return self._isa._ei
    @property
    def _SP(self):   return self._isa.SP

    def _Imm(self, v):      return self._isa.Imm(v)
    def _D(self, n):        return self._isa.D(n)
    def _A(self, n):        return self._isa.A(n)
    def _Ind(self, n):      return self._isa.Ind(n)
    def _Pre(self, n):      return self._isa.Pre(n)
    def _Post(self, n):     return self._isa.Post(n)

    # ── Встроенные операции ───────────────────────────────────────────────────

    def write_char(self):
        """D0 → порт вывода IO_OUT. 2 инструкции."""
        Op, ei = self._Op, self._ei
        self._emit(ei(Op.MOVEA, self._Imm(IO_OUT), self._A(5)))
        self._emit(ei(Op.MOVE,  self._D(0),         self._Ind(5)))

    def read_char(self):
        """Порт ввода IO_IN → D0. 2 инструкции."""
        Op, ei = self._Op, self._ei
        self._emit(ei(Op.MOVEA, self._Imm(IO_IN), self._A(5)))
        self._emit(ei(Op.MOVE,  self._Ind(5),      self._D(0)))

    def str_len(self):
        """mem[A0] → D0. 1 инструкция."""
        Op, ei = self._Op, self._ei
        self._emit(ei(Op.MOVE, self._Ind(0), self._D(0)))

    def str_get(self):
        """
        A0=строка, D0=индекс → D0=символ.
        EA = A0 + 4 + D0*4.
        D1 используется как scratch: сохраняется на стек и восстанавливается.
        """
        Op, ei, SP = self._Op, self._ei, self._SP
        # Сохранить D1
        self._emit(ei(Op.MOVE, self._D(1), self._Pre(SP)))    # push D1
        # A1 = A0 + 4 + D0*4
        self._emit(ei(Op.MOVEA, self._A(0),   self._A(1)))
        self._emit(ei(Op.ADD,   self._Imm(4), self._A(1)))    # пропустить length-слово
        self._emit(ei(Op.MOVE,  self._D(0),   self._D(1)))
        self._emit(ei(Op.MUL,   self._Imm(4), self._D(1)))    # D1 = index * 4
        self._emit(ei(Op.ADD,   self._D(1),   self._A(1)))    # A1 = &str[index]
        # D0 = mem[A1]
        self._emit(ei(Op.MOVE, self._Ind(1), self._D(0)))
        # Восстановить D1
        self._emit(ei(Op.MOVE, self._Post(SP), self._D(1)))   # pop D1

    def str_set(self):
        """
        A0=строка, D0=индекс, D1=символ → mem[A0+4+D0*4] = D1.
        D2 используется как scratch: сохраняется на стек и восстанавливается.
        """
        Op, ei, SP = self._Op, self._ei, self._SP
        # Сохранить D2
        self._emit(ei(Op.MOVE, self._D(2), self._Pre(SP)))    # push D2
        # A1 = A0 + 4 + D0*4
        self._emit(ei(Op.MOVEA, self._A(0),   self._A(1)))
        self._emit(ei(Op.ADD,   self._Imm(4), self._A(1)))
        self._emit(ei(Op.MOVE,  self._D(0),   self._D(2)))
        self._emit(ei(Op.MUL,   self._Imm(4), self._D(2)))    # D2 = index * 4
        self._emit(ei(Op.ADD,   self._D(2),   self._A(1)))    # A1 = &str[index]
        # mem[A1] = D1
        self._emit(ei(Op.MOVE, self._D(1), self._Ind(1)))
        # Восстановить D2
        self._emit(ei(Op.MOVE, self._Post(SP), self._D(2)))   # pop D2

    def str_set_len(self):
        """mem[A0] = D0. 1 инструкция."""
        Op, ei = self._Op, self._ei
        self._emit(ei(Op.MOVE, self._D(0), self._Ind(0)))