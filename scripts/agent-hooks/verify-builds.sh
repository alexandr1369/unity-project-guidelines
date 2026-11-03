#!/usr/bin/env bash
# Не даёт ассистенту запушить ветку, пока свой код проекта не собирается.
# Общий для всех проектов: сам находит Code*.csproj и Tests*.csproj в src/*/ (Unity)
# и src/*/Code/Code.csproj, src/*/Tests/Tests.csproj (.NET-решения вне Unity).
# Вендорные и сгенерированные Unity сборки не трогает.

set -uo pipefail

INPUT="$(cat)"
ROOT="$(printf '%s' "$INPUT" | python3 "$(dirname "$0")/command_directory.py")"
[ -z "$ROOT" ] && exit 0

command -v dotnet >/dev/null 2>&1 || exit 0

PROJECTS=$(ls "$ROOT"/src/*/Code.csproj "$ROOT"/src/*/Code.*.csproj "$ROOT"/src/*/Tests.csproj "$ROOT"/src/*/Tests.*.csproj "$ROOT"/src/*/Code/Code.csproj "$ROOT"/src/*/Tests/Tests.csproj 2>/dev/null)
[ -z "$PROJECTS" ] && exit 0

LOGS=$(mktemp -d)
FAILED=""

while IFS= read -r proj; do
    name=$(basename "$proj" .csproj)
    if ! dotnet build "$proj" -v q --nologo >"$LOGS/$name.log" 2>&1; then
        FAILED="$FAILED $name"
    fi
done <<<"$PROJECTS"

if [ -n "$FAILED" ]; then
    ERRORS=$(grep -h "error CS\|error MSB" "$LOGS"/*.log 2>/dev/null | head -3 | tr '\n' ' ')
    python3 - "$FAILED" "$ERRORS" <<'PY'
import json
import sys

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse", "permissionDecision": "deny",
    "permissionDecisionReason": f"Сборка падает:{sys.argv[1]}. Первые ошибки: {sys.argv[2]} Почини и собери заново перед push."}},
    ensure_ascii=False))
PY
    rm -rf "$LOGS"
    exit 0
fi

rm -rf "$LOGS"
