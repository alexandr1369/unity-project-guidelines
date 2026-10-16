#!/usr/bin/env python3
# Механические проверки C#-кода по правилам _guidelines. Запускается из commit-guard.py перед `git commit`
# ассистента и смотрит только индексированные изменения: нарушение в строках, которые коммит не трогает, не мешает.
# Каждое новое правило, которое можно проверить механически, добавляется сюда (AI_COLLABORATION_GUIDELINES.md,
# «Проверка нового правила»).
import json
import os
import re
import subprocess
import sys


def run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True).stdout


def added_lines(cwd, path):
    numbers = set()
    for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", run(["git", "diff", "--cached", "-U0", "--", path], cwd), re.M):
        start, count = int(match.group(1)), int(match.group(2) or "1")
        numbers.update(range(start, start + count))
    return numbers


def executable_lines(lines, start, end):
    return sum(1 for line in lines[start:end] if line.strip() and not line.strip().startswith("//") and line.strip() not in ("{", "}"))


def return_spacing(lines, added):
    # CODE_STYLE.md: перед return пустая строка, если в блоке больше двух исполняемых строк; при двух — вплотную.
    for index, line in enumerate(lines):
        text = line.strip()
        if not (text == "return;" or text.startswith("return ") or text.startswith("yield break")):
            continue
        indent = len(line) - len(line.lstrip())
        previous = index - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous < 0 or (len(lines[previous]) - len(lines[previous].lstrip()) < indent and lines[previous].strip() != "{"):
            continue
        start = index
        while start >= 0 and not (lines[start].strip() == "{" and len(lines[start]) - len(lines[start].lstrip()) < indent):
            start -= 1
        end = index
        while end < len(lines) and not (lines[end].strip() == "}" and len(lines[end]) - len(lines[end].lstrip()) < indent):
            end += 1
        if start < 0 or not added.intersection(range(start + 1, end + 2)):
            continue
        count = executable_lines(lines, start + 1, index) + 1
        blank = not lines[index - 1].strip()
        if count <= 2 and blank:
            yield index + 1, "перед return лишняя пустая строка: в блоке две строки"
        elif count > 2 and not blank:
            yield index + 1, "перед return нужна пустая строка: в блоке больше двух строк"


def config_extension(lines, added):
    # OPTIONAL_ENTITAS.md: extension не заводят на конфиг и данные — это метод самого конфига.
    for number in sorted(added):
        if number <= len(lines) and re.search(r"static\s+[^(]*\(\s*this\s+\w+(Config|Data|Definition)\b", lines[number - 1]):
            yield number, "extension на конфиг или данные: сделай методом самого конфига"


def component_name(lines, added):
    # OPTIONAL_ENTITAS.md, «Канон именования»: ожидание называют <Факт>Required, <Verb><Object>Request,
    # <Domain>Processing или прилагательным состояния (Unclaimed), а не Pending и его синонимами.
    for number in sorted(added):
        if number > len(lines):
            continue
        match = re.search(r"class (\w+)\s*:\s*I\w*Component\b", lines[number - 1])
        word = match and re.search(r"Pending|Awaiting|Queued|Dirty|InProgress", match.group(1))
        if word:
            yield number, f"в имени компонента {match.group(1)} запрещено {word.group(0)}: <Факт>Required, <Verb><Object>Request, <Domain>Processing или прилагательное состояния"


def wrapped_return(lines, added):
    # CODE_STYLE.md, «Методы»: два условия переносят, только когда строка не влезает из-за длинного обращения,
    # и тогда обращение сначала уходит в локальную переменную. return и expression-bodied метод с двумя
    # условиями остаются одной строкой; три и больше условий по одному на строку — норма предиката.
    starts = set()
    for number in sorted(added):
        if number > len(lines) or not re.search(r"(&&|\|\||\?)\s*$", lines[number - 1]):
            continue
        start = number - 1
        while start > 0 and re.search(r"(&&|\|\||\?|=>)\s*$", lines[start - 1]):
            start -= 1
        head = lines[start].strip()
        if head.startswith("return ") or re.match(r"(public|private|protected|internal)\b.*\w\(.*\)\s*=>$", head):
            starts.add(start)
    for start in sorted(starts):
        end = start
        while end + 1 < len(lines) and re.search(r"(&&|\|\||\?|=>)\s*$", lines[end]):
            end += 1
        wraps = sum(1 for line in lines[start:end] if re.search(r"(&&|\|\||\?)\s*$", line))
        if wraps == 1:
            yield start + 1, "два условия перенесены на строки: вынеси длинное обращение в локальную переменную, условие — одной строкой"


def vertical_call(lines, added):
    # CODE_STYLE.md, «Методы»: в месте вызова аргументы не раскладываются по одному на строку; исключение — состав
    # matcher-а (AllOf/AnyOf/NoneOf) и объявления сигнатур.
    for number in sorted(added):
        if number > len(lines):
            continue
        text = lines[number - 1].strip()
        if not text.endswith("(") or re.search(r"\b(AllOf|AnyOf|NoneOf)\($", text):
            continue
        if re.match(r"(public|private|protected|internal|static|override|abstract|virtual|async|delegate)\b", text):
            continue
        if re.match(r"[\w<>\[\],.?]+\s+\w+\($", text) and not re.search(r"[=.]", text.split("(")[0].split()[-1]):
            continue
        yield number, "аргументы вызова разложены по строкам: держи их в строке и переноси только хвост"


