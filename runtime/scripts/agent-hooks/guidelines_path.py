from pathlib import Path


def guidelines_path(name):
    runtime = Path(__file__).resolve().parents[2]
    directories = (
        runtime.parent / "skills" / "unity-project-guidelines",
        runtime.parent.parent / "unity-project-guidelines" / "local" / "skills" / "unity-project-guidelines",
    )
    for directory in directories:
        document = directory / name
        if document.is_file():
            return str(document)

    raise FileNotFoundError(
        "Не найден документ " + name + ". Установи или обнови основной плагин unity-project-guidelines; "
        "правила ожидались в " + ", ".join(str(directory) for directory in directories))
