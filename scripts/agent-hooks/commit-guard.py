#!/usr/bin/env python3
# Хук Claude Code и Codex (PreToolUse): не даёт агенту коммитить с глаголом git-действия в сообщении
# («Закоммитил», «Запушил»), индексировать всё подряд (`git commit -a`, `git add -A`) и коммитить код, который не проходит
# механические проверки правил из code-checks.py. Правила — GIT_WORKFLOW.md и AI_COLLABORATION_GUIDELINES.md.
import importlib
import json
import os
import re
import sys


def commit_directory(command, cwd):
    # Папка, где выполнится `git commit`: последний `cd`/`git -C` перед ним, с подстановкой ~, $HOME и переменных команды.
    variables = {"HOME": os.path.expanduser("~")}
    directory = cwd
    for token in re.finditer(r"(\w+)=([^\s;&|]+)|\bcd\s+([^\s;&|]+)|\bgit\s+-C\s+([^\s;&|]+)", command):
        name, value, cd_path, git_path = token.groups()
        expand = lambda text: os.path.expanduser(re.sub(r"\$\{?(\w+)\}?", lambda m: variables.get(m.group(1), m.group(0)), text.strip("\"'")))
        if name:
            variables[name] = expand(value)
        else:
            path = expand(cd_path or git_path)
            directory = path if os.path.isabs(path) else os.path.join(directory, path)
    return directory


data = json.load(sys.stdin)
tool_input = data.get("tool_input") or {}
command = tool_input.get("command") or tool_input.get("cmd") or ""
if isinstance(command, list):
    command = " ".join(command)

START = r"(?:^|[;&|(]\s*)"
GIT_VERBS = r"(?:Закоммитил|Закомитил|Запушил|Влил|Смержил|Замержил|Коммит|Пуш)"
reasons = []

for message in re.findall(START + r"git commit\b[^\n]*?\s-[a-zA-Z]*m\s+([\"'])(.*?)\1", command, re.M):
    if re.match(GIT_VERBS, message[1].strip()):
        reasons.append("сообщение коммита начинается с git-действия: глагол должен называть изменение в проекте")

unquoted = re.sub(r"([\"']).*?\1", "", command, flags=re.S)
if re.search(START + r"git commit\b[^\n;&|]*\s(?:-[a-zA-Z]*a[a-zA-Z]*|--all)\b", unquoted, re.M):
    reasons.append("`git commit -a` уносит незакоммиченные правки владельца: коммит собирать по путям задачи")
if re.search(START + r"git add\b[^\n;&|]*\s(?:-A|--all)\b", unquoted, re.M):
    reasons.append("`git add -A` индексирует всё подряд: добавлять файлы по путям задачи")

commit = re.search(START + r"git(?:\s+-[cC]\s+\S+)*\s+commit(?![-\w])", command, re.M)
if commit and re.search(START + r"git\b[^\n;&|]*\badd\b", unquoted[:commit.start()], re.M):
    reasons.append("`git add` и `git commit` в одной команде: хук проверяет индекс до запуска команды, и добавленные файлы "
                   "уходят без проверки. Индексируй отдельной командой, коммить следующей")
if commit:
    repo = commit_directory(command[:commit.end()], data.get("cwd") or ".")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    code_checks = importlib.import_module("code-checks")
    found = code_checks.violations(repo) if os.path.isdir(repo) else []
    if found:
        reasons.append("код в индексе нарушает правила _guidelines:\n" + "\n".join(found[:15]))

if reasons:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "; ".join(reasons) + ". Сверься с правилом: коммиты — GIT_WORKFLOW.md, код — CODE_STYLE.md и OPTIONAL_ENTITAS.md."}},
        ensure_ascii=False))
