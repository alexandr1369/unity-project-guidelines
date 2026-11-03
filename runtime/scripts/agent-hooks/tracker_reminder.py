"""Напоминание сверить карточки после возможного слияния PR."""
from guidelines_path import guidelines_path


def tracker_reminder():
    document = guidelines_path("AI_COLLABORATION_GUIDELINES.md")
    rules = []
    with open(document, encoding="utf-8") as source:
        for line in source:
            if not rules and line.strip() != "## Карточки трекера":
                continue
            if rules and line.startswith("## "):
                break
            rules.append(line.rstrip())
    return ("Проверь связанные карточки трекера после фактического merge, включая слияние, которое владелец сделал сам в GitHub. "
            "Порядок проверки — " + document + ":\n\n" + "\n".join(rules).strip())
