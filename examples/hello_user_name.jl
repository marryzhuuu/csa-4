// hello_user_name.jll
// Запрашивает имя пользователя и приветствует его.
// Использует Pascal-строки (Length-prefixed).

// Стандартные IO-функции (реализованы как встроенные,
// отображённые на memory-mapped порты ввода-вывода).
//   read_char()  : int  -- читает один символ из порта ввода
//   write_char(c : int) -- пишет один символ в порт вывода
//   write_str(s  : str) -- пишет Pascal-строку посимвольно

fn print_str(s: str) : void {
    var i: int = 0;
    var len: int = str_len(s);   // встроенная: читает слово-длину
    while (i < len) {
        write_char(str_get(s, i)); // встроенная: читает i-й символ
        i = i + 1;
    }
}

fn read_line(buf: str, max: int) : int {
    // Читает символы до '\n' или конца ввода.
    // Возвращает количество прочитанных символов.
    var i: int = 0;
    var c: int = 0;
    while (i < max) {
        c = read_char();
        if (c == -1) {    // конец ввода
            break;
        }
        if (c == 10) {    // '\n'
            break;
        }
        str_set(buf, i, c);  // встроенная: запись i-го символа
        i = i + 1;
    }
    str_set_len(buf, i);     // встроенная: устанавливает слово-длину
    return i;
}

fn main() : void {
    var name_buf: str;     // Буфер — Pascal-строка в секции данных
    var greeting: str;

    // Вывести приглашение
    print_str("What is your name?\n");

    // Читаем имя
    var n: int = read_line(name_buf, 64);

    if (n == 0) {
        print_str("Hello, stranger!\n");
    } else {
        print_str("Hello, ");
        print_str(name_buf);
        print_str("!\n");
    }
}