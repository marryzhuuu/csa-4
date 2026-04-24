"""Golden тесты компилятора и эмулятора.

Конфигурационнфе файлы: "golden/*.yml"
"""

import contextlib
import io
import logging
import os
import tempfile

import pytest

import compiler
import emulator

MAX_LOG = 4000


@pytest.mark.golden_test("golden/*.yml")
def test_compiler_and_emulator(golden, caplog):
    """Используется подход golden tests. У него не самая удачная реализация для
    python: https://pypi.org/project/pytest-golden/ , но знать об этом подходе
    крайне полезно.

    Принцип работы следующий: во внешних файлах специфицируются входные и
    выходные данные для теста. При запуске тестов происходит сравнение и если
    выход изменился -- выводится ошибка.

    Если вы меняете логику работы приложения -- то запускаете тесты с ключом:
    `cd python && poetry run pytest . -v --update-goldens`

    Это обновит файлы конфигурации, и вы можете закоммитить изменения в
    репозиторий, если они корректные.

    Формат файла описания теста -- YAML. Поля определяются доступом из теста к
    аргументу `golden` (`golden[key]` -- входные данные, `golden.out("key")` --
    выходные данные).

    Вход:

    - `in_source` -- исходный код
    - `in_stdin` -- данные на ввод процессора для симуляции

    Выход:

    - `out_hex` -- аннотированный машинный код
    - `out_binary` -- бинарный файл в base64
    - `out_stdout` -- стандартный вывод транслятора и симулятора
    - `out_log` -- журнал программы
    """
    # Установим уровень отладочного вывода на DEBUG
    caplog.set_level(logging.DEBUG)

    # Создаём временную папку для тестирования приложения.
    with tempfile.TemporaryDirectory() as tmpdirname:
        # Готовим имена файлов для входных и выходных данных.
        source = os.path.join(tmpdirname, "prog")
        input_stream = os.path.join(tmpdirname, "input.txt")
        target_data = os.path.join(tmpdirname, "prog.dmem")
        target_code = os.path.join(tmpdirname, "prog.imem")
        target_labels = os.path.join(tmpdirname, "prog.labels")
        target_code_hex = os.path.join(tmpdirname, "prog.lst")

        # Записываем входные данные в файлы. Данные берутся из теста.
        with open(source, "w", encoding="utf-8") as file:
            file.write(golden["in_source"])
        with open(input_stream, "w", encoding="utf-8") as file:
            file.write(golden["in_stdin"])

        # Запускаем транслятор и собираем весь стандартный вывод в переменную
        # stdout
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            compiler.run(source, tmpdirname)
            print("============================================================")
            emulator.run(target_code, target_data, target_labels, input_stream)

        # Выходные данные также считываем в переменные.
        with open(target_code, "rb") as file:
            code = file.read()
        with open(target_data, "rb") as file:
            data = file.read()
        with open(target_code_hex, encoding="utf-8") as file:
            code_hex = file.read()

        # Проверяем, что ожидания соответствуют реальности.
        assert code == golden.out["out_code"]
        assert data == golden.out["out_data"]
        assert code_hex == golden.out["out_code_hex"]
        out = stdout.getvalue().replace(tmpdirname, "<tmpdir>")
        assert out == golden.out["out_stdout"]
        print("out_log text:")
        print(caplog.text)
        assert caplog.text == golden.out["out_log"]
