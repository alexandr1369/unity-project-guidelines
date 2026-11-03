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

В Codex хуки должны быть включены и доверены текущей версии: Settings → Hooks → Unity Project
Guidelines → Review / Trust (в консоли — команда `/hooks`). После изменения скриптов плагина
доверие может потребоваться повторно: `Modified` или `Untrusted` означает, что хук не выполняется.
Сначала прочитать изменённые скрипты и подтвердить именно текущую версию; требование доверия не отключать.

После установки или переноса проверь фактический вызов хуков по разделу «Уровни контроля»
в [AI_COLLABORATION_GUIDELINES.md](skills/unity-project-guidelines/AI_COLLABORATION_GUIDELINES.md).
Обнаруженный навык и доверенный хук сами по себе не подтверждают автоматическую защиту.

## Обновление

```sh
claude plugin marketplace update unity-project-guidelines
claude plugin update unity-project-guidelines@unity-project-guidelines
codex plugin marketplace upgrade unity-project-guidelines
codex plugin add unity-project-guidelines@unity-project-guidelines
```

После обновления проверить статус через Hooks или `hooks/list` загрузчика той версии Codex,
в которой идёт работа: нужны `enabled: true` и `trustStatus: trusted`, без ошибок загрузки.
Затем безопасной пробой `git add --all --dry-run` проверить запрет до выполнения команды и
пробным коммитом в отдельном временном репозитории — блокировку нарушения кодстайла.
Успешный ручной запуск скрипта эту проверку не заменяет. Порядок доверия описан в
[документации Codex](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks).

## Хуки

- Перед `git commit` — механические проверки кода и коммита, нарушение блокирует коммит.
- Перед `git push` — сборка `.csproj` проекта, ошибка сборки блокирует push.
- После `git push` и `gh pr create` — напоминание сверить описание открытых PR.
- На просьбу дать вопросы ГД — правило понятных вопросов; на «перепроверь» — список самопроверки; на замечания и работу с ревью — обязательное обновление и проверка правил самим ассистентом.
- Перед командой Unity MCP — запрет сборки без защиты от повтора.

## Проверки плагина

```sh
python3 scripts/agent-hooks/test_hooks.py
```

Тесты используют стандартную библиотеку Python, временные Git-репозитории и поддельную
сборку: Unity и модель не запускаются. Проверяются запреты, разрешённые команды, выбор
репозитория и формат ответов. Этот же прогон выполняется в GitHub Actions при push и PR.
Он проверяет скрипты; фактическое подключение к агенту после обновления проверяется отдельно,
как описано в разделе «Обновление».

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
