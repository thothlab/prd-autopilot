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

Деньги — по списочным ценам API, а не по счёту подписки.
"""

import datetime
import glob
import io
import json
import os
import re
import sys

ROOT = os.path.expanduser("~/.claude/projects")

# $ за миллион токенов: (вход, выход, чтение кэша). Запись кэша — от входа: 5m x1.25, 1h x2.
# Сверено с прайсом на 10.2026.
PRICE = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-mythos-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
# Haiku 5.5 дороже на запросах длиннее 100K токенов.
LONG = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05))}
FALLBACK = "claude-opus-5-5"


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

    def add(self, part, model, fast):
        # model без даты и без хвоста вроде "[1m]"
        base = re.sub(r"\[.*\]$", "", model or "")
        rates = PRICE.get(base)
        unpriced = rates is None
        if unpriced:
            rates = PRICE[FALLBACK]
        cc = part.get("cache_creation") or {}
        w1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
        w5m = cc.get("ephemeral_5m_input_tokens", 0) or 0
        write = part.get("cache_creation_input_tokens", 0) or 0
        if w1h + w5m < write:
            w5m += write - w1h - w5m
        fresh = part.get("input_tokens", 0) or 0
        read = part.get("cache_read_input_tokens", 0) or 0
        out = part.get("output_tokens", 0) or 0
        if base in LONG and fresh + read + write > LONG[base][0]:
            rates = LONG[base][1]
        pin, pout, pread = rates
        usd = (fresh * pin + w5m * pin * 1.25 + w1h * pin * 2.0 + read * pread + out * pout) / 1e6
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


def scan(path, bucket, lo, hi, seen, unpriced):
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
            miss = bucket.add(part, part.get("model") or msg.get("model"), fast)
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

    lead, agents = Bucket(), Bucket()
    seen, unpriced, n_agents, found = set(), set(), 0, []
    for s in sessions:
        main_path, subs = transcripts(s)
        if not main_path:
            continue
        found.append(s)
        scan(main_path, lead, lo, hi, seen, unpriced)
        for p in subs:
            n_agents += scan(p, agents, lo, hi, seen, unpriced)
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
    }
    # страница опрашивает файл каждые десять секунд — подмена целиком, а не запись поверх
    tmp = out_path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.USAGE =\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, out_path)
    inp = total.input + total.cache_read + total.cache_write
    print(f"usage: вход {inp / 1e6:.1f}M, выход {total.output / 1e6:.2f}M, ~${total.usd:.0f} по ценам API")


if __name__ == "__main__":
    main(sys.argv)
