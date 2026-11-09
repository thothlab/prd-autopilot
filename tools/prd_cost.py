#!/usr/bin/env python3
"""Замер стоимости прогона по транскриптам Claude Code.

Читает `~/.claude/projects/<проект>/<сессия>.jsonl` и каталог `<сессия>/subagents/`
рядом с ним. Ничего не пишет и никуда не ходит: только стандартная библиотека.

    python3 tools/prd_cost.py run     <run/prd_XX> [сессия ...]   прогон по ролям и задачам
    python3 tools/prd_cost.py cost    <сессия> [...]   деньги: оркестратор против исполнителей
    python3 tools/prd_cost.py ceiling <сессия> [...]   держится ли потолок вызовов
    python3 tools/prd_cost.py curve   <сессия> [...]   как цена растёт с длиной задачи
    python3 tools/prd_cost.py compose <сессия>         чем набит контекст
    python3 tools/prd_cost.py agent   <файл.jsonl>     профиль одного исполнителя
    python3 tools/prd_cost.py find    [подстрока]      найти сессии по маркеру в тексте

Сессия задаётся путём к `.jsonl`, парой `<каталог-проекта>/<id-сессии>` или
одним id — искать будет от `~/.claude/projects`.

`run` берёт сессии из `usage.js` прогона, окно — из `state.js` (от `startedAt`
до `finishedAt`, у незакрытого прогона до `updatedAt`), роль субагента — из
`description` в `subagents/*.meta.json`. Он не зависит от того, когда оркестратор
последний раз запускал `usage.py`, поэтому сверяет и устаревшую карточку дашборда.

Деньги считаются так же, как карточку считает `skills/prd/tools/usage.py`:
по списочным ценам API, а не по счёту подписки, с вызовами советника из
`usage.iterations` и двойной ценой быстрого режима. `run` берёт снимок прайса
из `usage.js` прогона, остальные команды — встроенную таблицу `usage.py`.
Вход ещё приводится к «эквиваленту базовой цены»: сколько токенов обычного
входа стоили бы столько же, сколько прочитанное с чтением и записью кэша.
Цены меняются — сверяйтесь с прайсом перед тем, как цитировать абсолютные
числа; отношения между прогонами устойчивее абсолютных сумм.
"""

import collections
import glob
import io
import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "prd", "tools"))
import usage  # noqa: E402  одни цены и один разбор usage с карточкой дашборда

ROOT = usage.ROOT
PRICES = usage.builtin_prices()

# роль субагента по description из meta.json: первое совпадение сверху
ROLES = (
    ("повторное ревью", r"re-?review|повторн\w* ревью|^(review|ревью|verify).*(repair|починк|fix)"),
    ("ревью", r"^(prd\s*\d+\s*)?(review|ревью)|финальн\w* ревью|craft|manifest|манифест"),
    ("исполнители", r"^(prd\s*\d+\s*)?(task|задач|исполн)|^(continue|execute|fresh fix)\b.*task"),
    ("гейты", r"blind|coverage|acceptance|приёмк|покрыти|\bg[24]\b"),
    ("исследование", r"research|codebase map|^map |analog|ресёрч|исслед"),
    ("журнал и память", r"journal|правки|memory|памят"),
)


# ---------- разбор транскрипта ----------

def locate(arg):
    """Путь к .jsonl сессии из пути, пары <проект>/<id> или одного id; не нашёл — None."""
    for cand in (arg, arg + ".jsonl", os.path.join(ROOT, arg), os.path.join(ROOT, arg + ".jsonl")):
        if os.path.isfile(cand):
            return cand
    hits = glob.glob(os.path.join(ROOT, "*", arg + ".jsonl"))
    return hits[0] if hits else None


def resolve(arg):
    path = locate(arg)
    if not path:
        sys.exit(f"не нашёл транскрипт: {arg}")
    return path


def subagents(main_path):
    d = os.path.join(main_path[:-6], "subagents")
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".jsonl")]


