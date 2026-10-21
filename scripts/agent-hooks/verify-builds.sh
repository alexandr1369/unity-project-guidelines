#!/usr/bin/env bash
# Не даёт ассистенту запушить ветку, пока свой код проекта не собирается.
# Общий для всех проектов: сам находит Code*.csproj и Tests*.csproj в src/*/ (Unity)
# и src/*/Code/Code.csproj, src/*/Tests/Tests.csproj (.NET-решения вне Unity).
# Вендорные и сгенерированные Unity сборки не трогает.

set -uo pipefail

INPUT="$(cat)"
COMMAND="$(printf '%s' "$INPUT" | python3 -c 'import json,sys; i=json.load(sys.stdin).get("tool_input") or {}; c=i.get("command") or i.get("cmd") or ""; print(" ".join(c) if isinstance(c,list) else c)' 2>/dev/null)"

case "$COMMAND" in
    *"git push"*) ;;
    *) printf '{"suppressOutput":true}'; exit 0 ;;
esac

command -v dotnet >/dev/null 2>&1 || { printf '{"suppressOutput":true}'; exit 0; }

ROOT="${CLAUDE_PROJECT_DIR:-.}"
PROJECTS=$(ls "$ROOT"/src/*/Code.csproj "$ROOT"/src/*/Code.*.csproj "$ROOT"/src/*/Tests.csproj "$ROOT"/src/*/Tests.*.csproj "$ROOT"/src/*/Code/Code.csproj "$ROOT"/src/*/Tests/Tests.csproj 2>/dev/null)
[ -z "$PROJECTS" ] && { printf '{"suppressOutput":true}'; exit 0; }

LOGS=$(mktemp -d)
FAILED=""

while IFS= read -r proj; do
    name=$(basename "$proj" .csproj)
    if ! dotnet build "$proj" -v q --nologo >"$LOGS/$name.log" 2>&1; then
        FAILED="$FAILED $name"
    fi
done <<<"$PROJECTS"

if [ -n "$FAILED" ]; then
    ERRORS=$(grep -h "error CS\|error MSB" "$LOGS"/*.log 2>/dev/null | head -3 | tr '\n' ' ' | tr '"' "'")
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Сборка падает:%s. Первые ошибки: %s Почини и собери заново перед push."}}' "$FAILED" "$ERRORS"
    rm -rf "$LOGS"
    exit 0
fi

rm -rf "$LOGS"
printf '{"suppressOutput":true}'
