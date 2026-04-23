// sort.jll
// Читает список целых чисел (Length-prefixed, как тип строки),
// сортирует пузырьком и выводит результат.
// Дополнительно вычисляет сумму и среднее.

fn read_int() : int {
    // Читает одно целое число из потока токенов (символов '0'-'9').
    // Токены-разделители (пробел, \n) пропускаются.
    var c: int = 0;
    var result: int = 0;
    var negative: bool = false;

    // Пропустить пробелы
    c = read_char();
    while (c == 32 || c == 10 || c == 13) {
        c = read_char();
    }

    if (c == 45) {          // '-'
        negative = true;
        c = read_char();
    }

    while (c >= 48 && c <= 57) {   // '0'..'9'
        result = result * 10 + (c - 48);
        c = read_char();
    }

    if (negative) {
        result = 0 - result;
    }
    return result;
}

fn write_int(x: int) : void {
    if (x < 0) {
        write_char(45);      // '-'
        x = 0 - x;
    }
    if (x == 0) {
        write_char(48);      // '0'
        return;
    }
    // Рекурсивная запись цифр (старший разряд первым)
    if (x >= 10) {
        write_int(x / 10);
    }
    write_char(48 + (x % 10));
}

fn swap(arr: str, i: int, j: int) : void {
    // arr хранит int-значения как символы (каждый элемент — одно слово)
    var tmp: int = str_get(arr, i);
    str_set(arr, i, str_get(arr, j));
    str_set(arr, j, tmp);
}

fn bubble_sort(arr: str, n: int) : void {
    var i: int = 0;
    var j: int = 0;
    while (i < n - 1) {
        j = 0;
        while (j < n - 1 - i) {
            if (str_get(arr, j) > str_get(arr, j + 1)) {
                swap(arr, j, j + 1);
            }
            j = j + 1;
        }
        i = i + 1;
    }
}

fn main() : void {
    // Формат ввода: первое число — количество элементов N,
    // затем N чисел (Pascal-list).
    var arr: str;
    var n: int = read_int();

    var i: int = 0;
    while (i < n) {
        str_set(arr, i, read_int());
        i = i + 1;
    }
    str_set_len(arr, n);

    bubble_sort(arr, n);

    // Вычислить сумму и среднее (математика)
    var sum: int = 0;
    i = 0;
    while (i < n) {
        sum = sum + str_get(arr, i);
        i = i + 1;
    }
    var avg: int = 0;
    if (n > 0) {
        avg = sum / n;
    }

    // Вывод отсортированного списка
    print_str("Sorted: ");
    i = 0;
    while (i < n) {
        write_int(str_get(arr, i));
        if (i < n - 1) {
            write_char(32);   // пробел
        }
        i = i + 1;
    }
    write_char(10);           // '\n'

    // Вывод статистики
    print_str("Sum: ");
    write_int(sum);
    write_char(10);
    print_str("Avg: ");
    write_int(avg);
    write_char(10);
}