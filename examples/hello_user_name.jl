// hello_user_name.jl
// Запрашивает имя пользователя, выводит приветствие.

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
    var name_buf: str;
    var n: int = 0;

    print_str("What is your name?\n");

    n = read_line(name_buf, 64);

    if (n == 0) {
        print_str("Hello, stranger!\n");
    } else {
        print_str("Hello, ");
        print_str(name_buf);
        print_str("!\n");
    }
}