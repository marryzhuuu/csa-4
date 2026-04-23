// cat.jl
// Читает строку и выводит ее.

fn print_str(s: str) : void {
    var i: int = 0;
    var len: int = str_len(s);
    while (i < len) {
        write_char(str_get(s, i));
        i = i + 1;
    }
}

fn read_line(buf: str, maxlen: int) : int {
    var i: int = 0;
    var c: int = 0;
    while (i < maxlen) {
        c = read_char();
        if (c == -1) {
            break;
        }
        if (c == 10) {
            break;
        }
        str_set(buf, i, c);
        i = i + 1;
    }
    str_set_len(buf, i);
    return i;
}

fn main() : void {
    var buf: str;
    var n: int = 0;

    read_line(buf, 64);
    print_str(buf);
}