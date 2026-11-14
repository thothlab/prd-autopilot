#!/usr/bin/env python3
"""Правка `state.js` прогона одной короткой командой.

    python3 "$skillDir/tools/state.py" "$RUN" <правка> [<правка> ...]
    python3 "$skillDir/tools/state.py" "$RUN" - <<'EOF'
    ticket 03 note="текст с `кодом`, $414 и кавычками"
    EOF

Правки идут по порядку, файл пишется один раз в конце, атомарно, и в нём
двигается `updatedAt`. Одна ошибка - и не пишется ничего.

    ticket 03 status=review                задача по id
    ticket 03,04 status=in-progress startedAt=now
    stage build status=active startedAt=now note="3 из 5 задач готовы"
    set tests:='{"passed": 34, "failed": 0}' requirements.done:=9
    ticket 03 repairs+=1 calls+=44 repairFindings+="дубль при повторном нажатии"
    set concerns+="FavoritesScreen.kt:40 - два формата даты"
    wait action "пуш в main - нужно твоё да"
    unwait                                 ответ получен: waiting -> waits
    status                                 печатает слоты и счёт, ничего не пишет

Значения: `k=v` - строка, `k:=v` - JSON (числа, true, null, списки, объекты),
`k+=v` - дописать строку в список или прибавить число, `k+:=v` - то же с JSON.
`k:=@путь` - JSON из файла (массив задач Фазы 4). `now` в любом месте
значения - текущее время с поясом. Ключ с точками (`requirements.done`)
идёт вглубь объекта.

Значение с обратной кавычкой, `$` или кавычками - только через `-` и heredoc
в одинарных кавычках: в аргументах командной строки шелл раскроет их раньше
скрипта. Строки heredoc разбираются как аргументы шелла, но без подстановок.
"""

import datetime
import io
import json
import os
import re
import shlex
import sys

BUSY = ("in-progress", "review", "repair")


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def load(path):
    try:
        text = io.open(path, encoding="utf-8").read()
    except OSError as e:
        sys.exit(f"state: не прочитан {path} ({e.strerror}) - путь к run/prd_XX литералом из state.js")
    body = re.sub(r"^\s*window\.STATE\s*=\s*", "", text).rstrip().rstrip(";")
    try:
        return json.loads(body)
    except ValueError as e:
        sys.exit(f"state: {path} не разбирается как JSON ({e}) - перепиши файл целиком")


def save(path, state):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.STATE =\n" + json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    # дашборд читает файл каждые десять секунд: замена целиком, а не запись поверх
    os.replace(tmp, path)


def value(raw, is_json, stamp):
    if is_json and raw.startswith("@"):
        try:
            raw = io.open(raw[1:], encoding="utf-8").read()
        except OSError as e:
            sys.exit(f"state: не прочитан {raw[1:]} ({e.strerror})")
    if is_json:
        try:
            return json.loads(raw.replace('"now"', json.dumps(stamp)))
        except ValueError as e:
            sys.exit(f"state: не JSON: {raw} ({e})")
    return stamp if raw == "now" else raw


def assign(obj, expr, stamp):
    m = re.match(r"^([\w.]+)(\+?)(:?)=(.*)$", expr, re.S)
    if not m:
        sys.exit(f"state: не понял правку {expr!r}: нужно k=v, k:=json, k+=v")
    key, append, is_json, raw = m.groups()
    *path, last = key.split(".")
    for k in path:
        nxt = obj.get(k)
        if not isinstance(nxt, dict):
            nxt = obj[k] = {}
        obj = nxt
    v = value(raw, bool(is_json), stamp)
    if not append:
        obj[last] = v
        return
    cur = obj.get(last)
    # `calls+=44` на пустом поле - число, а не список из строки "44"
    if not is_json and not isinstance(cur, list) and re.fullmatch(r"-?\d+", v):
        v = int(v)
    if cur is None:
        obj[last] = v if isinstance(v, (int, float)) else [v]
    elif isinstance(cur, list):
        cur.append(v)
    elif isinstance(cur, (int, float)) and isinstance(v, (int, float)):
        obj[last] = cur + v
    else:
        sys.exit(f"state: {key}+= - в поле {type(cur).__name__}, дописывать некуда")


def pick(items, ids, what):
    found = []
    for i in ids.split(","):
        hit = [x for x in items if str(x.get("id")) == i]
        if not hit:
            sys.exit(f"state: {what} {i} нет в state.js - ничего не записано")
        found += hit
    return found


def status(state):
    t = state.get("tickets") or []
    busy = [f"{x['id']} {x['status']}" for x in t if x.get("status") in BUSY]
    pend = [x["id"] for x in t if x.get("status") == "pending"]
    done = sum(1 for x in t if x.get("status") == "done")
    w = state.get("waiting")
    print(f"в работе {len(busy)}" + (f" ({', '.join(busy)})" if busy else "")
          + f"; ждут {len(pend)}" + (f" ({', '.join(pend)})" if pend else "")
          + f"; сдано {done} из {len(t)}" + (f"; ждём пользователя: {w.get('kind')}" if w else ""))


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    run, ops = argv[1], argv[2:]
    if ops == ["-"]:
        try:
            ops = [tok for line in sys.stdin.read().splitlines() for tok in shlex.split(line)]
        except ValueError as e:
            sys.exit(f"state: строка не разобралась ({e}) - проверь кавычки")
    path = run if run.endswith(".js") else os.path.join(run, "state.js")
    state = load(path)
    stamp = now()
    target, wrote, i = None, False, 0
    while i < len(ops):
        op = ops[i]
        if op == "status":
            status(state)
            target = None
        elif op in ("ticket", "stage") and i + 1 < len(ops):
            items = state.get("tickets" if op == "ticket" else "stages") or []
            target = pick(items, ops[i + 1], "задачи" if op == "ticket" else "этапа")
            i += 1
        elif op == "set":
            target = [state]
        elif op == "wait" and i + 2 < len(ops):
            state["waiting"] = {"kind": ops[i + 1], "note": ops[i + 2], "since": stamp}
            target, wrote = None, True
            i += 2
        elif op == "unwait":
            w = state.get("waiting")
            if w:
                state.setdefault("waits", []).append({"from": w.get("since"), "to": stamp, "note": w.get("note", "")})
                state["waiting"] = None
                wrote = True
            target = None
        elif target is not None and "=" in op:
            for obj in target:
                assign(obj, op, stamp)
            wrote = True
        else:
            sys.exit(f"state: не понял {op!r} - см. python3 state.py без аргументов")
        i += 1
    if wrote:
        state["updatedAt"] = stamp
        save(path, state)
        print(f"state: записано, updatedAt {stamp[11:16]}")


if __name__ == "__main__":
    main(sys.argv)