def multiline_expression_method(lines, added):
    # CODE_STYLE.md, «Форматирование» и «Методы»: метод через => — только когда всё тело одна строка после =>;
    # иначе тело метода в фигурных скобках. Свойства-условия (CanX =>) сюда не попадают.
    for number in sorted(added):
        if number >= len(lines):
            continue
        if re.match(r"\s*(public|private|protected|internal)\b.*\w(<[^>]*>)?\(.*\)\s*=>\s*$", lines[number - 1]) and not lines[number].rstrip().endswith(";"):
            yield number, "метод через => занимает несколько строк: перепиши телом в фигурных скобках"


CHECKS = (return_spacing, config_extension, component_name, wrapped_return, vertical_call, multiline_expression_method)


VOCABULARY_EXCLUDES = [":!*/Generated/*", ":!*/Tests/*", ":!*/Editor/*", ":!*/Plugins/*"]


def stem(word):
    word = word.lower()
    for suffix in ("ing", "ed", "es", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def camel_words(name):
    return re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|\d+", name)


def approved_words_path(cwd):
    return os.path.join(cwd, run(["git", "rev-parse", "--git-common-dir"], cwd).strip(), "approved-type-words.json")


def approved_words(cwd):
    try:
        return set(json.load(open(approved_words_path(cwd), encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def approve_words(words, cwd):
    approved = approved_words(cwd) | {stem(word) for word in words}
    json.dump(sorted(approved), open(approved_words_path(cwd), "w", encoding="utf-8"), indent=1)
    print("Одобрены слова: " + ", ".join(words))


def new_type_words(cwd):
    # CODE_STYLE.md, «Перед тем как назвать…»: слово в имени нового типа берут из модуля, MonoBehaviour фичи или того,
    # что видит игрок. Слова нет в коде проекта до коммита — оно новое, и вводит его только владелец.
    identifiers = run(["git", "grep", "-h", "-o", "-I", "-E", "[A-Za-z][A-Za-z0-9_]*", "HEAD", "--", "*.cs"] + VOCABULARY_EXCLUDES, cwd)
    vocabulary = {stem(word) for identifier in set(identifiers.split()) for word in camel_words(identifier)}
    vocabulary |= approved_words(cwd)
    found = []
    for path in run(["git", "diff", "--cached", "--name-only", "--diff-filter=AM"], cwd).split():
        if not path.endswith(".cs") or any(part in path for part in ("/Generated/", "/Tests/", "/Editor/")):
            continue
        lines = run(["git", "show", f":{path}"], cwd).split("\n")
        for number in sorted(added_lines(cwd, path)):
            match = number <= len(lines) and re.search(r"\b(?:class|struct|enum|interface)\s+(\w+)", lines[number - 1])
            if not match:
                continue
            words = [word for word in camel_words(match.group(1)) if not word.isdigit() and stem(word) not in vocabulary]
            if words:
                found.append(f"{path}:{number}: новое слово {', '.join(words)} в имени {match.group(1)}: возьми слово из модуля, "
                             f"его MonoBehaviour или того, что видит игрок; новое понятие — согласуй с владельцем и после его "
                             f"«да» одобри: code-checks.py --approve-word {' '.join(words)}")
    return found


def nested_feature(cwd):
    # OPTIONAL_ENTITAS.md, «Состав и порядок фич»: фичи добавляет только корневая фича, которую создаёт состояние;
    # фича модуля содержит только системы.
    found = []
    for path in run(["git", "diff", "--cached", "--name-only", "--diff-filter=AM"], cwd).split():
        if not path.endswith("Feature.cs") or any(part in path for part in ("/Generated/", "/Tests/")):
            continue
        lines = run(["git", "show", f":{path}"], cwd).split("\n")
        owner = next((m.group(1) for m in (re.search(r"class (\w+Feature)\s*:\s*Feature\b", line) for line in lines) if m), None)
        if not owner:
            continue
        creations = run(["git", "grep", "--cached", "-h", f"Create<{owner}>(", "--", "*.cs", ":!*/Tests/*"], cwd).splitlines()
        if any("Add(" not in line for line in creations):
            continue
        for number in sorted(added_lines(cwd, path)):
            match = number <= len(lines) and re.search(r"Create<(\w+Feature)>\(", lines[number - 1])
            if match:
                found.append(f"{path}:{number}: фича {match.group(1)} внутри фичи модуля {owner}: регистрируй её в корневой фиче, рядом, в нужной фазе")
    return found


def violations(cwd):
    found = []
    for path in run(["git", "diff", "--cached", "--name-only", "--diff-filter=AM"], cwd).split():
        if not path.endswith(".cs") or "/Generated/" in path:
            continue
        lines = run(["git", "show", f":{path}"], cwd).split("\n")
        added = added_lines(cwd, path)
        for check in CHECKS:
            found.extend(f"{path}:{number}: {message}" for number, message in check(lines, added))
    return found + new_type_words(cwd) + nested_feature(cwd)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--approve-word":
        approve_words(sys.argv[2:], os.getcwd())
    else:
        print("\n".join(violations(sys.argv[1] if len(sys.argv) > 1 else ".")))