def records(path, lo=None, hi=None):
    """Оплаченные запросы с их временем: записи ассистента с непустым usage, по одной на message.id.

    Ответ пишется в транскрипт строкой на каждый блок, и у всех строк один и тот же
    usage — без дедупа счёт выходит в 2-3 раза больше настоящего. Блоки контента
    собираются со всех строк ответа: по одной первой строке вызовов видно вчетверо меньше."""
    merged = {}
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
        if not msg.get("usage") or msg.get("model") == "<synthetic>":
            continue
        key = msg.get("id")
        if key in merged:
            if merged[key]:
                merged[key][0]["content"] = blocks(merged[key][0]) + blocks(msg)
            continue
        t = usage.parse_ts(d.get("timestamp"))
        if lo is not None and (t is None or t < lo or (hi is not None and t > hi)):
            merged[key] = None
            continue
        merged[key] = (dict(msg), t)
    return [r for r in merged.values() if r]


def blocks(msg):
    c = (msg or {}).get("content")
    return c if isinstance(c, list) else []


def price_part(part, model, fast, prices):
    """$, вход в эквиваленте базовой цены и выход одной части запроса."""
    b = usage.Bucket()
    b.add(part, model, fast, prices)
    base = re.sub(r"\[.*\]$", "", model or "")
    rates = prices["models"].get(base) or prices["models"].get(usage.FALLBACK) \
        or usage.rates5(*usage.BUILTIN[usage.FALLBACK])
    pin, pout = rates[0], rates[4]
    k = 2.0 if fast else 1.0
    return b.usd, (b.usd / k - b.output * pout / 1e6) * 1e6 / pin, b.output


class Stats:
    """Счётчики одного контекста: денег, запросов, вызовов, размера контекста."""

    def __init__(self, prices=None):
        self.prices = prices or PRICES
        self.usd = 0.0
        self.advisor = 0.0    # $ вызовов советника, уже внутри usd
        self.advisor_calls = 0
        self.equiv = 0.0      # вход в эквиваленте базовой цены
        self.out = 0
        self.req = 0
        self.calls = 0
        self.ctx = []

    def add(self, msg):
        """Учесть запрос; вернуть его $ и долю советника в них."""
        u = msg["usage"]
        fast = u.get("speed") == "fast"
        usd = adv = 0.0
        ctx = 0
        # вызовы советника лежат в iterations со своей моделью, верхний usage их не включает;
        # зато он складывает вход частей до и после советника, и размер контекста по нему вдвое больше
        for part in u.get("iterations") or [u]:
            p_usd, equiv, out = price_part(part, part.get("model") or msg.get("model"), fast, self.prices)
            usd += p_usd
            self.equiv += equiv
            self.out += out
            if part.get("type") == "advisor_message":
                adv += p_usd
                self.advisor_calls += 1
            else:
                ctx = max(ctx, part.get("cache_read_input_tokens", 0)
                          + part.get("cache_creation_input_tokens", 0)
                          + part.get("input_tokens", 0))
        self.usd += usd
        self.advisor += adv
        self.req += 1
        self.ctx.append(ctx)
        self.calls += sum(1 for b in blocks(msg) if b.get("type") == "tool_use")
        return usd, adv

    @property
    def avg_ctx(self):
        return sum(self.ctx) / len(self.ctx) if self.ctx else 0

    @property
    def peak_ctx(self):
        return max(self.ctx) if self.ctx else 0


def scan(path):
    s = Stats()
    for msg, _ in records(path):
        s.add(msg)
    return s


def role_of(desc):
    d = desc.lower().strip()
    return next((name for name, rx in ROLES if re.search(rx, d)), "прочее")


def task_of(desc):
    m = re.search(r"\d+", re.sub(r"(?i)prd\s*\d+|\bg[24]\b", "", desc))
    return m.group(0).zfill(2) if m else None


