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
    commits                                сданные задачи, чьих коммитов нет в ветке, - ничего не пишет
    ticket 03 by-user "их слова" status=pending repairs:=0 retries:=0

Скрипт отказывает - и не пишет ничего - на третьем `repairs`, `retries` или
`handoffs` задачи, на любой правке задачи `failed`, на `done` без `commit` и
на `set finishedAt=now`, пока прогон не готов: красный или непроведённый
гейт, незакрытый этап, несданная задача, коммит не в ветке. Снимает отказ
только `by-user "слова пользователя"` в той же команде; закрытие с ним и с
расхождениями слепой приёмки ложится в `caveats` - «сдано с оговорками».

Значения: `k=v` - строка, `k:=v` - JSON (числа, true, null, списки, объекты),
`k+=v` - дописать строку в список или прибавить число, `k+:=v` - то же с JSON.
`k:=@путь` - JSON из файла (массив задач Фазы 4). `now` в любом месте
значения - текущее время с поясом. Ключ с точками (`requirements.done`)
идёт вглубь объекта.

Значение с обратной кавычкой, `$` или кавычками - только через `-` и heredoc
в одинарных кавычках: в аргументах командной строки шелл раскроет их раньше
скрипта. Строки heredoc разбираются как аргументы шелла, но без подстановок.
"""

import copy
import datetime
import io
import json
import os
import re
import shlex
import subprocess
import sys

BUSY = ("in-progress", "review", "repair")
CAPS = {"repairs": 2, "retries": 2, "handoffs": 2}
CAP_NEXT = {
    "repairs": "дальше свежий контекст (retries+=1), phases/5-repair.md",
    "retries": "дальше status=failed с причиной и вопрос пользователю",
    "handoffs": "разрез неверен: status=failed с причиной и вопрос пользователю",
}


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
        verb = re.match(r"^(set|ticket|stage)\s", expr)
        hint = (f": слово {verb.group(1)} попало в кавычки вместе с правкой - вынеси его отдельным аргументом"
                if verb else ": нужно k=v, k:=json, k+=v")
        sys.exit(f"state: не понял правку {expr[:80]!r}{hint} - ничего не записано")
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


def code_repo(state):
    repo = state.get("codeRepo")
    if not repo and state.get("storage") == "local" and state.get("prdRoot"):
        # local: prdRoot = <repo>/.prd/PRD
        repo = os.path.dirname(os.path.dirname(state["prdRoot"].rstrip("/")))
    return repo


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def stray_commits(state):
    """Коммиты сданных задач, которых нет в ветке кода: строки причин."""
    owned = [(t.get("id"), t) for t in state.get("tickets") or [] if t.get("status") == "done"]
    sp = state.get("singlePass")
    if sp and sp.get("commit"):
        owned.append(("T0", sp))
    hashes = [(i, h) for i, t in owned for h in ([t["commit"]] if t.get("commit") else []) + list(t.get("commits") or [])]
    if not hashes:
        return []
    repo = code_repo(state)
    if not repo:
        return ["в state.js нет codeRepo - set codeRepo=<путь к репозиторию кода>, без него коммиты не проверить"]
    branch = state.get("codeBranch")
    ref = branch if branch and git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0 else "HEAD"
    out = []
    for i, h in hashes:
        r = git(repo, "merge-base", "--is-ancestor", h, ref)
        if r.returncode == 1:
            out.append(f"задача {i}: коммита {h} нет в {ref} - reset или rebase увёл его")
        elif r.returncode:
            out.append(f"задача {i}: коммит {h} не найден в {repo}")
    return out


def guard_tickets(before, after):
    old = {str(t.get("id")): t for t in before.get("tickets") or []}
    out = []
    for t in after.get("tickets") or []:
        tid, was = str(t.get("id")), old.get(str(t.get("id")))
        if was is None:
            continue
        if was.get("status") == "failed" and t != was:
            out.append(f"задача {tid} провалена: снова в работу - только словами пользователя (by-user)")
            continue
        for k, cap in CAPS.items():
            n, m = t.get(k) or 0, was.get(k) or 0
            if isinstance(n, (int, float)) and n > cap and n > m:
                out.append(f"задача {tid}: {k} станет {n} при потолке {cap} - {CAP_NEXT[k]}")
        if t.get("status") == "done" and was.get("status") != "done" and not (t.get("commit") or t.get("commits")):
            out.append(f"задача {tid}: done без commit= - задача сдана, когда есть её коммит")
    return out


def finish_open(state):
    """Чем прогон не готов к finishedAt - и отдельно расхождения G4, которые лягут оговоркой."""
    out = []
    for s in state.get("stages") or []:
        if s.get("status") == "failed":
            out.append(f"этап {s.get('id')} провален")
        elif s.get("status") not in ("done", "skipped"):
            out.append(f"этап {s.get('id')} ещё {s.get('status')} - приведи к правде в той же команде")
    for t in state.get("tickets") or []:
        if t.get("status") != "done":
            out.append(f"задача {t.get('id')} {t.get('status')}")
        elif not (t.get("commit") or t.get("commits")):
            out.append(f"задача {t.get('id')} done без коммита")
    sp = state.get("singlePass")
    if not state.get("tickets") and sp and not sp.get("commit"):
        out.append("единственный заход T0 без коммита")
    out += stray_commits(state)
    if state.get("coverage") is None:
        out.append("G2 не проводился (coverage: null)")
    blind = state.get("blind")
    if blind is None:
        out.append("G4 не проводился (blind: null)")
    elif blind.get("done") is False:
        out.append(f"G4 не проведён: {blind.get('verdict', '')}")
    tests = state.get("tests") or {}
    if (tests.get("failed") or 0) > 0:
        out.append(f"последний полный прогон красный: упало {tests['failed']}")
    drift = [f"G4: {m}" for m in (blind or {}).get("mismatches") or []]
    return out, drift


def main(argv):
    if len(argv) < 3 or argv[1] in ("-h", "--help", "help"):
        sys.exit(__doc__)
    run, ops = argv[1], argv[2:]
    if ops == ["-"]:
        try:
            ops = [tok for line in sys.stdin.read().splitlines() for tok in shlex.split(line)]
        except ValueError as e:
            sys.exit(f"state: строка не разобралась ({e}) - проверь кавычки")
    path = run if run.endswith(".js") else os.path.join(run, "state.js")
    state = load(path)
    before = copy.deepcopy(state)
    stamp = now()
    target, wrote, i, by_user = None, False, 0, None
    while i < len(ops):
        op = ops[i]
        if op == "status":
            status(state)
            target = None
        elif op == "commits":
            for line in stray_commits(state):
                print("! " + line)
            target = None
        elif op == "by-user" and i + 1 < len(ops):
            by_user = ops[i + 1].strip()
            if not by_user:
                sys.exit("state: by-user без слов пользователя - ничего не записано")
            i += 1
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
        elif "=" in op:
            sys.exit(f"state: правке {op[:80]!r} не к чему относиться - перед ней нужен set, ticket NN или stage ID; ничего не записано")
        else:
            sys.exit(f"state: не понял {op[:80]!r} - см. python3 state.py без аргументов; ничего не записано")
        i += 1
    refused = guard_tickets(before, state)
    closing = not before.get("finishedAt") and state.get("finishedAt")
    drift = []
    if closing:
        still, drift = finish_open(state)
        refused += still
    if refused and not by_user:
        sys.exit("state: отказ, ничего не записано:\n" + "\n".join("  - " + r for r in refused)
                 + "\nСнять отказ могут только слова пользователя: by-user \"<их слова>\" в той же команде.")
    if refused:
        state.setdefault("byUser", []).append({"at": stamp, "words": by_user, "overrode": refused})
        wrote = True
    if closing and (refused or drift):
        state["caveats"] = refused + drift
    if wrote:
        state["updatedAt"] = stamp
        save(path, state)
        print(f"state: записано, updatedAt {stamp[11:16]}")


if __name__ == "__main__":
    main(sys.argv)
