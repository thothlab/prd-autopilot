#!/usr/bin/env python3
"""Расход токенов прогона для карточки дашборда.

    python3 "$skillDir/tools/usage.py" "$RUN"

Читает транскрипты Claude Code (`~/.claude/projects/*/<сессия>.jsonl` и
`<сессия>/subagents/*.jsonl`), берёт запросы от `startedAt` прогона до
`finishedAt`, пишет `$RUN/usage.js` и печатает одну строку итога.

Сессии копятся в самом `usage.js`: продолжение прогона в новой сессии
добавляет её к прежним, а не начинает счёт заново. Нет `CLAUDE_CODE_SESSION_ID`
и нет прежних сессий (другой агент, не Claude Code) — файл не пишется,
и карточка на дашборде не появляется.

Деньги — по списочным ценам API, а не по счёту подписки. Прайс скачивается
с PRICING_URL один раз на прогон и кладётся снимком в `usage.js`: весь прогон
считается по одним ценам, даже если прайс поменялся посередине. Сети нет или
таблица не разобралась — встроенная BUILTIN, и до удачи скрипт пробует снова
на каждом запуске. Карточка пишет, по каким ценам посчитано.
"""

import datetime
import glob
import io
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects")

PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
BUILTIN_AT = "2026-10-23"

# $ за миллион токенов: (вход, выход, чтение кэша); запись кэша — от входа, 5m x1.25, 1h x2.
BUILTIN = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-mythos-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
# Haiku 5.5 дороже на запросах длиннее 100K токенов.
BUILTIN_LONG = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05))}
FALLBACK = "claude-opus-5-5"


def rates5(pin, pout, pread):
    """Цены в порядке колонок прайса: вход, запись 5m, запись 1h, чтение кэша, выход."""
    return [pin, pin * 1.25, pin * 2.0, pread, pout]


def builtin_prices():
    return {
        "source": "builtin", "at": BUILTIN_AT,
        "models": {k: rates5(*v) for k, v in BUILTIN.items()},
        "long": {k: [n, rates5(*v)] for k, (n, v) in BUILTIN_LONG.items()},
    }


def model_id(name):
    """«Claude Opus 5.5 ([limited availability](...))» -> claude-opus-5-5."""
    name = re.sub(r"\(.*", "", name).strip().lower()
    return re.sub(r"[ .]+", "-", name)


def fetch_prices():
    """Таблица моделей из прайса; что-то не так с сетью или вёрсткой — None."""
    try:
        req = urllib.request.Request(PRICING_URL, headers={"User-Agent": "prd-usage"})
        text = urllib.request.urlopen(req, timeout=5).read().decode("utf-8", "replace")
    except Exception:
        return None
    money = lambda cell: float(re.search(r"\$([\d.]+)", cell).group(1))
    models, long, head = {}, {}, None
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if head is None:
            # первая таблица с колонкой «Cache hits» — основная, дальше batch и прочие
            if len(cells) == 6 and "Base input" in cells[1] and "Cache hits" in cells[4]:
                head = []
            continue
        if not line.startswith("|"):
            break
        if set(cells[0]) <= set(":- "):
            continue
        try:
            rates = [money(c) for c in cells[1:6]]
        except (AttributeError, IndexError, ValueError):
            return None
        mid = model_id(re.sub(r"<sup>.*?</sup>", "", cells[0]))
        tier = re.search(r"over ([\d,]+) tokens", cells[0])
        if tier:
            long[mid] = [int(tier.group(1).replace(",", "")), rates]
        else:
            models[mid] = rates
    # вёрстка поменялась незаметно для разбора — лучше встроенные цены, чем чужие числа
    sane = all(r[0] > 0 and r[4] > r[0] and r[3] < r[0] < r[2] for r in models.values())
    if FALLBACK not in models or len(models) < 5 or not sane:
        return None
    return {"source": PRICING_URL, "at": datetime.date.today().isoformat(), "models": models, "long": long}


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_js(path, var):
    """JSON из файла вида `window.X =\\n{...}`; нет файла или он помят — None."""
    try:
        text = io.open(path, encoding="utf-8").read()
    except OSError:
        return None
    text = re.sub(r"^\s*window\.%s\s*=\s*" % var, "", text).rstrip().rstrip(";")
    try:
        return json.loads(text)
    except ValueError:
        return None


def transcripts(sid):
    mains = glob.glob(os.path.join(ROOT, "*", sid + ".jsonl"))
    if not mains:
        return None, []
    main = mains[0]
    return main, sorted(glob.glob(os.path.join(main[:-6], "subagents", "*.jsonl")))


