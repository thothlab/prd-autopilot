#!/usr/bin/env python3
"""Замер стоимости прогона по транскриптам Claude Code.

Читает `~/.claude/projects/<проект>/<сессия>.jsonl` и каталог `<сессия>/subagents/`
рядом с ним. Ничего не пишет и никуда не ходит: только стандартная библиотека.

    python3 tools/prd_cost.py cost    <сессия> [...]   деньги: оркестратор против исполнителей
    python3 tools/prd_cost.py ceiling <сессия> [...]   держится ли потолок вызовов
    python3 tools/prd_cost.py curve   <сессия> [...]   как цена растёт с длиной задачи
    python3 tools/prd_cost.py compose <сессия>         чем набит контекст
    python3 tools/prd_cost.py agent   <файл.jsonl>     профиль одного исполнителя
    python3 tools/prd_cost.py find    [подстрока]      найти сессии по маркеру в тексте

Сессия задаётся путём к `.jsonl`, либо парой `<каталог-проекта>/<id-сессии>` —
искать будет от `~/.claude/projects`.

Деньги считаются по списочным ценам API (таблица PRICE ниже), а не по счёту
подписки: это мера того, во что прогон обходится по объёму, и она сопоставима
между прогонами. Кэш дороже и дешевле обычного входа в разы, поэтому входные
токены приводятся к «эквиваленту базовой цены»: чтение x0.1, запись 5m x1.25,
запись 1h x2. Цены меняются — сверяйтесь с прайсом перед тем, как цитировать
абсолютные числа; отношения между прогонами устойчивее абсолютных сумм.
"""

import collections
import io
import json
import os
import statistics
import sys

ROOT = os.path.expanduser("~/.claude/projects")

# $ за миллион токенов: (вход, выход). Модель, которой здесь нет, считается по Opus.
PRICE = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
DEFAULT_PRICE = (5.0, 25.0)


# ---------- разбор транскрипта ----------

def resolve(arg):
    """Путь к .jsonl сессии из пути или из пары <проект>/<id>."""
    for cand in (arg, arg + ".jsonl", os.path.join(ROOT, arg), os.path.join(ROOT, arg + ".jsonl")):
        if os.path.isfile(cand):
            return cand
    sys.exit(f"не нашёл транскрипт: {arg}")


def subagents(main_path):
    d = os.path.join(main_path[:-6], "subagents")
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".jsonl")]


def records(path):
    """Записи ассистента с непустым usage — каждая это один оплаченный запрос."""
    for line in io.open(path, encoding="utf-8", errors="replace"):
        if '"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        if msg.get("usage"):
            yield msg


def blocks(msg):
    c = (msg or {}).get("content")
    return c if isinstance(c, list) else []


class Stats:
    """Счётчики одного контекста: денег, запросов, вызовов, размера контекста."""

    def __init__(self):
        self.usd = 0.0
        self.equiv = 0.0      # вход в эквиваленте базовой цены
        self.out = 0
        self.req = 0
        self.calls = 0
        self.ctx = []

    def add(self, msg):
        u = msg["usage"]
        pin, pout = PRICE.get(msg.get("model"), DEFAULT_PRICE)
        cc = u.get("cache_creation") or {}
        equiv = (u.get("input_tokens", 0)
                 + u.get("cache_read_input_tokens", 0) * 0.1
                 + cc.get("ephemeral_1h_input_tokens", 0) * 2.0
                 + cc.get("ephemeral_5m_input_tokens", 0) * 1.25)
        self.equiv += equiv
        self.out += u.get("output_tokens", 0)
        self.usd += equiv / 1e6 * pin + u.get("output_tokens", 0) / 1e6 * pout
        self.req += 1
        self.ctx.append(u.get("cache_read_input_tokens", 0)
                        + u.get("cache_creation_input_tokens", 0)
                        + u.get("input_tokens", 0))
        self.calls += sum(1 for b in blocks(msg) if b.get("type") == "tool_use")

    @property
    def avg_ctx(self):
        return sum(self.ctx) / len(self.ctx) if self.ctx else 0

    @property
    def peak_ctx(self):
        return max(self.ctx) if self.ctx else 0


def scan(path):
    s = Stats()
    for msg in records(path):
        s.add(msg)
    return s


# ---------- команды ----------

