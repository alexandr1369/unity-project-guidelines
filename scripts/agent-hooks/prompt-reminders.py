#!/usr/bin/env python3
# Хук Claude Code и Codex (UserPromptSubmit): по словам владельца кладёт агенту нужный раздел правил
# из AI_COLLABORATION_GUIDELINES.md — правило понятных вопросов на просьбу дать вопросы к ГД или к себе,
# список самопроверки на «перепроверь». Туда же кладёт просроченный архитектурный аудит:
# счётчик читает audit_due.py, память ассистента его не ведёт.
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import audit_due

QUESTIONS_SECTION = "## Вопросы геймдизайну и владельцу"
SELF_CHECK_SECTION = "## Самопроверка перед отчётом"
QUESTION = re.compile(r"вопрос", re.I)
GAME_DESIGN = re.compile(r"(?<!\w)(?:г\.?д|gd|гэдэ)(?!\w)|геймдиз", re.I)
QUESTIONS = re.compile(r"вопрос(?:ы|ов|ам|ами|ах)", re.I)
REQUEST = re.compile(r"дай|кинь|кидай|закин|отда|продублир|составь|собер|запиши|задавай|задай|остал|какие|сократи|перепиш", re.I)
SELF_CHECK = re.compile(r"перепров|провер\w* себя|сам себя провер", re.I)
WINDOW = 70


def asks_questions(prompt):
    for match in QUESTION.finditer(prompt):
        window = prompt[max(0, match.start() - WINDOW):match.end() + WINDOW]
        if GAME_DESIGN.search(window):
            return True
        if QUESTIONS.match(prompt, match.start()) and REQUEST.search(window):
            return True
    return False


def section(title):
    guidelines = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "skills", "unity-project-guidelines", "AI_COLLABORATION_GUIDELINES.md")
    rules = []
    for line in open(guidelines, encoding="utf-8"):
        if not rules and line.strip() != title:
            continue
        if rules and line.startswith("## "):
            break
        rules.append(line.rstrip())
    return "\n".join(rules).strip()


data = json.load(sys.stdin)
prompt = data.get("prompt") or ""
reminders = []
session = data.get("session_id") or data.get("sessionId") or ""
audit = audit_due.context(data.get("cwd") or os.getcwd(), session, refresh=False, repeat=False)
if audit:
    reminders.append(audit)
if asks_questions(prompt):
    reminders.append("Владелец просит вопросы. Каждый вопрос до отдачи проверь по правилам ниже глазами ГД, который видит его впервые.\n\n"
                     + section(QUESTIONS_SECTION))
if SELF_CHECK.search(prompt):
    reminders.append("Владелец просит перепроверить. Пройди список ниже по итоговому диффу целиком, с первого пункта, и назови, что нашлось.\n\n"
                     + section(SELF_CHECK_SECTION))
if not reminders:
    sys.exit(0)

print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n\n".join(reminders)}},
                 ensure_ascii=False))
