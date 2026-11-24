"""Напоминания после возможного слияния PR: следующий PR владельцу первым шагом и сверка карточек."""
from guidelines_path import guidelines_path

NEXT_REVIEW_RULE = "- Первым шагом после подтверждённого merge"


def next_review_reminder():
    workflow = guidelines_path("GIT_WORKFLOW.md")
    with open(workflow, encoding="utf-8") as source:
        rule = next((line.strip() for line in source if line.startswith(NEXT_REVIEW_RULE)), "")
    return "Следующий PR — владельцу первым шагом, " + workflow + ", «После merge»:\n\n" + rule


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
