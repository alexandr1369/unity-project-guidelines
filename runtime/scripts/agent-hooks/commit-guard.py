#!/usr/bin/env python3
# Хук Claude Code и Codex (PreToolUse): не даёт агенту коммитить с глаголом git-действия в сообщении
# («Закоммитил», «Запушил»), индексировать всё подряд (`git commit -a`, `git add -A`), коммитить код, который не проходит
# механические проверки правил из code-checks.py, и вливать PR, на ветке которого стоят открытые дочерние PR.
# Правила — GIT_WORKFLOW.md и AI_COLLABORATION_GUIDELINES.md.
import importlib
import json
import os
import re
import subprocess
import sys

from command_directory import GIT, START, command_directory
from guidelines_path import guidelines_path


data = json.load(sys.stdin)
tool_input = data.get("tool_input") or {}
cwd = tool_input.get("workdir") or data.get("cwd") or "."
command = tool_input.get("command") or tool_input.get("cmd") or ""
if isinstance(command, list):
    command = " ".join(command)

GIT_VERBS = r"(?:Закоммитил|Закомитил|Запушил|Влил|Смержил|Замержил|Коммит|Пуш)"
reasons = []

for message in re.findall(START + GIT + r"\s+commit\b[^\n]*?\s-[a-zA-Z]*m\s+([\"'])(.*?)\1", command, re.M):
    if re.match(GIT_VERBS, message[1].strip()):
        reasons.append("сообщение коммита начинается с git-действия: глагол должен называть изменение в проекте")

unquoted = re.sub(r"([\"']).*?\1", "ARG", command, flags=re.S)
if re.search(START + GIT + r"\s+commit\b[^\n;&|]*\s(?:-[a-zA-Z]*a[a-zA-Z]*|--all)\b", unquoted, re.M):
    reasons.append("`git commit -a` уносит незакоммиченные правки владельца: коммит собирать по путям задачи")
if re.search(START + GIT + r"\s+add\b[^\n;&|]*\s(?:-A|--all)\b", unquoted, re.M):
    reasons.append("`git add -A` индексирует всё подряд: добавлять файлы по путям задачи")

commit = re.search(START + GIT + r"\s+commit(?![-\w])", command, re.M)
plain_commit = re.search(START + GIT + r"\s+commit(?![-\w])", unquoted, re.M)
if plain_commit and re.search(START + GIT + r"\s+push\b", unquoted[plain_commit.end():], re.M):
    reasons.append("`git commit` и `git push` в одной команде: сначала заверши самопроверку итогового diff после коммита, затем отдельной командой публикуй")
if commit and re.search(START + r"git\b[^\n;&|]*\badd\b", unquoted[:commit.start()], re.M):
    reasons.append("`git add` и `git commit` в одной команде: хук проверяет индекс до запуска команды, и добавленные файлы "
                   "уходят без проверки. Индексируй отдельной командой, коммить следующей")
if commit:
    repo = command_directory(command[:commit.end()], cwd)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    code_checks = importlib.import_module("code-checks")
    found = code_checks.violations(repo) if os.path.isdir(repo) else []
    if found:
        reasons.append("код в индексе нарушает правила _guidelines:\n" + "\n".join(found[:15]))

merge = re.search(START + r"gh pr merge\s+(\d+)", command, re.M)
if merge:
    # GIT_WORKFLOW.md, стек PR: base дочернего переставляют до merge родителя, после merge поздно.
    repo = command_directory(command[:merge.end()], cwd)
    gh = lambda *args: subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True, timeout=4).stdout.strip()
    try:
        head = gh("pr", "view", merge.group(1), "--json", "headRefName", "-q", ".headRefName")
        children = gh("pr", "list", "--state", "open", "--base", head, "--json", "number", "-q", ".[].number").split() if head else []
    except (OSError, subprocess.TimeoutExpired):
        children = []
    if children:
        reasons.append(f"на ветке PR #{merge.group(1)} стоят открытые PR {', '.join('#' + child for child in children)}: до merge "
                       f"переставь их base на ветку, куда вливается родитель (`gh pr edit <номер> --base main`), после merge поздно")

if reasons:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "; ".join(reasons) + ". Сверься с правилом: коммиты — GIT_WORKFLOW.md, код — CODE_STYLE.md и OPTIONAL_ENTITAS.md."}},
        ensure_ascii=False))
elif commit:
    guidelines = guidelines_path("AI_COLLABORATION_GUIDELINES.md")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "Перед коммитом: до публикации и отчёта о готовности сам пройди весь итоговый diff по разделам «Самопроверка перед отчётом» и «Выводы из замечаний владельца» в " + guidelines + ". Механический допуск не доказывает смысловую корректность."}}, ensure_ascii=False))