def cmd_cost(args):
    """Деньги по сессии: сколько стоил оркестратор и сколько исполнители."""
    grand = 0.0
    for arg in args:
        main_path = resolve(arg)
        main = scan(main_path)
        subs = [(os.path.basename(p), scan(p)) for p in subagents(main_path)]
        sub_usd = sum(s.usd for _, s in subs)
        sub_req = sum(s.req for _, s in subs)
        sub_ctx = sum(sum(s.ctx) for _, s in subs)
        total = main.usd + sub_usd
        grand += total
        print("=" * 74)
        print(f"{os.path.basename(main_path)}   исполнителей {len(subs)}")
        print(f"  оркестратор: ${main.usd:8.0f}  {main.req:5d} запросов, "
              f"средний контекст {main.avg_ctx / 1000:.0f}k, пик {main.peak_ctx / 1000:.0f}k")
        print(f"  исполнители: ${sub_usd:8.0f}  {sub_req:5d} запросов, "
              f"средний контекст {sub_ctx / max(sub_req, 1) / 1000:.0f}k")
        share = sub_usd / total * 100 if total else 0
        print(f"  ИТОГО:       ${total:8.0f}  доля исполнителей {share:.0f}%")
        print(f"  вход-эквивалент {(main.equiv + sum(s.equiv for _, s in subs)) / 1e6:.0f}M, "
              f"выход {(main.out + sum(s.out for _, s in subs)) / 1e6:.2f}M")
        if subs:
            top = sorted(subs, key=lambda kv: -kv[1].usd)[:5]
            print("  самые дорогие исполнители:")
            for name, s in top:
                print(f"     ${s.usd:6.0f}  {s.calls:4d} вызовов, {s.req:4d} запросов, "
                      f"пик {s.peak_ctx / 1000:4.0f}k  {name}")
    if len(args) > 1:
        print("=" * 74)
        print(f"по всем сессиям: ${grand:.0f}")


def cmd_ceiling(args, limit=50):
    """Держится ли потолок вызовов и сколько стоят те, кто за него вышел."""
    for arg in args:
        main_path = resolve(arg)
        subs = [scan(p) for p in subagents(main_path)]
        subs = [s for s in subs if s.req > 3]
        if not subs:
            print(f"{os.path.basename(main_path)}: исполнителей нет")
            continue
        over = [s for s in subs if s.calls > limit]
        total = sum(s.usd for s in subs)
        calls = sorted(s.calls for s in subs)
        print("=" * 74)
        print(f"{os.path.basename(main_path)}: исполнителей {len(subs)}, "
              f"за потолком в {limit} вызовов — {len(over)} ({len(over) * 100 // len(subs)}%)")
        print(f"  вызовов: медиана {statistics.median(calls):.0f}, максимум {max(calls)}")
        print(f"  на вышедших за потолок ${sum(s.usd for s in over):.0f} из ${total:.0f}")


def cmd_curve(args, edges=(50, 100, 200)):
    """Как цена задачи растёт с числом вызовов — эмпирическая кривая."""
    rows = []
    for arg in args:
        for p in subagents(resolve(arg)):
            s = scan(p)
            if s.req > 3:
                rows.append(s)
    if not rows:
        sys.exit("исполнителей не нашлось")
    lo = 0
    buckets = []
    for e in edges:
        buckets.append((f"{lo + 1}-{e}", lo + 1, e))
        lo = e
    buckets.append((f"больше {lo}", lo + 1, 10 ** 9))
    print(f"исполнителей в выборке: {len(rows)}")
    print(f"{'вызовов':<12}{'шт':>4}{'медиана $':>11}{'$/вызов':>10}{'пик':>9}{'запросов':>10}")
    for label, a, b in buckets:
        part = [s for s in rows if a <= s.calls <= b]
        if not part:
            continue
        print(f"{label:<12}{len(part):>4}"
              f"{statistics.median(s.usd for s in part):>11.1f}"
              f"{statistics.median(s.usd / max(s.calls, 1) for s in part):>10.2f}"
              f"{statistics.median(s.peak_ctx for s in part) / 1000:>8.0f}k"
              f"{statistics.median(s.req for s in part):>10.0f}")