def busy_hours(stamps, gap=1800):
    """Сумма промежутков между запросами не длиннее получаса: ночи и ожидание не считаются."""
    stamps = sorted(stamps)
    return sum(b - a for a, b in zip(stamps, stamps[1:]) if b - a <= gap) / 3600


# ---------- команды ----------

def cmd_run(args):
    """Прогон целиком: деньги и время по ролям и по задачам, советник отдельно."""
    run = args[0].rstrip("/")
    state = usage.read_js(os.path.join(run, "state.js"), "STATE")
    if not state or usage.parse_ts(state.get("startedAt")) is None:
        sys.exit(f"не прочитан {run}/state.js или в нём нет startedAt")
    prev = usage.read_js(os.path.join(run, "usage.js"), "USAGE") or {}
    prices = prev.get("prices") or PRICES
    sessions = list(dict.fromkeys(list(prev.get("sessions") or []) + args[1:]))
    if not sessions:
        sys.exit("в usage.js нет сессий: назови их после каталога прогона")
    lo = usage.parse_ts(state.get("startedAt"))
    hi = usage.parse_ts(state.get("finishedAt") or state.get("updatedAt"))

    roles = collections.defaultdict(lambda: Stats(prices))
    agents = collections.Counter()
    per_task = collections.defaultdict(lambda: collections.defaultdict(float))
    models = collections.defaultdict(collections.Counter)
    others, missing, stamps = collections.Counter(), [], []
    for sid in sessions:
        main_path = locate(sid)
        if not main_path:
            missing.append(sid)
            continue
        files = [("оркестратор", None, main_path, "")]
        for p in subagents(main_path):
            meta = p[:-6] + ".meta.json"
            desc = json.load(io.open(meta, encoding="utf-8")).get("description", "") if os.path.exists(meta) else ""
            files.append((role_of(desc), task_of(desc), p, desc))
        for role, task, p, desc in files:
            n = 0
            for msg, t in records(p, lo, hi):
                usd, adv = roles[role].add(msg)
                stamps.append(t)
                n += 1
                if task is None or role not in ("исполнители", "ревью", "повторное ревью"):
                    continue
                T = per_task[task]
                T["советник"] += adv
                T["исполнение" if role == "исполнители" else "ревью"] += usd - adv
                if role == "исполнители":
                    models[task][re.sub(r"^claude-|\[.*\]$", "", msg.get("model") or "?")] += 1
            if n and role == "прочее":
                others[desc[:60]] += 1
            if n and role != "оркестратор":
                agents[role] += 1
                if role == "исполнители" and task:
                    per_task[task]["контекстов"] += 1

    waits = [(usage.parse_ts(w.get("from")), usage.parse_ts(w.get("to"))) for w in state.get("waits") or []]
    waits = [(a, b) for a, b in waits if a and b]

    def active_min(a, b):
        return (b - a - sum(max(0, min(b, y) - max(a, x)) for x, y in waits)) / 60

    tickets = state.get("tickets") or []
    done = [t for t in tickets if t.get("status") in ("done", "accepted")]
    total = sum(s.usd for s in roles.values())
    adv = sum(s.advisor for s in roles.values())
    hours = busy_hours(stamps)
    lead = roles.get("оркестратор") or Stats(prices)
    end = "finishedAt" if state.get("finishedAt") else "updatedAt, прогон не закрыт"
    print("=" * 74)
    print(f"{run}   схема {state.get('scheme') or 'prd'}")
    print(f"  окно {state.get('startedAt', '')[:16]} -> {(state.get('finishedAt') or state.get('updatedAt') or '')[:16]} ({end})")
    print(f"  сессий {len(sessions) - len(missing)} из {len(sessions)}"
          + (f", не найдены здесь: {', '.join(s[:8] for s in missing)}" if missing else "")
          + f"; цены: {prices.get('source', '?')[:40]} на {prices.get('at', '?')}")
    if not stamps:
        print("  запросов в окне нет")
        return
    per_done = f"${total / len(done):.1f}" if done else "-"
    print(f"  ИТОГО ${total:.1f}, задач сделано {len(done)} из {len(tickets)}, на задачу {per_done}")
    print(f"  активных часов {hours:.1f}" + (f", {hours / len(done):.2f} на задачу" if done else ""))
    print(f"  советник ${adv:.1f} ({adv / total * 100:.0f}%) в {sum(s.advisor_calls for s in roles.values())} вызовах")
    print(f"  контекст оркестратора: пик {lead.peak_ctx / 1000:.0f}k, в конце окна {(lead.ctx or [0])[-1] / 1000:.0f}k")
    print(f"\n  {'роль':<18}{'$':>8}{'доля':>6}{'советник':>10}{'агентов':>9}{'запросов':>10}")
    for role, s in sorted(roles.items(), key=lambda kv: -kv[1].usd):
        print(f"  {role:<18}{s.usd:>8.1f}{s.usd / total * 100:>5.0f}%{s.advisor:>10.1f}"
              f"{agents.get(role, 0) or '-':>9}{s.req:>10}")
    if others:
        print("  прочее по description:", "; ".join(f"{d} x{n}" for d, n in others.most_common(5)))

    print("\n  по задачам: $ без оркестратора, минуты по state.js за вычетом ожиданий")
    print(f"  {'задача':<8}{'метка':<10}{'модель':<16}{'исполнение':>11}{'советник':>9}{'ревью':>7}"
          f"{'итого':>7}{'контекстов':>11}{'мин':>6}")
    for t in tickets:
        T = per_task.get(t.get("id"), {})
        a, b = usage.parse_ts(t.get("startedAt")), usage.parse_ts(t.get("finishedAt"))
        mins = f"{active_min(a, b):.0f}" if a and b else "-"
        model = "+".join(m for m, _ in models.get(t.get("id"), collections.Counter()).most_common()) or "-"
        cost = T.get("исполнение", 0) + T.get("советник", 0) + T.get("ревью", 0)
        print(f"  {t.get('id', '?'):<8}{t.get('model') or '-':<10}{model[:15]:<16}{T.get('исполнение', 0):>11.1f}"
              f"{T.get('советник', 0):>9.1f}{T.get('ревью', 0):>7.1f}{cost:>7.1f}"
              f"{int(T.get('контекстов', 0)):>11}{mins:>6}")
    lost = sorted(set(per_task) - {t.get("id") for t in tickets})
    if lost:
        print(f"  номера из description без задачи в state.js: {', '.join(lost)}")


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
        adv = main.advisor + sum(s.advisor for _, s in subs)
        n_adv = main.advisor_calls + sum(s.advisor_calls for _, s in subs)
        print(f"  советник:    ${adv:8.0f}  {n_adv:5d} вызовов, {adv / total * 100 if total else 0:.0f}% итога")
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
            seen, with_tool = set(), set()
            for path in paths:
                for line in io.open(path, encoding="utf-8", errors="replace"):
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    t = d.get("type")
                    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                    if t == "assistant" and (msg.get("usage") or {}):
                        # строк на запрос несколько, по блоку на строку: проза считается с каждой, запрос — один раз
                        seen.add(msg.get("id"))
                        for b in blocks(msg):
                            if b.get("type") == "text":
                                cat["проза ассистента"] += len(b.get("text", ""))
                            elif b.get("type") == "tool_use":
                                with_tool.add(msg.get("id"))
                                n = len(json.dumps(b.get("input") or {}, ensure_ascii=False))
                                cat["аргументы вызовов"] += n
                                tool_in[b.get("name", "?")] += n
                    elif t == "user":
                        for b in blocks(msg):
                            if b.get("type") == "tool_result":
                                c = b.get("content")
                                cat["результаты инструментов"] += len(
                                    c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
                            elif b.get("type") == "text":
                                cat["реплики и напоминания"] += len(b.get("text", ""))
            req, no_tool = len(seen), len(seen - with_tool)
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
        for msg, _ in records(path):
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
    "run": cmd_run,
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
