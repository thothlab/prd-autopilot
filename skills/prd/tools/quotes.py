#!/usr/bin/env python3
"""Сверка цитат диагноста с логами, из которых он их взял.

    python3 "$skillDir/tools/quotes.py" <файл-с-ответом-диагноста>
    ... | python3 "$skillDir/tools/quotes.py" -

Цитата - строка ответа вида `<путь>:<номер строки>: <текст>`. Она сверена,
если файл есть, строка с таким номером в нём есть и текст цитаты целиком
входит в эту строку (пробелы по краям и ANSI-цвета не считаются; длинную
строку диагност вправе обрезать, но не короче 12 знаков).

Печатает одну строку итога. Код выхода 0 - сверены все и их больше нуля,
1 - иначе: такому ответу не верят.
"""

import io
import re
import sys

QUOTE = re.compile(r"^\s*(?:[-*>]\s*)?`?(?P<path>/[^:`\s][^:`]*):(?P<line>\d+):\s?(?P<text>.*?)`?\s*$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clean(s):
    return ANSI.sub("", s).strip()


def check(path, n, text):
    try:
        lines = io.open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return "нет файла"
    if not 1 <= n <= len(lines):
        return f"в файле {len(lines)} строк"
    want, have = clean(text), clean(lines[n - 1])
    if len(want) < min(12, len(have)):
        return "цитата короче 12 знаков"
    return None if want and want in have else "текст не совпал"


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    src = sys.stdin if argv[1] == "-" else io.open(argv[1], encoding="utf-8", errors="replace")
    total, bad = 0, []
    for raw in src.read().splitlines():
        m = QUOTE.match(raw)
        if not m:
            continue
        total += 1
        why = check(m["path"], int(m["line"]), m["text"])
        if why:
            bad.append(f"  {m['path']}:{m['line']} - {why}")
    if not total:
        print("цитаты: ни одной в формате <путь>:<строка>: <текст> - ответ не сверить")
        sys.exit(1)
    print(f"цитаты: сверено {total - len(bad)} из {total}")
    if bad:
        print("\n".join(bad))
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
