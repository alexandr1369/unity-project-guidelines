#!/usr/bin/env python3
# Хук Claude Code и Codex (UserPromptSubmit): по словам владельца кладёт агенту нужный раздел правил
# из AI_COLLABORATION_GUIDELINES.md — правило понятных вопросов на просьбу дать вопросы к ГД или к себе
# и на просьбу дать, что спросить у другого человека, или сообщение для него,
# список самопроверки на «перепроверь», выводы из замечаний при работе с ревью и фильтр материалов ГД на разговор о них.
# К каждому сообщению кладёт заголовки разделов «Отложено из ГД» и «Отложено из МР» из TASKS.md, чтобы задачу
# сверяли с отложенным.
# На сообщение о слиянии кладёт переключение владельца на следующий PR первым шагом и сверку карточек.
# На сообщение о карточке трекера или о взятии задачи кладёт раздел о карточках трекера: перевод в «В работе» — первым действием.
# Туда же кладёт просроченный архитектурный аудит: счётчик читает audit_due.py, память ассистента его не ведёт.
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import audit_due
from guidelines_path import guidelines_path
from merge_reminders import next_review_reminder, tracker_reminder

QUESTIONS_SECTION = "## Вопросы геймдизайну и владельцу"
SELF_CHECK_SECTION = "## Самопроверка перед отчётом"
FEEDBACK_SECTION = "## Выводы из замечаний владельца"
MATERIALS_SECTION = "## Что брать из материалов геймдизайна"
TRACKER_SECTION = "## Карточки трекера"
DEFERRED_SECTIONS = ("## Отложено из ГД", "## Отложено из МР")
QUESTION = re.compile(r"вопрос", re.I)
GAME_DESIGN = re.compile(r"(?<!\w)(?:г\.?д|gd|гэдэ)(?!\w)|геймдиз", re.I)
MATERIALS = re.compile(r"карточк|trello|трелл|(?<!\w)тз(?!\w)", re.I)
TRACKER = re.compile(r"(?<!\w)трел|trello|карточк", re.I)
TASK = re.compile(r"таск", re.I)
TAKE = re.compile(r"займ|возьм|(?<!\w)бер(?:и|ём|ем)(?!\w)|взя|приступ|в\s*работу|(?<!\w)(?:пере)?тян|(?<!\w)с?делай", re.I)
QUESTIONS = re.compile(r"вопрос(?:ы|ов|ам|ами|ах)", re.I)
REQUEST = re.compile(r"дай|кинь|кидай|закин|отда|продублир|составь|собер|запиши|задавай|задай|остал|какие|сократи|перепиш", re.I)
MESSAGE_FOR_SOMEONE = re.compile(r"(?<!\w)(?:что|чего|какую|какие|какой|кого)\s+(?:(?:мне|нам|там|тут|ещ[её]|именно|надо|нужно|можно|стоит|лучше|бы)\s+){0,3}"
                                 r"спрос(?:ить|им)(?!\w)"
                                 r"|(?<!\w)(?:пере)?спрос(?:ить|и|им)\s+у(?!\w)"
                                 r"|сообщени\w*\s+(?:для|ему|ей|им|тому)(?!\w)|(?<!\w)(?:ему|ей|им)\s+сообщени"
                                 r"|(?<!\w)(?:что|как)\s+(?:мне\s+)?(?:ему|ей|им)\s+(?:написать|сказать|ответить)(?!\w)", re.I)
SELF_CHECK = re.compile(r"перепров|провер\w* себя|сам себя провер", re.I)
FEEDBACK = re.compile(r"\b(?:мр|mr|pr|review|comments?)\b|ревью|замечан|коммент|кодстайл|косяк|ошибк|подсвет|костыл|правил|сам себя обуч", re.I)
PR_COMPLETION = re.compile(r"\b(?:(?:с|за)?м[её]р(?:дж|ж)(?:ил|нул)[аи]?|влил[аи]?|закрыл[аи]?|merged|closed)\b", re.I)
PR_REFERENCE = re.compile(r"\b(?:мр|mr|pr)\b|#?\d+", re.I)
WINDOW = 70


def asks_questions(prompt):
    for match in QUESTION.finditer(prompt):
        window = prompt[max(0, match.start() - WINDOW):match.end() + WINDOW]
        if GAME_DESIGN.search(window):
            return True
        if QUESTIONS.match(prompt, match.start()) and REQUEST.search(window):
            return True
    return False


