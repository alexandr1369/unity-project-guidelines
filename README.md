# Общие правила Unity-проектов

Плагин `unity-project-guidelines` для Claude Code и Codex: навык с командными правилами
и хуки, которые проверяют часть правил во время работы агента. Порядок чтения правил —
в [SKILL.md](skills/unity-project-guidelines/SKILL.md).

## Установка

Требуются `python3` и `git`; `dotnet` — для проверки сборки перед push.
Установку можно поручить агенту: он выполняет шаги ниже для каждого установленного агента.

Claude Code:

```sh
claude plugin marketplace add alexandr1369/unity-project-guidelines
claude plugin install unity-project-guidelines@unity-project-guidelines
```

Codex:

```sh
codex plugin marketplace add alexandr1369/unity-project-guidelines
codex plugin add unity-project-guidelines@unity-project-guidelines
```

В Codex после установки хуки разрешаются один раз: Settings → Hooks → Unity Project
Guidelines → Trust all (в консоли — команда `/hooks`). Навык и хуки работают с новой сессии.

## Обновление

```sh
claude plugin marketplace update unity-project-guidelines
claude plugin update unity-project-guidelines@unity-project-guidelines
codex plugin marketplace upgrade unity-project-guidelines
codex plugin add unity-project-guidelines@unity-project-guidelines
```

## Хуки

- Перед `git commit` — механические проверки кода и коммита, нарушение блокирует коммит.
- Перед `git push` — сборка `.csproj` проекта, ошибка сборки блокирует push.
- После `git push` и `gh pr create` — напоминание сверить описание открытых PR.
- На просьбу дать вопросы ГД — правило понятных вопросов; на «перепроверь» — список самопроверки.
- Перед командой Unity MCP — запрет сборки без защиты от повтора.

## Подключение проекта

В корне Unity-репозитория лежат побайтово одинаковые `AGENTS.md` и `CLAUDE.md` с этим блоком
и особенностями проекта:

```md
## Общие Unity-правила

Перед работой в этом проекте обязательно используй навык `unity-project-guidelines` из
одноимённого плагина: прочитай его `SKILL.md`, базовые и подходящие к задаче профильные
документы. Ссылки внутри навыка разрешаются от папки его `SKILL.md`.
Если навыка нет в списке, до изменений проекта сообщи, что плагин не установлен
(установка — README https://github.com/alexandr1369/unity-project-guidelines);
не считай общие правила прочитанными.
Особенности ниже дополняют общие правила. Изменяй `AGENTS.md` и `CLAUDE.md` вместе,
сохраняя их побайтовое равенство.
```
