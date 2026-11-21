import json
import os
import sys
from pathlib import Path

# Claude Code фиксирует CLAUDE_PLUGIN_ROOT при запуске сессии, а `claude plugin update` ставит версию в новую папку.
# Хук запускает скрипт установленной версии, чтобы обновлённые проверки действовали в открытых сессиях без перезапуска.
scripts = Path("runtime", "scripts", "agent-hooks")
root = Path(__file__).resolve().parents[3]
script = root / scripts / sys.argv[1]
try:
    registry = json.loads((root.parents[3] / "installed_plugins.json").read_text())
    entries = registry["plugins"][f"{root.parent.name}@{root.parents[1].name}"]
    installed = Path(max(entries, key=lambda entry: entry["lastUpdated"])["installPath"]) / scripts / sys.argv[1]
    if installed.is_file():
        script = installed
except (OSError, ValueError, LookupError, TypeError):
    pass

command = ["bash", str(script)] if script.suffix == ".sh" else [sys.executable, "-B", str(script)]
os.execvp(command[0], command + sys.argv[2:])
