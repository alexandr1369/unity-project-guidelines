#!/usr/bin/env python3
# Счётчик архитектурного аудита. Память ассистента его не ведёт.
# Порог — восемь влитых PR после строки «Последний проход — PR #N» в TASKS.md.
# Открытый PR не считается. Хук напоминает на первом сообщении сессии и сразу
# после merge или fetch/pull. Новая сессия напомнит снова, пока строка не сдвинута.
import json
import os
import re
import subprocess
import sys
import time

EVERY = 8
LAST_AUDIT = re.compile(r"Последний проход — PR #(\d+)")
CACHE_SECONDS = 900


def due(last_audit, merged_numbers):
    since = sorted({number for number in merged_numbers if number > last_audit})
    if len(since) < EVERY:
        return None
    return since


def reminder(last_audit, since):
    listed = ", ".join("#%d" % number for number in since)
    return (
        "Архитектурный аудит просрочен. Последний проход — PR #%d. После него влиты %d: %s. "
        "Восьмой — PR #%d. Текущую просьбу доделай и следующей задачей возьми проход по разделу "
        "«Аудит ECS»: отдельный PR только с системными несоответствиями. Счётчик в голове не веди, "
        "его читает этот хук. Молча переходить к другим задачам нельзя."
        % (last_audit, len(since), listed, since[EVERY - 1]))


def failed_reminder():
    return (
        "Не удалось сверить влитые PR со строкой «Последний проход — PR #N» в TASKS.md. "
        "До другой работы посчитай `gh pr list --state merged` после этого номера. Молча пропускать нельзя.")


def missing_counter_reminder():
    return (
        "В TASKS.md нет строки «Последний проход — PR #N», раздел «Архитектурный аудит». "
        "До другой работы восстанови счётчик. Молча пропускать нельзя.")


def git_output(args, cwd):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=8)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_path(cwd, args):
    value = git_output(args, cwd)
    if not value:
        return None
    if os.path.isabs(value):
        return value
    return os.path.join(cwd, value)


def last_audit(cwd):
    root = git_output(["git", "rev-parse", "--show-toplevel"], cwd)
    if not root:
        return None
    path = os.path.join(root, "TASKS.md")
    if not os.path.isfile(path):
        return None
    match = LAST_AUDIT.search(open(path, encoding="utf-8").read())
    if not match:
        return "missing"
    return int(match.group(1))


def merged_numbers(cwd, refresh):
    git_dir = git_path(cwd, ["git", "rev-parse", "--git-common-dir"])
    cache = os.path.join(git_dir, "audit-due-merged.json") if git_dir else None
    if cache and not refresh:
        try:
            saved = json.load(open(cache, encoding="utf-8"))
            if time.time() - saved["at"] < CACHE_SECONDS:
                return saved["numbers"]
        except (OSError, ValueError, KeyError):
            pass
    result = subprocess.run(
        ["gh", "pr", "list", "--state", "merged", "--limit", "200", "--json", "number"],
        cwd=cwd, capture_output=True, text=True, timeout=8)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    numbers = [item["number"] for item in json.loads(result.stdout)]
    if cache:
        json.dump({"at": time.time(), "numbers": numbers}, open(cache, "w", encoding="utf-8"))
    return numbers


def told_path(cwd):
    git_dir = git_path(cwd, ["git", "rev-parse", "--git-common-dir"])
    if not git_dir:
        return None
    return os.path.join(git_dir, "audit-due-reminded.json")


def already_told(cwd, session, signature):
    path = told_path(cwd)
    if not path or not session:
        return False
    try:
        saved = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return saved.get("session") == session and saved.get("signature") == signature


def mark_told(cwd, session, signature):
    path = told_path(cwd)
    if not path or not session:
        return
    json.dump({"session": session, "signature": signature}, open(path, "w", encoding="utf-8"))


def context(cwd, session, refresh, repeat):
    last = last_audit(cwd)
    if last is None:
        return ""
    if last == "missing":
        return missing_counter_reminder()
    try:
        numbers = merged_numbers(cwd, refresh)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        return failed_reminder()
    since = due(last, numbers)
    if since is None:
        return ""
    signature = "%d:%d" % (last, len(since))
    if not repeat and already_told(cwd, session, signature):
        return ""
    mark_told(cwd, session, signature)
    return reminder(last, since)


def self_test():
    assert due(147, list(range(148, 155))) is None
    eight = due(147, list(range(148, 156)))
    assert eight is not None and len(eight) == 8 and eight[7] == 155
    ten = due(147, list(range(148, 158)))
    assert ten is not None and len(ten) == 10 and ten[7] == 155 and 158 not in ten
    assert due(157, list(range(148, 158))) is None
    shuffled = due(147, [155, 148, 149, 150, 151, 152, 153, 154, 154])
    assert shuffled == list(range(148, 156))
    sample = "Последний проход — PR #147. Следующий — на восьмом влитом PR после него.\n"
    assert LAST_AUDIT.search(sample).group(1) == "147"
    assert "Восьмой — PR #155" in reminder(147, ten)
    assert "влиты 10" in reminder(147, ten)
    print("self-test ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
