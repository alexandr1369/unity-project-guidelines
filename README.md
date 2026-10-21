# Общие правила Unity-проектов

Плагин `unity-project-guidelines` для Claude Code и Codex. Внутри навык с командными
соглашениями — код, архитектура, Git, ревью, UI, тесты — и хуки, которые часть правил
проверяют прямо во время работы агента. [SKILL.md](skills/unity-project-guidelines/SKILL.md)
задаёт порядок чтения и направляет к профильным документам. Особенности отдельного проекта
остаются в его `AGENTS.md` и `CLAUDE.md`.

## Установка

Проще всего попросить своего агента:

> Поставь плагин unity-project-guidelines по README
> https://github.com/alexandr1369/unity-project-guidelines — для Claude Code, Codex
> или обоих, смотря что у меня установлено.

Агент выполняет шаги ниже для каждого установленного у человека агента.

**Claude Code**

```sh
claude plugin marketplace add alexandr1369/unity-project-guidelines
claude plugin install unity-project-guidelines@unity-project-guidelines
```

В приложении Claude те же две команды набираются через `/plugin` в поле ввода, каждая
отдельным сообщением. Навык и хуки подхватываются в новой сессии.

**Codex**

```sh
codex plugin marketplace add alexandr1369/unity-project-guidelines
codex plugin add unity-project-guidelines@unity-project-guidelines
```

Затем запустить `codex`, открыть `/hooks`, разрешить хуки плагина и начать новый тред.
Приложение Codex после установки перезапускается.

**Перед установкой**

- Нужны `python3` и `git`. `dotnet` нужен хуку сборки перед push; без него хук молчит.
- Старую установку через ссылки убрать, иначе навык и хуки сработают дважды:
  папки-ссылки `~/.claude/skills/unity-project-guidelines` и
  `~/.agents/skills/unity-project-guidelines`, хуки со словом `agent-hooks` в
  `~/.claude/settings.json` и `~/.codex/hooks.json`.

## Обновление

Версия плагина — последний коммит `main`, отдельного номера нет.

```sh
claude plugin marketplace update unity-project-guidelines
claude plugin update unity-project-guidelines@unity-project-guidelines
```

```sh
codex plugin marketplace upgrade unity-project-guidelines
codex plugin add unity-project-guidelines@unity-project-guidelines
```

Новые правила действуют со следующей сессии агента.

## Удаление

```sh
claude plugin uninstall unity-project-guidelines@unity-project-guidelines
codex plugin remove unity-project-guidelines
```

## Что внутри

| Путь | Что это |
| --- | --- |
| `skills/unity-project-guidelines/` | Навык: `SKILL.md` и профильные документы |
| `hooks/` | Подключение хуков: `claude-hooks.json` и `codex-hooks.json` |
| `scripts/agent-hooks/commit-guard.py` | Перед `git commit`: не пускает коммит, который нарушает механически проверяемые правила кода и коммитов (`code-checks.py`) |
| `scripts/agent-hooks/verify-builds.sh` | Перед `git push`: собирает свои `.csproj` проекта и не пускает push, если сборка падает |
| `scripts/agent-hooks/pr-description-reminder.py` | После `git push` и `gh pr create`: просит сверить описание открытых PR; после коммита — назвать, откуда слова в новых именах |
| `scripts/agent-hooks/questions-reminder.py` | На просьбу дать вопросы ГД или владельцу: кладёт агенту правило понятных вопросов |
| `scripts/agent-hooks/mcp-long-command-guard.py` | Не пускает сборку и смену платформы через Unity MCP без защиты от повтора |
| `scripts/git-hooks/pre-commit` | Необязательный git-хук Unity-репозитория: сверка `AGENTS.md` и `CLAUDE.md` |
| `.claude-plugin/`, `.codex-plugin/`, `.agents/plugins/` | Манифесты плагина и маркетплейса для Claude Code и Codex |

## Подключение проекта

В корне Unity-репозитория лежат побайтово одинаковые `AGENTS.md` и `CLAUDE.md`. Они требуют
использовать навык и описывают особенности проекта: пути, наличие Entitas, плагины и правила
Git. Общий блок:

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

Необязательная проверка равенства пары подключается в клоне проекта:
`git config core.hooksPath scripts/git-hooks`, с копией `scripts/git-hooks/pre-commit`.

## Изменение правил

Общие правила меняются в профильных документах `skills/unity-project-guidelines/`, правила
одной игры — в её паре `AGENTS.md` и `CLAUDE.md`. После push в `main` установленный плагин
обновляется командами из раздела «Обновление».
