# Общие правила Unity-проектов

`unity-project-guidelines` — общий навык для Codex и Claude Code. [SKILL.md](SKILL.md)
задаёт порядок работы и направляет к профильным документам. В них записаны командные
соглашения, а особенности отдельного проекта остаются в его `AGENTS.md` и `CLAUDE.md`.

## Подключение навыка

Навык хранится в отдельном [Git-репозитории](https://github.com/alexandr1369/unity-project-guidelines).
Оба агента используют один локальный клон через ссылки на папку `unity-project-guidelines`.
Пример для macOS и Linux:

```sh
mkdir -p "$HOME/agent-skills" "$HOME/.agents/skills" "$HOME/.claude/skills"
git clone https://github.com/alexandr1369/unity-project-guidelines.git "$HOME/agent-skills/unity-project-guidelines"
ln -s "$HOME/agent-skills/unity-project-guidelines" "$HOME/.agents/skills/unity-project-guidelines"
ln -s "$HOME/agent-skills/unity-project-guidelines" "$HOME/.claude/skills/unity-project-guidelines"
```

Если установлен один агент, нужна только его ссылка. На Windows вместо символьных ссылок
подходят junction-ссылки; для WSL навык устанавливается отдельно внутри WSL. При уже
существующем подключении сначала проверяется его источник, чтобы не создать вторую копию.

## Подключение проекта

В корне Unity-репозитория находятся побайтово одинаковые `AGENTS.md` и `CLAUDE.md`.
Они требуют использовать навык и описывают особенности конкретного проекта: пути,
наличие Entitas, плагины и правила Git. Пример общего блока:

```md
## Общие Unity-правила

В проекте применяется навык `unity-project-guidelines`.
Перед изменениями агент читает `SKILL.md`, `CODE_STYLE.md`, `GIT_WORKFLOW.md`
и профильные документы по задаче.
Codex: `~/.agents/skills/unity-project-guidelines/SKILL.md`.
Claude Code: `~/.claude/skills/unity-project-guidelines/SKILL.md`.
Если навык недоступен, агент сообщает об этом до изменений проекта.
`AGENTS.md` и `CLAUDE.md` обновляются вместе и остаются побайтово одинаковыми.
```

Общие правила изменяются в профильных документах этого репозитория; правила одной игры —
в её паре `AGENTS.md` и `CLAUDE.md`. Папка `scripts/git-hooks` содержит необязательную
проверку равенства этой пары.

## Обновление и проверка

Обновление локального клона выполняется через `git pull`. После подключения или обновления
в новой сессии агента проверяются доступность `SKILL.md` и чтение профильных документов.

Для передачи без Git возможен ZIP со всеми корневыми `*.md` и каталогом `scripts`, без
`.git`, локальных задач и файлов Unity-проектов. Такой архив остаётся снимком и сам не
обновляется.
