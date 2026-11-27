#!/usr/bin/env python3
# Хук Claude Code и Codex (PostToolUse): после чтения карточки трекера кладёт агенту рядом с её текстом раздел
# «Что брать из материалов геймдизайна» из AI_COLLABORATION_GUIDELINES.md. Карточку пишет нейросеть ГД, которая
# проект не видела, поэтому каждый пункт до кода сверяется с гайдлайнами и архитектурой и делается самым простым способом.
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from guidelines_path import guidelines_section

MATERIALS_SECTION = "## Что брать из материалов геймдизайна"
CARD_READ_TOOL = re.compile(r"trello\w*read\w*card", re.I)
CARD_URL = re.compile(r"trello\.com/c/", re.I)

data = json.load(sys.stdin)
tool = data.get("tool_name") or ""
tool_input = data.get("tool_input") or {}
card_read = CARD_READ_TOOL.search(tool) and tool_input.get("action", "get") == "get"
card_opened = tool.endswith("navigate") and CARD_URL.search(json.dumps(tool_input, ensure_ascii=False))
if not card_read and not card_opened:
    sys.exit(0)

reminder = ("Прочитана карточка трекера. До кода, макетов и ветки раздели её на 200: каждый пункт сначала сверь с гайдлайнами, "
            "архитектурой и аналогом в проекте и делай самым простым и чистым способом, который даёт игроку главное. "
            "Деталь, ради которой в коде появятся своё состояние, ожидание или синхронизация, упрости или отложи.\n\n"
            + guidelines_section(MATERIALS_SECTION))
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": reminder}}, ensure_ascii=False))
