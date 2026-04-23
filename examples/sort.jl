// sort.jl
// Читает N чисел, сортирует пузырьком, выводит с суммой и средним.

fn write_char_fn(c: int) : void {
    write_char(c);
}

fn write_int(x: int) : void {
    var neg: int = 0;
    if (x < 0) {
        write_char(45);
        x = 0 - x;
    }
    if (x == 0) {
        write_char(48);
        return;
    }
    if (x >= 10) {
        write_int(x / 10);
    }
    write_char(48 + x % 10);
}

fn read_int() : int {
    var c: int = 0;
    var result: int = 0;
    var neg: int = 0;

    c = read_char();
    while (c == 32 || c == 10 || c == 13) {
        c = read_char();
    }

    if (c == 45) {
        neg = 1;
        c = read_char();
    }

    while (c >= 48 && c <= 57) {
        result = result * 10 + (c - 48);
        c = read_char();
    }

    if (neg == 1) {
        result = 0 - result;
    }
    return result;
}

fn print_str(s: str) : void {
    var i: int = 0;
    var len: int = str_len(s);
    while (i < len) {
        write_char(str_get(s, i));
        i = i + 1;
    }
}

fn bubble_sort(arr: str, n: int) : void {
    var i: int = 0;
    var j: int = 0;
    var tmp: int = 0;
    var a: int = 0;
    var b: int = 0;
    while (i < n - 1) {
        j = 0;
        while (j < n - 1 - i) {
            a = str_get(arr, j);
            b = str_get(arr, j + 1);
            if (a > b) {
                tmp = a;
                str_set(arr, j, b);
                str_set(arr, j + 1, tmp);
            }
            j = j + 1;
        }
        i = i + 1;
    }
}

fn main() : void {
    var arr: str;
    var n: int = 0;
    var i: int = 0;
    var sum: int = 0;
    var avg: int = 0;

    n = read_int();

    i = 0;
    while (i < n) {
        str_set(arr, i, read_int());
        i = i + 1;
    }
    str_set_len(arr, n);

    bubble_sort(arr, n);

    sum = 0;
    i = 0;
    while (i < n) {
        sum = sum + str_get(arr, i);
        i = i + 1;
    }

    if (n > 0) {
        avg = sum / n;
    }

    print_str("Sorted: ");
    i = 0;
    while (i < n) {
        write_int(str_get(arr, i));
        if (i < n - 1) {
            write_char(32);
        }
        i = i + 1;
    }
    write_char(10);

    print_str("Sum: ");
    write_int(sum);
    write_char(10);

    print_str("Avg: ");
    write_int(avg);
    write_char(10);
}