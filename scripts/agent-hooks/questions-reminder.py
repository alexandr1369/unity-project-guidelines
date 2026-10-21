#!/usr/bin/env python3
# Хук Claude Code и Codex (UserPromptSubmit): когда владелец просит вопросы к ГД или к себе, кладёт агенту
# раздел «Вопросы геймдизайну и владельцу» из AI_COLLABORATION_GUIDELINES.md.
import json
import os
import re
import sys

SECTION = "## Вопросы геймдизайну и владельцу"
QUESTION = re.compile(r"вопрос", re.I)
GAME_DESIGN = re.compile(r"(?<!\w)(?:г\.?д|gd|гэдэ)(?!\w)|геймдиз", re.I)
QUESTIONS = re.compile(r"вопрос(?:ы|ов|ам|ами|ах)", re.I)
REQUEST = re.compile(r"дай|кинь|кидай|закин|отда|продублир|составь|собер|запиши|задавай|задай|остал|какие|сократи|перепиш", re.I)
WINDOW = 70


def asks_questions(prompt):
    for match in QUESTION.finditer(prompt):
        window = prompt[max(0, match.start() - WINDOW):match.end() + WINDOW]
        if GAME_DESIGN.search(window):
            return True
        if QUESTIONS.match(prompt, match.start()) and REQUEST.search(window):
            return True
    return False


def questions_rules():
    guidelines = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "skills", "unity-project-guidelines", "AI_COLLABORATION_GUIDELINES.md")
    rules = []
    for line in open(guidelines, encoding="utf-8"):
        if not rules and line.strip() != SECTION:
            continue
        if rules and line.startswith("## "):
            break
        rules.append(line.rstrip())
    return "\n".join(rules).strip()


data = json.load(sys.stdin)
if not asks_questions(data.get("prompt") or ""):
    sys.exit(0)

print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": (
    "Владелец просит вопросы. Каждый вопрос до отдачи проверь по правилам ниже глазами ГД, который видит его впервые.\n\n"
    + questions_rules())}}, ensure_ascii=False))