class Bucket:
    def __init__(self):
        self.input = self.cache_read = self.cache_write = self.output = self.requests = 0
        self.usd = 0.0

    def add(self, part, model, fast, prices):
        # model без хвоста вроде "[1m]"
        base = re.sub(r"\[.*\]$", "", model or "")
        table = prices["models"]
        rates = table.get(base)
        unpriced = rates is None
        if unpriced:
            rates = table.get(FALLBACK) or rates5(*BUILTIN[FALLBACK])
        cc = part.get("cache_creation") or {}
        w1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
        w5m = cc.get("ephemeral_5m_input_tokens", 0) or 0
        write = part.get("cache_creation_input_tokens", 0) or 0
        if w1h + w5m < write:
            w5m += write - w1h - w5m
        fresh = part.get("input_tokens", 0) or 0
        read = part.get("cache_read_input_tokens", 0) or 0
        out = part.get("output_tokens", 0) or 0
        tier = prices.get("long", {}).get(base)
        if tier and fresh + read + write > tier[0]:
            rates = tier[1]
        pin, p5m, p1h, pread, pout = rates
        usd = (fresh * pin + w5m * p5m + w1h * p1h + read * pread + out * pout) / 1e6
        self.usd += usd * (2.0 if fast else 1.0)
        self.input += fresh
        self.cache_read += read
        self.cache_write += write
        self.output += out
        return base if unpriced and base else None

    def as_dict(self):
        return {
            "input": self.input, "cacheRead": self.cache_read, "cacheWrite": self.cache_write,
            "output": self.output, "requests": self.requests, "usd": round(self.usd, 2),
        }


def scan(path, bucket, lo, hi, seen, unpriced, prices):
    """Один оплаченный запрос пишется в транскрипт строкой на каждый блок ответа
    с одним и тем же usage — без дедупа по message.id счёт выходит примерно вдвое."""
    counted = False
    for line in io.open(path, encoding="utf-8", errors="replace"):
        if '"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        u = msg.get("usage")
        if d.get("type") != "assistant" or not u or msg.get("model") == "<synthetic>":
            continue
        key = msg.get("id") or d.get("requestId") or d.get("uuid")
        if key in seen:
            continue
        t = parse_ts(d.get("timestamp"))
        if t is None or t < lo or (hi is not None and t > hi):
            continue
        seen.add(key)
        fast = u.get("speed") == "fast"
        # в iterations лежат и вызовы советника со своей моделью; верхний usage их не включает
        for part in u.get("iterations") or [u]:
            miss = bucket.add(part, part.get("model") or msg.get("model"), fast, prices)
            if miss:
                unpriced.add(miss)
        bucket.requests += 1
        counted = True
    return counted


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    run = argv[1]
    state = read_js(os.path.join(run, "state.js"), "STATE")
    if not state or parse_ts(state.get("startedAt")) is None:
        sys.exit("usage: state.js не прочитан или без startedAt")
    lo, hi = parse_ts(state.get("startedAt")), parse_ts(state.get("finishedAt"))
    out_path = os.path.join(run, "usage.js")
    prev = read_js(out_path, "USAGE") or {}
    sessions = list(prev.get("sessions") or [])
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid and sid not in sessions:
        sessions.append(sid)
    if not sessions:
        print("usage: не Claude Code или нет id сессии - токены не считаются")
        return

    prices = prev.get("prices")
    if not prices or prices.get("source") == "builtin":
        prices = fetch_prices() or builtin_prices()

    lead, agents = Bucket(), Bucket()
    seen, unpriced, n_agents, found = set(), set(), 0, []
    for s in sessions:
        main_path, subs = transcripts(s)
        if not main_path:
            continue
        found.append(s)
        scan(main_path, lead, lo, hi, seen, unpriced, prices)
        for p in subs:
            n_agents += scan(p, agents, lo, hi, seen, unpriced, prices)
    if not found:
        print("usage: транскрипты сессий не найдены - usage.js не тронут")
        return

    total = Bucket()
    for b in (lead, agents):
        for f in ("input", "cache_read", "cache_write", "output", "requests", "usd"):
            setattr(total, f, getattr(total, f) + getattr(b, f))
    data = {
        "updatedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "sessions": sessions,
        "lead": lead.as_dict(),
        "agents": dict(agents.as_dict(), count=n_agents),
        "total": total.as_dict(),
        "unpriced": sorted(unpriced),
        "prices": prices,
    }
    # страница опрашивает файл каждые десять секунд — подмена целиком, а не запись поверх
    tmp = out_path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.USAGE =\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, out_path)
    inp = total.input + total.cache_read + total.cache_write
    src = "встроенным ценам" if prices["source"] == "builtin" else "прайсу на " + prices["at"]
    print(f"usage: вход {inp / 1e6:.1f}M, выход {total.output / 1e6:.2f}M, ~${total.usd:.0f} по {src}")


if __name__ == "__main__":
    main(sys.argv)
