#!/usr/bin/env python3
# Хук Claude Code и Codex (PreToolUse): не даёт агенту коммитить с глаголом git-действия в сообщении
# («Закоммитил», «Запушил») и индексировать всё подряд (`git commit -a`, `git add -A`). Правила — GIT_WORKFLOW.md.
import json
import re
import sys

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

if reasons:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "; ".join(reasons) + ". Перечитай «Правила коммитов» в GIT_WORKFLOW.md."}},
        ensure_ascii=False))
