# Лабораторная №4. Эксперимент.

- Жукова Мария Владимировна, s381731
- Вариант: `alg | cisc | harv | mc | tick | binary | stream | mem | pstr | prob2 | pipeline`

## Язык программирования
### Синтаксис

Расширенная форма Бэкуса-Наура:

``` ebnf
<program>     ::= { <decl> }

<decl>        ::= <var_decl>
               | <fn_decl>

(* Объявления *)
<var_decl>    ::= "var" <ident> [":" <type>] ["=" <expr>] ";"
               | "const" <ident> [":" <type>] "=" <expr> ";"

<fn_decl>     ::= "fn" <ident> "(" [<param_list>] ")" [":" <type>] <block>

<param_list>  ::= <param> { "," <param> }
<param>       ::= <ident> ":" <type>

<type>        ::= "int" | "bool" | "str" | "void"
               | <ident>            (* пользовательский тип *)

(* Блок и операторы *)
<block>       ::= "{" { <stmt> } "}"

<stmt>        ::= <var_decl>
               | <assign_stmt>
               | <if_stmt>
               | <while_stmt>
               | <for_stmt>
               | <return_stmt>
               | <break_stmt>
               | <continue_stmt>
               | <expr_stmt>
               | <block>

<assign_stmt> ::= <ident> ["[" <expr> "]"] "=" <expr> ";"

<if_stmt>     ::= "if" "(" <expr> ")" <block> [ "else" (<if_stmt> | <block>) ]

<while_stmt>  ::= "while" "(" <expr> ")" <block>

<for_stmt>    ::= "for" "(" (<var_decl> | <assign_stmt> | ";")
                            <expr> ";"
                            (<assign_stmt_no_semi> | <expr>) ")" <block>
(* assign_stmt_no_semi — присвоение без финальной ";" для заголовка for *)

<return_stmt> ::= "return" [<expr>] ";"
<break_stmt>  ::= "break" ";"
<continue_stmt>::= "continue" ";"

<expr_stmt>   ::= <expr> ";"

(* Выражения по возрастанию приоритета *)
<expr>        ::= <assign_expr>

<assign_expr> ::= <or_expr>                              (* rvalue *)
               | <ident> "=" <assign_expr>               (* l = r  *)

<or_expr>     ::= <and_expr> { "||" <and_expr> }
<and_expr>    ::= <eq_expr>  { "&&" <eq_expr>  }
<eq_expr>     ::= <rel_expr> { ("==" | "!=") <rel_expr> }
<rel_expr>    ::= <add_expr> { ("<" | "<=" | ">" | ">=") <add_expr> }
<add_expr>    ::= <mul_expr> { ("+" | "-") <mul_expr> }
<mul_expr>    ::= <unary_expr> { ("*" | "/" | "%") <unary_expr> }
<unary_expr>  ::= ("!" | "-") <unary_expr>
               | <postfix_expr>

<postfix_expr>::= <primary_expr>
               | <postfix_expr> "[" <expr> "]"           (* индексирование *)
               | <postfix_expr> "(" [<arg_list>] ")"     (* вызов функции *)

<primary_expr>::= <literal>
               | <ident>
               | "(" <expr> ")"
               | <string_lit>

<arg_list>    ::= <expr> { "," <expr> }

(* Литералы *)
<literal>     ::= <int_lit> | <bool_lit>
<int_lit>     ::= ["-"] DIGIT { DIGIT }
<bool_lit>    ::= "true" | "false"
<string_lit>  ::= '"' { <str_char> } '"'
<str_char>    ::= любой символ кроме '"' и '\n' | escape_sequence
<escape_sequence> ::= "\" ("n" | "t" | "\" | '"')

<ident>       ::= LETTER { LETTER | DIGIT | "_" }

LETTER        ::= [a-z] | [A-Z] | "_"
DIGIT         ::= [0-9]
```