def cmd_compose(args):
    """Чем набит контекст: результаты инструментов, аргументы вызовов, проза."""
    for arg in args:
        main_path = resolve(arg)
        for label, paths in (("ОРКЕСТРАТОР", [main_path]), ("ИСПОЛНИТЕЛИ", subagents(main_path))):
            if not paths:
                continue
            cat = collections.Counter()
            tool_in = collections.Counter()
            req = no_tool = 0
            for path in paths:
                for line in io.open(path, encoding="utf-8", errors="replace"):
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    t = d.get("type")
                    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                    if t == "assistant" and (msg.get("usage") or {}):
                        req += 1
                        has = False
                        for b in blocks(msg):
                            if b.get("type") == "text":
                                cat["проза ассистента"] += len(b.get("text", ""))
                            elif b.get("type") == "tool_use":
                                has = True
                                n = len(json.dumps(b.get("input") or {}, ensure_ascii=False))
                                cat["аргументы вызовов"] += n
                                tool_in[b.get("name", "?")] += n
                        no_tool += 0 if has else 1
                    elif t == "user":
                        for b in blocks(msg):
                            if b.get("type") == "tool_result":
                                c = b.get("content")
                                cat["результаты инструментов"] += len(
                                    c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
                            elif b.get("type") == "text":
                                cat["реплики и напоминания"] += len(b.get("text", ""))
            total = sum(cat.values()) or 1
            print("=" * 74)
            print(f"{label}: запросов {req}, из них без вызова инструмента "
                  f"{no_tool} ({no_tool * 100 // max(req, 1)}%)")
            print(f"материал транскрипта: {total / 1048576:.2f} МБ символов")
            for k, v in cat.most_common():
                print(f"   {v / 1048576:6.2f} МБ  {v * 100 // total:3d}%  {k}")
            if tool_in:
                print("   в т.ч. аргументы по инструментам:",
                      [(k, f"{v / 1048576:.2f}МБ") for k, v in tool_in.most_common(4)])


def cmd_agent(args):
    """Профиль одного исполнителя: с чего начал контекст и чем он вырос."""
    for arg in args:
        path = resolve(arg)
        s = Stats()
        tools = collections.Counter()
        results = []
        pending = {}
        for msg in records(path):
            s.add(msg)
            for b in blocks(msg):
                if b.get("type") == "tool_use":
                    name = b.get("name", "?")
                    tools[name] += 1
                    inp = b.get("input") or {}
                    sig = (" ".join(str(inp.get("command", "")).split())[:64] if name == "Bash"
                           else str(inp.get("file_path") or json.dumps(inp, ensure_ascii=False))[:64])
                    pending[b.get("id")] = (name, sig)
        for line in io.open(path, encoding="utf-8", errors="replace"):
            if '"tool_result"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            for b in blocks(d.get("message") if isinstance(d.get("message"), dict) else {}):
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                    name, sig = pending.pop(b.get("tool_use_id"), ("?", ""))
                    results.append((len(text), name, sig))
        print("=" * 74)
        print(os.path.basename(path))
        per_call = s.req / max(s.calls, 1)
        print(f"  ${s.usd:.0f}  запросов {s.req}, вызовов {s.calls} "
              f"({per_call:.1f} запроса на вызов)")
        if s.ctx:
            q = [s.ctx[int(len(s.ctx) * f)] for f in (0.25, 0.5, 0.75)]
            print(f"  контекст: старт {s.ctx[0] / 1000:.0f}k -> "
                  + " -> ".join(f"{x / 1000:.0f}k" for x in q)
                  + f" -> финал {s.ctx[-1] / 1000:.0f}k, средний {s.avg_ctx / 1000:.0f}k")
        print(f"  инструменты: {tools.most_common(6)}")
        results.sort(reverse=True)
        print("  крупнейшие результаты:")
        for size, name, sig in results[:6]:
            print(f"     {size // 1024:5d} КБ  {name:6s} {sig}")


def cmd_find(args):
    """Сессии, где встречается маркер: по умолчанию следы прогона prd."""
    needle = args[0] if args else "prd_0"
    hits = []
    for project in sorted(os.listdir(ROOT)):
        pdir = os.path.join(ROOT, project)
        if not os.path.isdir(pdir):
            continue
        for f in sorted(os.listdir(pdir)):
            if not f.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, f)
            n = 0
            with io.open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if needle in line:
                        n += 1
                        if n > 20:
                            break
            if n > 3:
                subs = len(subagents(path))
                hits.append((n, subs, os.path.getsize(path), f"{project}/{f[:-6]}"))
    hits.sort(reverse=True)
    print(f"сессии со следами «{needle}»:")
    for n, subs, size, name in hits[:15]:
        print(f"  {size // 1048576:4d} МБ  исполнителей {subs:3d}  {name}")


COMMANDS = {
    "cost": cmd_cost,
    "ceiling": cmd_ceiling,
    "curve": cmd_curve,
    "compose": cmd_compose,
    "agent": cmd_agent,
    "find": cmd_find,
}


def main(argv):
    if len(argv) < 2 or argv[1] not in COMMANDS:
        sys.exit(__doc__)
    cmd = COMMANDS[argv[1]]
    args = argv[2:]
    if not args and cmd is not cmd_find:
        sys.exit(f"нужен аргумент: {argv[1]} <сессия>")
    cmd(args)


if __name__ == "__main__":
    main(sys.argv)
