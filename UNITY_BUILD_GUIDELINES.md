# Unity Build Guidelines

Этот документ фиксирует практические правила по сборкам Unity-проекта.

## Чистая сборка

`Clean Build` использовать, когда player build не отражает последние изменения, особенно после правок в:

- Unity license, Unity Hub license state или версии Editor.
- `Player Settings`, splash screen, scripting backend, stripping, compression или build symbols.
- Android Gradle templates, SDK/NDK/JDK, plugins, Firebase, Ads, analytics SDK или generated local Maven repositories.
- Build size cleanup, asset stripping, `Resources`/Addressables-like content, render pipeline settings или shader settings.
- Unity version upgrades или package upgrades, которые затрагивают player/runtime code.

Обычная Unity-сборка может переиспользовать cached player data. Если в `Editor.log` есть:

```text
Information on used Assets is not available, since player data was not rebuilt.
Do a clean build to view the Asset build report information.
```

значит player data не был полностью пересобран, даже если UI-действие выглядело как clean/rebuild. Такой APK/AAB не считать доказательством, что настройки реально применились.

## Сброс кешей Android

Если `Clean Build` всё равно переиспользует старый Android player data, закрыть Unity и удалить generated cache folders:

```text
src/<проект>/Library/PlayerDataCache/Android
src/<проект>/Library/Bee/Android
src/<проект>/Library/Bee/artifacts/Android
src/<проект>/Library/BuildPlayerData
```

После этого открыть Unity заново и запустить `Build And Run` или собрать свежий APK/AAB.

## Чеклист симптомов

Сначала пробовать `Clean Build`, если:

- Watermark, splash screen, icon, version, signing, permission или manifest change на девайсе выглядят по-старому.
- APK/AAB size не меняется после удаления assets или SDK.
- Android install запускает билд, который ведёт себя как старая сборка.
- Build logs показывают, что player data was not rebuilt.

## Сборки через MCP

- Сервер Unity MCP повторяет команду до шести раз, если Unity не ответил за отведённое время, и Unity выполняет все копии по очереди. Долгая команда в `execute_code` (сборка, переключение платформы) поэтому запускается несколько раз подряд, а iOS-сборка панели перед экспортом удаляет папку прошлого экспорта.
- Любой такой вызов начинается с защиты от повтора: уникальный id запуска в `SessionState`, и если он уже стоит — немедленный выход. Без неё хук `mcp-long-command-guard.py` команду не пропустит.
- Ответ «Timeout receiving Unity response» не значит, что команда не выполнилась. Результат проверять маркером в `Editor.log` и считать маркеры `start`, а не вызывать команду ещё раз.
- Сборку запускать той же функцией, что и кнопка панели проекта. Свои шаги поверх неё не добавлять. Модальное окно в конце штатное: его закрывает владелец.
- Файлы на рабочем столе проверять из Unity (`System.IO` в `execute_code`) или точечным чтением конкретных путей. Список папки из песочницы ассистента бывает неполным.
