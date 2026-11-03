#!/usr/bin/env python3
# Хук Claude Code (PreToolUse на execute_code моста Unity MCP): сервер моста повторяет команду до шести раз,
# если Unity не ответил вовремя, и Unity выполняет все копии по очереди. Сборку и переключение платформы
# без защиты от повтора хук не пропускает. Правило — UNITY_BUILD_GUIDELINES.md, раздел «Сборки через MCP».
import json
import re
import sys

data = json.load(sys.stdin)
tool_input = data.get("tool_input") or {}
code = tool_input.get("code") or ""
if tool_input.get("action", "execute") != "execute" or not code:
    sys.exit(0)

LONG_OPERATIONS = r"BuildPipeline\.BuildPlayer|SwitchActiveBuildTarget|TryPrepare(?:IOS|Android)Build|\"Build(?:IOS|Android\w*)\""
if not re.search(LONG_OPERATIONS, code):
    sys.exit(0)

if re.search(r"SessionState\.GetBool\(", code) and re.search(r"SessionState\.SetBool\(", code):
    sys.exit(0)

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": (
        "Сборка или переключение платформы в execute_code без защиты от повтора: при таймауте мост MCP "
        "отправит ту же команду ещё до пяти раз, и каждая копия выполнится (iOS-сборка при этом сначала удаляет "
        "папку экспорта). Первой строкой после проверки пути поставь: "
        "`string runId = \"<уникальный id запуска>\"; if (SessionState.GetBool(runId, false)) return \"duplicate\"; "
        "SessionState.SetBool(runId, true);`. После таймаута считай маркеры start в Editor.log, а не повторяй вызов. "
        "Правило — UNITY_BUILD_GUIDELINES.md, «Сборки через MCP».")}},
    ensure_ascii=False))