def mentions_tracker_task(prompt):
    if TRACKER.search(prompt):
        return True
    for match in TASK.finditer(prompt):
        if TAKE.search(prompt[max(0, match.start() - WINDOW):match.end() + WINDOW]):
            return True
    return False


def reports_pr_completion(prompt):
    for match in PR_COMPLETION.finditer(prompt):
        before = prompt[max(0, match.start() - WINDOW):match.start()]
        if re.search(r"\b(?:не|not|never)\s+(?:yet\s+)?$", before, re.I):
            continue
        window = prompt[max(0, match.start() - WINDOW):match.end() + WINDOW]
        if PR_REFERENCE.search(window):
            return True
    return False


def section(title):
    guidelines = guidelines_path("AI_COLLABORATION_GUIDELINES.md")
    rules = []
    for line in open(guidelines, encoding="utf-8"):
        if not rules and line.strip() != title:
            continue
        if rules and line.startswith("## "):
            break
        rules.append(line.rstrip())
    return "\n".join(rules).strip()


def deferred_reminder(cwd):
    root = audit_due.git_output(["git", "rev-parse", "--show-toplevel"], cwd)
    tasks = os.path.join(root, "TASKS.md") if root else ""
    if not os.path.isfile(tasks):
        return ""
    titles = {section: [] for section in DEFERRED_SECTIONS}
    current = None
    for line in open(tasks, encoding="utf-8"):
        if line.startswith("## "):
            current = titles.get(line.strip())
        elif current is not None and line.startswith("### "):
            current.append(line[4:].strip())
    found = [f"{section[3:]} в TASKS.md: " + "; ".join(entries) for section, entries in titles.items() if entries]
    if not found:
        return ""
    return (". ".join(found) + ". Если задача касается записи, сделай её в этой задаче целиком и удали запись тем же МР — "
            "AI_COLLABORATION_GUIDELINES.md, «Что брать из материалов геймдизайна» и «Список задач».")


data = json.load(sys.stdin)
prompt = data.get("prompt") or ""
reminders = []
session = data.get("session_id") or data.get("sessionId") or ""
cwd = data.get("cwd") or os.getcwd()
audit = audit_due.context(cwd, session, refresh=False, repeat=False)
if audit:
    reminders.append(audit)
deferred = deferred_reminder(cwd)
if deferred:
    reminders.append(deferred)
if reports_pr_completion(prompt):
    reminders.append(next_review_reminder())
    reminders.append(tracker_reminder())
elif mentions_tracker_task(prompt):
    reminders.append("Речь о задаче или карточке трекера. Если владелец даёт карточку в работу, первым действием, как только определён состав работы, "
                     "до кода, макетов и ветки выполни постоянное поручение проекта о статусах из CLAUDE.md/AGENTS.md и проверь колонку.\n\n"
                     + section(TRACKER_SECTION))
if asks_questions(prompt):
    reminders.append("Владелец просит вопросы. Каждый вопрос до отдачи проверь по правилам ниже глазами ГД, который видит его впервые.\n\n"
                     + section(QUESTIONS_SECTION))
elif MESSAGE_FOR_SOMEONE.search(prompt):
    reminders.append("Владелец просит, что спросить у другого человека, или сообщение для него. "
                     "До отдачи сверь текст с правилами ниже о готовом сообщении для пересылки.\n\n"
                     + section(QUESTIONS_SECTION))
if GAME_DESIGN.search(prompt) or MATERIALS.search(prompt):
    reminders.append("Речь о материалах ГД. Каждый пункт пропусти через фильтр ниже: нужное сейчас — лёгким способом, остальное — в отложенное.\n\n"
                     + section(MATERIALS_SECTION))
if SELF_CHECK.search(prompt):
    reminders.append("Владелец просит перепроверить. Пройди список ниже по итоговому диффу целиком, с первого пункта, и назови, что нашлось.\n\n"
                     + section(SELF_CHECK_SECTION))
if FEEDBACK.search(prompt):
    reminders.append("При обработке замечаний и объяснений владельца сам обновляй правила и проверяй их тем же заходом, что и фикс.\n\n"
                     + section(FEEDBACK_SECTION))
if not reminders:
    sys.exit(0)

print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n\n".join(reminders)}},
                 ensure_ascii=False))
