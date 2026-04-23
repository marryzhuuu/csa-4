// hello_world.jl
// Выводит приветствие.

fn print_str(s: str) : void {
    var i: int = 0;
    var len: int = str_len(s);
    while (i < len) {
        write_char(str_get(s, i));
        i = i + 1;
    }
}

fn main() : void {
    print_str("Hello, world!\n");
}