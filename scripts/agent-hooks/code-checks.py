#!/usr/bin/env python3
# Механические проверки C#-кода по правилам _guidelines. Запускается из commit-guard.py перед `git commit`
# ассистента и смотрит только индексированные изменения: нарушение в строках, которые коммит не трогает, не мешает.
# Каждое новое правило, которое можно проверить механически, добавляется сюда (AI_COLLABORATION_GUIDELINES.md,
# «Проверка нового правила»).
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


CHECKS = (return_spacing, config_extension)


def violations(cwd):
    found = []
    for path in run(["git", "diff", "--cached", "--name-only", "--diff-filter=AM"], cwd).split():
        if not path.endswith(".cs") or "/Generated/" in path:
            continue
        lines = run(["git", "show", f":{path}"], cwd).split("\n")
        added = added_lines(cwd, path)
        for check in CHECKS:
            found.extend(f"{path}:{number}: {message}" for number, message in check(lines, added))
    return found


if __name__ == "__main__":
    print("\n".join(violations(sys.argv[1] if len(sys.argv) > 1 else ".")))
