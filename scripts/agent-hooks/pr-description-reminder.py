#!/usr/bin/env python3
# Хук Claude Code и Codex (PostToolUse): после git push из task-ветки и после gh pr create/edit
# кладёт агенту в контекст правила описания PR. Текст берётся из GIT_WORKFLOW.md — у правила один владелец.
import json
import os
import re
import subprocess
import sys

data = json.load(sys.stdin)
tool_input = data.get("tool_input") or {}
command = tool_input.get("command") or tool_input.get("cmd") or ""
if isinstance(command, list):
    command = " ".join(command)

START = r"(?:^|[;&|(]\s*)"
is_push = re.search(START + r"git (?:-C \S+ )?push\b", command, re.M)
if not is_push and not re.search(START + r"gh pr (?:create|edit)\b", command, re.M):
    sys.exit(0)

target = re.search(START + r"(?:cd|git -C) (\S+)", command, re.M)
cwd = os.path.expanduser(target.group(1).strip("'\"")) if target else data.get("cwd") or os.getcwd()
branch = subprocess.run(["git", "-C", cwd, "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
if is_push and (branch in ("", "main", "master") or re.search(r"\bpush\b.*\b(?:main|master)\b", command)):
    sys.exit(0)

workflow = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "GIT_WORKFLOW.md")
rules = []
for line in open(workflow, encoding="utf-8"):
    if not rules and not line.startswith("- Описание PR по умолчанию"):
        continue
    if not line.strip():
        break
    rules.append(line.rstrip())

context = "Сверь описание PR с итоговым diff от base до head и перепиши его, если расходится. Правила GIT_WORKFLOW.md:\n"
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context + "\n".join(rules)}},
                 ensure_ascii=False))
