#!/usr/bin/env python3
# Механические проверки C#-кода по правилам _guidelines. Запускается из commit-guard.py перед `git commit`
# ассистента и смотрит только индексированные изменения: нарушение в строках, которые коммит не трогает, не мешает.
# Каждое новое правило, которое можно проверить механически, добавляется сюда (AI_COLLABORATION_GUIDELINES.md,
# «Проверка нового правила»).
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter, namedtuple


def run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True).stdout


def changed_lines(cwd, path, base):
    numbers = set()
    for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", run(["git", "diff", "--cached", "-U0"] + base + ["--", path], cwd), re.M):
        start, count = int(match.group(1)), int(match.group(2) or "1")
        numbers.update(range(start, start + count))
    return numbers


def merge_head(cwd):
    return run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd).strip()


def added_lines(cwd, path):
    # При слиянии строки вливаемой ветки уже прошли проверку в ней: новыми считаются только строки, которых нет
    # ни в текущей ветке, ни во вливаемой, — разрешение конфликтов и правки поверх.
    numbers = changed_lines(cwd, path, [])
    merging = merge_head(cwd)
    return numbers & changed_lines(cwd, path, [merging]) if merging else numbers


CSHARP_TOKENS = re.compile(
    r'(?P<comment>//[^\n]*|/\*[\s\S]*?(?:\*/|\Z))'
    r'|(?P<raw_dollars>\$*)(?P<raw_quotes>"{3,})'
    r'|(?P<interpolated>\$@"|@\$"|\$")'
    r'|@"(?:""|[^"])*"|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\''
    r'|(?P<punctuation>::|[{}()[\]:])'
)


def csharp_string_comments(source, start, quotes=1, braces=1, verbatim=False):
    if quotes >= 3:
        closing = '"' * quotes
        opening = r"\{" + "{%d,}" % braces if braces else r"(?!)"
        tokens = re.compile(f"{closing}|{opening}")
    else:
        closing = '"'
        escape = '""' if verbatim else r"\\."
        tokens = re.compile(escape + r'|\{\{|"|\{')

    while token := tokens.search(source, start):
        start = token.end()
        if token[0] == closing:
            return start
        if token[0].startswith("{") and (quotes >= 3 or token[0] == "{"):
            start = yield from csharp_comments(source, start, braces)

    return len(source)


def csharp_comments(source, start=0, braces=0):
    nesting = []
    while token := CSHARP_TOKENS.search(source, start):
        start = token.end()
        if token["comment"]:
            yield token
        elif token["raw_quotes"]:
            start = yield from csharp_string_comments(source, start, len(token["raw_quotes"]), len(token["raw_dollars"]))
        elif token["interpolated"]:
            start = yield from csharp_string_comments(source, start, verbatim="@" in token[0])
        elif braces and token["punctuation"]:
            punctuation = token["punctuation"]
            if punctuation in ("(", "[", "{"):
                nesting.append(punctuation)
            elif nesting and punctuation in (")", "]", "}"):
                nesting.pop()
            elif not nesting and punctuation == "}" and source.startswith("}" * braces, token.start()):
                return token.start() + braces
            elif not nesting and punctuation == ":":
                end = source.find("}" * braces, start)
                return end + braces if end != -1 else len(source)

    return len(source)


def established_comment(path, lines, number, comment):
    line = lines[number - 1].lstrip("\ufeff").strip()
    if comment == "// Command + G":
        return "/Editor/" in f"/{path}" and re.fullmatch(r'\[(?:UnityEditor\.)?MenuItem\("[^"]+ %g"\)\]\s+// Command \+ G', line)
    if comment == "// Has execution order to start before every other script":
        return (path.endswith("/SwitchToEntrySceneInEditor.cs") and line == comment and number < len(lines)
                and lines[number].strip() == "public class SwitchToEntrySceneInEditor : MonoBehaviour")
    return False


def added_production_comments(cwd, path, lines, added, previous_path):
    if any(part in f"/{path}" for part in CODE_EXCLUDES):
        return

    source = "\n".join(lines)
    if "//" not in source and "/*" not in source:
        return
    comments = [(source.count("\n", 0, token.start()) + 1, token[0]) for token in csharp_comments(source)]
    if not comments:
        return

    def comments_in(revision, file):
        source = run(["git", "show", f"{revision}:{file}"], cwd)
        return Counter(token[0] for token in csharp_comments(source))

    previous = comments_in("HEAD", previous_path)
    merging = merge_head(cwd)
    if merging:
        previous |= comments_in(merging, path)

    # Prioritise unchanged lines so an identical new copy is reported at its added location.
    for number, comment in sorted(comments, key=lambda entry: entry[0] in added):
        if previous[comment]:
            previous[comment] -= 1
        elif not established_comment(path, lines, number, comment):
            yield number, "добавлен комментарий C#: вырази смысл именами и структурой кода или перенеси пояснение в PR"


def executable_lines(lines, start, end):
    return sum(1 for line in lines[start:end] if line.strip() and not line.strip().startswith("//") and line.strip() not in ("{", "}"))


JUMP_STATEMENT = re.compile(r"(return|continue|break|throw|goto|yield break)\b")
SWITCH_LABEL = re.compile(r"(?:case\b.*|default)\s*:")


def last_step_lines(lines, start, index):
    # Строки последнего шага перед переходом: от пустой строки на уровне блока; пустая строка во вложенном блоке шаг не делит.
    boundary = index - 1
    depth = 0
    while boundary > start:
        text = lines[boundary].strip()
        if not text and depth == 0:
            break
        depth += text.count("}") - text.count("{")
        boundary -= 1
    return executable_lines(lines, boundary + 1, index)


def jump_spacing(lines, added):
    # CODE_STYLE.md: перед оператором перехода пустая строка, если в блоке больше двух строк на экране; при двух — вплотную.
    # Строки считаются по экрану: инициализатор объекта или цепочка на несколько строк — уже не «две строки».
    # Блок — тело в фигурных скобках или секция case в switch; тело if без скобок блоком не считается.
    # Блок, уже поделённый пустой строкой, может кончаться шагом «одна строка и переход» вплотную.
    for index, line in enumerate(lines):
        text = line.strip()
        jump = JUMP_STATEMENT.match(text)
        if not jump:
            continue
        indent = len(line) - len(line.lstrip())
        previous = index - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous < 0 or (len(lines[previous]) - len(lines[previous].lstrip()) < indent and lines[previous].strip() != "{"):
            continue
        if not lines[previous].rstrip().endswith((";", "{", "}")):
            continue
        start = index - 1
        while start >= 0 and not (len(lines[start]) - len(lines[start].lstrip()) < indent and (lines[start].strip() == "{" or SWITCH_LABEL.fullmatch(lines[start].strip()))):
            start -= 1
        end = index + 1
        while end < len(lines) and not (len(lines[end]) - len(lines[end].lstrip()) < indent and (lines[end].strip() == "}" or SWITCH_LABEL.fullmatch(lines[end].strip()))):
            end += 1
        if start < 0 or not added.intersection(range(start + 1, end + 2)):
            continue
        statement_end = index
        balance = 0
        while statement_end < end:
            text_line = lines[statement_end]
            balance += text_line.count("{") + text_line.count("(") - text_line.count("}") - text_line.count(")")
            if balance <= 0 and text_line.rstrip().endswith(";"):
                break
            statement_end += 1
        before = executable_lines(lines, start + 1, index)
        if before == 0:
            continue
        jump_lines = executable_lines(lines, index, statement_end + 1)
        count = before + jump_lines
        blank = not lines[index - 1].strip()
        if count <= 2 and blank:
            yield index + 1, f"перед {jump.group(1)} лишняя пустая строка: в блоке две строки"
        elif count > 2 and not blank and last_step_lines(lines, start, index) + jump_lines > 2:
            yield index + 1, f"перед {jump.group(1)} нужна пустая строка: в блоке больше двух строк"


def config_extension(lines, added):
    # OPTIONAL_ENTITAS.md: extension не заводят на конфиг и данные; чтение/миграция корневого ProgressData — исключение ARCHITECTURE_GUIDELINES.md.
    for number in sorted(added):
        if number > len(lines):
            continue
        line = lines[number - 1]
        if not re.search(r"static\s+[^(]*\(\s*this\s+\w+(Config|Data|Definition)\b", line):
            continue
        if re.search(r"\bValidate\(\s*this\s+ProgressData\b", line) or re.search(
                r"\b(?:ReadFrom|PopulateFromSave)\(\s*this\s+ProgressData\s+\w+\s*,\s*string\s+\w+\s*\)", line):
            continue
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


def component_value_initializer(lines, added):
    # OPTIONAL_ENTITAS.md, «Канон именования»: поле Value компонента без инициализатора — значение приходит через Add/Replace.
    for number in sorted(added):
        if number <= len(lines) and re.search(r"I\w*Component\s*\{.*\bValue\s*=", lines[number - 1]):
            yield number, "у поля Value компонента инициализатор: убери, значение приходит через Add/Replace"


CONTEXTS = ("Game", "Meta", "Input")
COMPONENT_CONTEXTS = re.compile(r"^\s*(?P<attributes>(?:\[[^\]]*\]\s*)+)(?:public\s+)?(?:sealed\s+)?class\s+(?P<name>\w+)\s*:\s*[^{]*\bI\w*Component\b")


def component_context(lines, added):
    # OPTIONAL_ENTITAS.md, «Сущности и компоненты»: компонент объявляется в одном контексте — там, где живёт его сущность.
    # Запрос из другого контекста несёт свои компоненты (DebugGems в Game), сохраняемый Gems остаётся в Meta.
    # Исключение — технические Id и Destructed, которые есть у сущностей каждого контекста.
    for number in sorted(added):
        if number > len(lines):
            continue

        match = COMPONENT_CONTEXTS.match(blank_literals(lines[number - 1]))
        if not match or match.group("name") in ("Id", "Destructed"):
            continue

        contexts = [word for word in re.findall(r"\w+", match.group("attributes")) if word in CONTEXTS]
        if len(contexts) > 1:
            yield number, (f"компонент {match.group('name')} объявлен в контекстах {', '.join(contexts)}: компонент живёт в одном "
                           f"контексте, запросу из другого контекста заводят свои компоненты (DebugGems, а не [Game, Meta] Gems)")


RECORD_COPY = re.compile(r"(?:\b[\w.<>\[\],?]+|\bvar)\s+(\w+)\s*=\s*(\w+)\s*\.\s*([A-Z]\w*)\s*;")
RECORD_LOCAL = re.compile(r"\b(\w+)\s*=\s*new\b[\w\s.<>,()]*?\{([^{}]*)\}\s*;")
RECORD_ARGUMENT = re.compile(r"\.\s*(?:Add|Replace)([A-Z]\w*)\s*\(\s*new\b[\w\s.<>,()]*?\{([^{}]*)\}\s*\)")
RECORD_FIELD = r"[A-Z]\w*\s*(?:\+\+|--|[-+*/%|&^]?=(?!=))"


def block_end(source, start):
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            if not depth:
                return index
            depth -= 1
    return len(source)


def component_record(lines, added):
    # OPTIONAL_ENTITAS.md, «Канон именования»: значение компонента — один факт. Запись из нескольких полей в Value
    # раскладывают на компоненты: с ней система, меняющая одно поле, читает запись, правит копию и кладёт обратно целиком,
    # а сборка записи прямо в Add/Replace прячет несколько фактов за одним, и в запросе тоже. Запись элементом списка
    # (List<BroomMove>, List<EffectSetup>) — норма: список записей не разложить по компонентам одной сущности.
    source = blank_literals("\n".join(lines))

    def line(position):
        return source.count("\n", 0, position) + 1

    for match in RECORD_COPY.finditer(source):
        variable, entity, component = match.groups()
        end = block_end(source, match.end())
        replace = re.compile(rf"\b{entity}\s*\.\s*Replace{component}\s*\(\s*{variable}\s*\)").search(source, match.end(), end)
        change = replace and re.compile(rf"\b{variable}\s*\.\s*{RECORD_FIELD}|(?:\+\+|--)\s*{variable}\s*\.\s*[A-Z]").search(
            source, match.end(), replace.start())
        if change and added.intersection({line(match.start()), line(change.start()), line(replace.start())}):
            yield line(replace.start()), (f"{component} хранит запись, а код меняет одно её поле и кладёт запись обратно: "
                                          "каждый факт записи сделай своим компонентом")
    records = [(match.group(1), match.start(), match.group(2)) for match in RECORD_LOCAL.finditer(source)]
    records += [(None, match.start(), match.group(2)) for match in RECORD_ARGUMENT.finditer(source)]
    for variable, start, fields in records:
        if len(re.findall(r"(?:^|,)\s*[A-Z]\w*\s*=(?!=)", fields)) < 2:
            continue
        if variable:
            use = re.compile(rf"\.\s*(?:Add|Replace)([A-Z]\w*)\s*\(\s*{variable}\s*\)").search(source, start, block_end(source, start))
        else:
            use = RECORD_ARGUMENT.match(source, start)
        if use and added.intersection({line(start), line(use.start())}):
            yield line(use.start()), (f"{use.group(1)} получает запись из нескольких полей: каждый факт сделай своим компонентом, "
                                      "запись допустима только элементом списка")


def top_level_expression(expression):
    nesting, result = [], []
    for character in expression:
        if character in "([":
            result.append(character if not nesting else " ")
            nesting.append(character)
        elif character in ")]":
            if not nesting or nesting.pop() != {")": "(", "]": "["}[character]:
                return None
            result.append(character if not nesting else " ")
        else:
            result.append(character if not nesting or character == "\n" else " ")
    return None if nesting else "".join(result)


def flat_boolean_predicate(expression):
    parts = [line.strip() for line in expression.strip().splitlines()]
    if len(parts) < 2:
        return False
    operators = [re.search(r"(&&|\|\|)$", part) for part in parts[:-1]]
    if not all(operators) or len({operator[0] for operator in operators}) != 1:
        return False
    operands = [part[:operator.start()].strip() for part, operator in zip(parts, operators)] + [parts[-1]]
    for operand in operands:
        top = top_level_expression(operand)
        if not operand or top is None or re.search(r"&&|\|\||(?<!\?)\?(?![?.\[])|[;,{}]|=>|(?<![!=<>])=(?!=)", top):
            return False
    return True


def flat_ternary(expression):
    parts = [line.strip() for line in expression.strip().splitlines()]
    if len(parts) != 3 or not parts[1].startswith("?") or not parts[2].startswith(":"):
        return False
    branches = [parts[0], parts[1][1:].strip(), parts[2][1:].strip()]
    for index, branch in enumerate(branches):
        top = top_level_expression(branch)
        if not branch or top is None or re.search(r"[;,{}]", top) or index == 0 and re.search(r"(?<!\?)\?(?![?.\[])", top):
            return False
    return True


def expression_wrapping(lines, added):
    # CODE_STYLE.md, «Методы»: вызов — одной строкой или с компактным хвостом аргументов, сигнатура реализации — одной
    # строкой или столбцом, перенесённое условие — по связкам && и || с телом в скобках. Структурная раскладка сохраняется.
    source = "\n".join(lines)

    masked, position = list(source), 0
    while token := CSHARP_TOKENS.search(source, position):
        position = token.end()
        if token["punctuation"]:
            continue
        if token["raw_quotes"] or token["interpolated"]:
            parser = csharp_string_comments(source, position, len(token["raw_quotes"]), len(token["raw_dollars"])) if token["raw_quotes"] else csharp_string_comments(source, position, verbatim="@" in token[0])
            while True:
                try:
                    next(parser)
                except StopIteration as completed:
                    position = completed.value
                    break
        literal = source[token.start():position]
        blank = re.sub(r"[^\n]", " ", literal)
        if not token["comment"]:
            blank = "{" + blank[1:-1] + "}" if "\n" in blank else "x" + blank[1:]
        masked[token.start():position] = blank
    source = "".join(masked)
    stack, pairs = [], {}
    for index, character in enumerate(source):
        if character == "(":
            stack.append(index)
        elif character == ")" and stack:
            pairs[stack.pop()] = index

    def generic_start(text, end):
        end = len(text[:end].rstrip()) - 1
        if end < 0 or text[end] != ">":
            return None
        depth = 0
        for index in range(end, -1, -1):
            depth += (text[index] == ">") - (text[index] == "<")
            if depth == 0:
                return index if re.search(r"[\w.?]+$", text[:index].rstrip()) else None
        return None

    generic_commas = set()
    for opening in pairs:
        start = generic_start(source, opening)
        if start is not None:
            generic_commas.update(index for index in range(start, opening) if source[index] == ",")
    source = "".join(" " if index in generic_commas else character for index, character in enumerate(source))
    declarations, problems = [], []

    def report(start, end, message):
        first = source.count("\n", 0, start) + 1
        last = source.count("\n", 0, end) + 1
        if added.intersection(range(first, last + 1)) and not any(left <= end and start <= right for left, right, _ in problems):
            problems.append((start, end, (first, message)))

    def expression_end(start):
        nesting = []
        for index in range(start, len(source)):
            character = source[index]
            if not nesting and character in ";,})]":
                return index
            if character in "([{":
                nesting.append(character)
            elif nesting and character in ")]}":
                nesting.pop()
        return len(source)

    def structural(expression):
        builder = re.match(r"\s*\(?\s*(?:Create(?:Meta|Input)?Entity\.Empty|[\w.]+\.CreateSequence|(?:DG\.Tweening\.)?(?:DOTween|DOVirtual)\.\w+|[\w.]+\.DO[A-Z]\w*)\s*\(", expression)
        links = re.findall(r"\n\s*\.\w+(?:<[^\n]+>)?\s*\(", expression)
        # DOTween на своей строке: первое звено — .To(...), цепочка — когда за ним идут настройки.
        tween_root = re.match(r"\s*(?:DG\.Tweening\.)?(?:DOTween|DOVirtual)\s*\n", expression)
        chain = builder and links or tween_root and len(links) > 1
        matcher = re.search(r"\b\w*Matcher\b[\s\S]*\.(AllOf|NoneOf|AnyOf)\s*\(", expression)
        return "{" in expression or expression.lstrip().startswith("[") or bool(chain or matcher)

    expression_heads = list(re.finditer(r"(?P<assignment>(?<![=!<>])=(?!=|>))|(?P<arrow>=>)|(?P<return>\breturn\b)", source))
    structural_breaks = set()

    def mark_internal_breaks(start, end):
        expression = source[start:end]
        first = start + len(expression) - len(expression.lstrip())
        last = end - len(expression) + len(expression.rstrip())
        structural_breaks.update(index for index in range(first, last) if source[index] == "\n")

    for match in expression_heads:
        end = expression_end(match.end())
        expression = source[match.end():end]
        if match["arrow"]:
            body_start = match.end() + len(expression) - len(expression.lstrip())
            structural_breaks.update(index for index in range(match.end(), body_start) if source[index] == "\n")
        if (match["arrow"] or match["return"]) and flat_boolean_predicate(expression) or flat_ternary(expression):
            mark_internal_breaks(match.end(), end)
    for opening, closing in pairs.items():
        top = top_level_expression(source[opening + 1:closing])
        if top is None:
            continue
        start = opening + 1
        for offset in [index for index, character in enumerate(top) if character == ","] + [len(top)]:
            end = opening + 1 + offset
            if flat_ternary(source[start:end]):
                mark_internal_breaks(start, end)
            start = end + 1
    layout = "".join(" " if index in structural_breaks else character for index, character in enumerate(source))

    def wrapped_after(text, position):
        rest = text[position + 1:]
        return "\n" in rest[:len(rest) - len(rest.lstrip())]

    def angle_masked(text):
        depth, result = 0, []
        for character in text:
            depth += character == "<"
            result.append(" " if depth and character == "," else character)
            depth -= character == ">" and depth > 0
        return "".join(result)

    for opening, closing in sorted(pairs.items()):
        prefix_start = source.rfind("\n", 0, len(source[:opening].rstrip())) + 1
        prefix = source[prefix_start:opening].strip()
        prefix = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", prefix)
        declaration = re.match(r"(?:(?:public|private|protected|internal|static|override|abstract|virtual|async|sealed|partial|new|delegate)\s+)+[^=;{}]*$", prefix)
        local_declaration = re.fullmatch(r"\w[\w.<>,?\[\]]*\s+\w+(?:<[^>]+>)?", prefix) and prefix.split()[0] not in ("return", "await", "new", "throw", "yield", "else")
        if (declaration and not prefix.startswith("new ") or local_declaration) and "=>" not in prefix:
            declarations.append((opening, closing))
            parameters = source[opening + 1:closing]
            if "\n" in parameters and not source[closing + 1:].lstrip().startswith(";"):
                top = top_level_expression(angle_masked(parameters)) or ""
                commas = [index for index, character in enumerate(top) if character == ","]
                if not wrapped_after(parameters, -1) or not all(wrapped_after(parameters, comma) for comma in commas):
                    report(prefix_start, closing, "сигнатура перенесена частично: в реализации пиши параметры одной строкой или каждый на своей строке, начиная с первого")
            continue
        control = re.search(r"\b(if|while|for|foreach)\s*$", prefix)
        if "\n" not in source[prefix_start:closing]:
            continue
        if control:
            condition = source[opening + 1:closing]
            top = top_level_expression(condition) or condition
            endings = {line.rstrip()[-2:] for line in top.split("\n")[:-1]}
            if control[1] in ("for", "foreach") or "\n" in source[prefix_start:opening]:
                report(prefix_start, closing, "заголовок перенесён: for, foreach и начало условия до ( пиши одной строкой, длинный запрос вынеси в переменную")
            elif len(endings) != 1 or not endings <= {"&&", "||"} or re.search(r"(&&|\|\|)(?!\s*\n)", top.rstrip()):
                report(prefix_start, closing, "условие перенесено не по связкам: каждое законченное условие на своей строке, одинаковый && или || в конце строки")
            elif not source[closing + 1:].lstrip().startswith(("{", ";")):
                report(prefix_start, closing, "перенесённое условие требует тела в фигурных скобках")
            continue
        if "\n" not in layout[prefix_start:closing]:
            continue
        call_prefix = prefix.rstrip()
        generic = generic_start(call_prefix, len(call_prefix))
        if generic is not None:
            call_prefix = call_prefix[:generic].rstrip()
        invocation = re.search(r"([\w.?]+)\s*$", call_prefix)
        if not invocation or invocation[1] in ("catch", "using", "lock", "switch", "typeof", "nameof", "default", "return"):
            continue
        name = invocation[1].split(".")[-1]
        expression = source[opening:closing + 1]
        if name in ("AllOf", "NoneOf", "AnyOf") or structural(expression):
            continue
        arguments = layout[opening + 1:closing]
        top = top_level_expression(arguments) or ""
        commas = [index for index, character in enumerate(top) if character == ","]
        if "\n" in layout[prefix_start:opening] or wrapped_after(arguments, -1):
            report(prefix_start, closing, "аргументы вызова начинаются с новой строки: оставь на строке вызова аргументы, которые помещаются, и переноси только хвост")
        elif len(commas) >= 2 and all(wrapped_after(arguments, comma) for comma in commas):
            report(prefix_start, closing, "аргументы вызова разложены по одному на строку: заполняй строку и переноси компактный хвост")

    for match in expression_heads:
        if any(start < match.start() < end for start, end in declarations):
            continue
        end = expression_end(match.end())
        expression = source[match.end():end]
        call_breaks = {index for opening, closing in pairs.items() if match.end() <= opening and closing < end
                       for index in range(opening, closing) if source[index] == "\n"}
        outer = "".join(" " if index in call_breaks else layout[index] for index in range(match.end(), end))
        inspected = outer.strip() if match["arrow"] else outer.rstrip()
        if "\n" not in inspected or structural(expression):
            continue
        report(match.start(), end, "выражение перенесено между операндами: присваивание, return и выражение после => начинай на их строке, длинный вызов переноси хвостом аргументов")
    yield from (problem for _, _, problem in sorted(problems))


def inserted_in_list(lines, added):
    # CODE_STYLE.md, «Новые объявления дописывать в конец списка»: новая привязка в инсталлере дописывается в конец своего метода. Поля,
    # константы и matcher-ы хук не проверяет: там соседство по смыслу даёт ложные срабатывания.
    for number in sorted(added):
        if number < 2 or number >= len(lines) or number - 1 in added or number + 1 in added:
            continue
        if all(lines[index].strip().startswith("Container.Bind") for index in (number - 2, number - 1, number)):
            yield number, "привязка вставлена в середину списка: допиши её в конец метода"


def request_before_save(lines, added):
    # CODE_STYLE.md, порядок в системе: запрос на сохранение фиксирует действие самой системы и стоит сразу за ним;
    # запрос, который запускает следующее действие в другой системе (рестарт уровня, метла), идёт после сохранения.
    for index, line in enumerate(lines):
        if not re.search(r"\.isSave\w*Request = true;", line) or index < 2:
            continue
        previous = index - 2
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous > 0 and lines[previous].strip() == "}":
            previous -= 1
        request = previous >= 0 and re.search(r"\.(is\w+Request = true;|Add\w+Request\()", lines[previous])
        if request and "Save" not in request.group(0) and added.intersection({index + 1, previous + 1}):
            yield index + 1, "запрос другой системе стоит перед сохранением: сохранение закрывает действие этой системы, запрос следующего шага — после него"


def block_body_end(lines, start):
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines) and not (lines[end].strip() == "}" and len(lines[end]) - len(lines[end].lstrip()) == indent):
        end += 1
    return end


def single_return_getter(lines, added):
    # CODE_STYLE.md, «Форматирование»: свойство из одного выражения пишется через =>, даже если условие
    # переносится по строкам; get { return ...; } с единственным return не пишется.
    for index, line in enumerate(lines):
        if line.strip() != "get" or index + 1 >= len(lines) or lines[index + 1].strip() != "{":
            continue
        end = block_body_end(lines, index + 1)
        body = " ".join(text.strip() for text in lines[index + 2:end] if text.strip())
        if body.startswith("return ") and body.count(";") == 1 and added.intersection(range(index + 1, end + 2)):
            yield index + 1, "свойство из одного выражения: пиши через =>, условие можно перенести по строкам"


EXPRESSION_MEMBER = re.compile(r"\s*(?:\[[^\]]*\]\s*)*(?:public|private|protected|internal)\s")
EXPRESSION_ACCESSOR = re.compile(r"\s*(?:(?:public|private|protected|internal)\s+)+(?:get|set|init)\s*$")
DECLARATION_ASSIGNMENT = re.compile(r"(?<![=!<>])=(?![=>])")


def expression_body_line(lines, added):
    # CODE_STYLE.md, «Форматирование»: у expression-bodied свойства, метода и конструктора реализация всегда на
    # следующей строке после =>. Лямбды, ветви switch, get/set свойства и инициализаторы полей не проверяются.
    for number in sorted(added):
        if number > len(lines):
            continue
        head, arrow, body = lines[number - 1].partition("=>")
        if not arrow or not body.strip() or "{" in head or not EXPRESSION_MEMBER.match(head) or EXPRESSION_ACCESSOR.match(head):
            continue
        previous = None
        while previous != head:
            previous, head = head, re.sub(r"\([^()]*\)", "()", head)
        if not DECLARATION_ASSIGNMENT.search(head):
            yield number, "реализация справа от =>: у свойства, метода и конструктора пиши её на следующей строке"


def awake_method(lines, added):
    # CODE_STYLE.md, «Паттерны Unity»: Awake не пишем. Подписки — в Start, снятие — в OnDestroy; подготовку, которая
    # нужна до первого вызова сразу после создания, делает Initialize, который зовёт фабрика.
    for number in sorted(added):
        if number <= len(lines) and re.match(r"\s*(private|protected)?\s*(override\s+)?void Awake\(\)", lines[number - 1]):
            yield number, "Awake: подписки — в Start, подготовку до первого вызова делает Initialize из фабрики"


def serialized_property_order(lines, added):
    # CODE_STYLE.md, «Порядок членов класса»: блок [field: SerializeField] — первый в секции свойств, выше обычных
    # свойств, в том числе вычисляемых private.
    property_seen = False
    for index, line in enumerate(lines):
        text = line.strip()
        if text.startswith("[field: SerializeField]"):
            if property_seen and index + 1 in added:
                yield index + 1, "сериализуемое свойство ниже обычного: блок [field: SerializeField] идёт первым"
        elif re.match(r"(public|private|protected|internal)\s+(static\s+|override\s+|virtual\s+|abstract\s+)*[\w<>\[\],.?]+\s+[A-Z]\w*\s*(=>|\{\s*get)", text) and "(" not in text.split("=>")[0]:
            property_seen = True


def refresh_method_name(lines, added, path=""):
    # CODE_STYLE.md, «Типы»: Refresh в имени — только у ECS-системы, которая проталкивает состояние (Refresh…System);
    # метод view, окна или сервиса, который перерисовывает или принимает данные, называется Update….
    if path.endswith("System.cs") or "/Editor/" in path:
        return
    for number in sorted(added):
        if number <= len(lines) and re.search(r"\b(void|bool|int|float|string|Task)\s+Refresh\w*\(", lines[number - 1]):
            yield number, "метод с Refresh вне системы: называй его Update… по обновляемой части"


PURCHASE_SYNONYMS = {"buy": "Purchase", "bought": "Purchased"}
DECLARED_NAME = re.compile(
    r"(?<![\w.])(?!(?:return|new|await|throw|case|goto|else|in|is|as|out|ref|params|this|using|yield|when|where|from|select|let|"
    r"public|private|protected|internal|static|readonly|const|override|virtual|abstract|sealed|partial|async|event|extern|volatile)\b)"
    r"[A-Za-z_][\w.]*(?:<[^<>;]*>)?(?:\[\])*\??\s+(?P<name>@?_?[A-Za-z]\w*)\s*(?==>|[=;,){:(<]|\bin\b)")


def purchase_word(lines, added, path=""):
    # CODE_STYLE.md, «Поля и свойства»: одно понятие — одно слово. Покупка называется Purchase и за деньги, и за гемы,
    # поэтому Buy и Bought в новых объявлениях не используются: поиск по одному слову находит все покупки.
    if "Assets/Code/" not in path or "/Plugins/" in path:
        return

    source = blank_literals("\n".join(lines)).split("\n")
    for number in sorted(added):
        if number > len(source):
            continue

        line = source[number - 1]
        names = [match.group("name") for match in DECLARED_NAME.finditer(line)] + [name for _, name in TYPE_DECLARATION.findall(line)]
        for name in dict.fromkeys(names):
            words = {word.lower() for word in NAME_WORDS.findall(name.lstrip("_@"))}
            for word, canonical in PURCHASE_SYNONYMS.items():
                if word in words:
                    yield number, f"имя {name}: покупка называется {canonical}, слово {word.capitalize()} не используем — одно понятие, одно слово"


def unity_time(lines, added, path=""):
    # ARCHITECTURE_GUIDELINES.md, «Границы зависимостей»: время игры берётся у ITimeProvider, а не у UnityEngine.Time.
    if "/Common/Time/" in path or "/Editor/" in path or "/Tests/" in path:
        return
    for number in sorted(added):
        if number <= len(lines) and re.search(r"(?:(?<![\w.])|UnityEngine\.)Time\.(time|deltaTime|unscaledDeltaTime|unscaledTime|realtimeSinceStartup|timeSinceLevelLoad|fixedDeltaTime|timeScale|frameCount)\b", lines[number - 1]):
            yield number, "UnityEngine.Time напрямую: время бери у ITimeProvider"


def self_update(lines, added, path=""):
    # ARCHITECTURE_GUIDELINES.md, «Порядок выполнения»: объект игры не подписывает себя на кадр своим Update.
    # Покадровую работу вида зовёт владелец: Refresh-система каждый кадр передаёт состояние через UI-сервис.
    if "/Gameplay/" not in path and "/Meta/" not in path:
        return
    for number in sorted(added):
        if number <= len(lines) and re.match(r"\s*(private|protected)?\s*void (Update|LateUpdate|FixedUpdate)\(\)", lines[number - 1]):
            yield number, "свой Update у объекта: кадр ему передаёт владелец (Refresh-система через UI-сервис)"


def animator_lifecycle(lines, added, path=""):
    # ARCHITECTURE_GUIDELINES.md, «Границы зависимостей»: у <Owner>Animator нет своих Unity-колбэков; Initialize и
    # CleanUp ему зовёт владелец из своих Start/OnDestroy или фабрика.
    if not any(re.search(r"\bclass \w+Animator\s*:\s*MonoBehaviour\b", line) for line in lines):
        return
    for number in sorted(added):
        if number <= len(lines) and re.match(r"\s*(private|protected)?\s*void (Awake|OnEnable|Start|Update|LateUpdate|OnDisable|OnDestroy)\(\)", lines[number - 1]):
            yield number, "Unity-колбэк у аниматора: Initialize/CleanUp ему зовёт владелец"


def dialogue_lifecycle(lines, added):
    # CODE_STYLE.md, «Паттерны Unity»: наследник окна не заводит второй lifecycle поверх базы, включая OnUpdate.
    source = "\n".join(lines)
    owner = re.search(r"^\s*(?:(?:public|internal|abstract|sealed|partial)\s+)*class\s+\w+\s*:\s*(?:\w+\.)*(DialogueBase|ScreenBase|BaseWindow)\b", source, re.M)
    if not owner:
        return
    for number in sorted(added):
        if number > len(lines):
            continue
        callback = re.match(r"\s*(?:(?:public|private|protected|internal|override|virtual|sealed|new|async|static)\s+)*(?:void|(?:System\.Collections\.)?IEnumerator)\s+(Awake|OnEnable|Start|Update|LateUpdate|FixedUpdate|OnDisable|OnDestroy|OnUpdate)\s*\(\s*\)", lines[number - 1])
        if callback:
            yield number, f"{callback.group(1)} у наследника {owner.group(1)}: используй штатные Initialize/SubscribeUpdates/CleanUp базы; собственный покадровый сценарий во view не добавляй"


def mono_lifecycle_order(lines, added):
    # CODE_STYLE.md: Construct и разовый Set создателя, колбэки Unity, Initialize/SubscribeUpdates, публичные методы, очистка,
    # остальные методы; место задаёт роль, не видимость. Объявленные классом protected virtual/abstract — блок контракта базы.
    owner = next(((number, match) for number, line in enumerate(lines, 1) if (match := re.match(
        r"^([ \t]*)(?:(?:public|internal|abstract|sealed|partial)\s+)*class\s+(\w+)\s*:\s*(?:UnityEngine\.)?MonoBehaviour\b", line))), None)
    if owner is None:
        return

    owner_line, declaration = owner
    indent = len(declaration.group(1)) + 4
    lifecycle = {"Construct": 0, "Initialize": 2, "SubscribeUpdates": 3, "CleanUp": 5, "Dispose": 5, "Unsubscribe": 5}
    methods = []
    for number, line in enumerate(lines[owner_line:], owner_line + 1):
        if line.rstrip() == declaration.group(1) + "}":
            break
        if len(line) - len(line.lstrip()) != indent:
            continue
        text = re.sub(r"^\s*(?:\[[^\]]+\]\s*)+", "", line)
        method = re.match(r"\s*(?:(?:public|protected|internal|private|static|virtual|abstract|override|sealed|async)\s+)*[\w.<>,?\[\]]+\s+(?P<interface>(?:\w+\.)*)(?P<name>\w+)(?:<[^>()]*>)?\s*\(", text)
        if not method:
            continue
        name = method.group("name")
        if name == declaration.group(2):
            continue
        modifiers = text[:method.start("interface")].split()
        callback = re.match(r"\s*(?:(?:public|protected|internal|private|virtual|override|sealed|async)\s+)*(?:void|(?:System\.Collections\.)?IEnumerator)\s+"
                            r"(?:(?:Awake|OnEnable|Start|FixedUpdate|Update|LateUpdate|OnDisable|OnDestroy|Reset|OnValidate|OnTransformParentChanged|OnTransformChildrenChanged|OnRectTransformDimensionsChange|OnApplicationQuit)\s*\(\s*\)"
                            r"|(?:OnApplicationFocus|OnApplicationPause)\s*\(\s*bool\s+\w+\s*\))", text)
        if callback:
            methods.append((number, name, 1))
        elif "protected" in modifiers and {"virtual", "abstract"} & set(modifiers):
            continue
        elif name in lifecycle and "static" not in modifiers:
            methods.append((number, name, lifecycle[name]))
        elif re.match(r"Set[A-Z]", name) and "public" in modifiers and "static" not in modifiers and all(order <= 0.5 for _, _, order in methods):
            methods.append((number, name, 0.5))
        elif not re.match(r"On[A-Z]", name):
            methods.append((number, name, 4 if "public" in modifiers or method.group("interface") else 6))

    for index, (number, name, order) in enumerate(methods):
        for prior_number, prior_name, prior_order in methods[:index]:
            if prior_order > order and {prior_order, order} != {4, 6} and (number in added or prior_number in added):
                yield number, f"порядок MonoBehaviour: {name} ставь перед {prior_name} (Construct и разовый Set создателя → колбэки Unity → Initialize/SubscribeUpdates → публичные методы → CleanUp → остальные методы)"
                break


def dialogue_hide_base_first(lines, added, signature):
    if signature >= len(lines) or lines[signature].strip() != "{":
        return
    end = block_body_end(lines, signature)
    statements = [(index + 1, lines[index].strip()) for index in range(signature + 1, end)
                  if lines[index].strip() and not lines[index].lstrip().startswith("//")]
    for index, (number, statement) in enumerate(statements):
        if not statement.startswith("base.Hide("):
            continue
        preceding = statements[:index]
        touched = added.intersection({signature, number} | {line for line, _ in preceding})
        if preceding and touched and all(re.fullmatch(r"[\w.?]+(?:\([^{};]*\)|\s*=\s*[^;{}]+);", step) for _, step in preceding):
            yield number, "base.Hide вызывай первым действием: база обновляет Shown и может сразу вызвать onHidden"
        return


def dialogue_lifecycle_order(lines, added):
    # CODE_STYLE.md: разовый Set создателя идёт за Construct, Initialize/SubscribeUpdates открывают API окна, CleanUp закрывает
    # его. Объявленные классом protected virtual/abstract — блок контракта базы.
    owner = next((match for line in lines if (match := re.match(
        r"^([ \t]*)(?:(?:public|internal|abstract|sealed|partial)\s+)*class\s+\w+\s*:\s*(?:\w+\.)*(DialogueBase|ScreenBase|BaseWindow)\b", line))), None)
    if not owner:
        return
    dialogue = owner.group(2) == "DialogueBase"
    indent = len(owner.group(1)) + 4
    lifecycle_order = {"Initialize": 1, "SubscribeUpdates": 2, "CleanUp": 5, "Dispose": 5, "Unsubscribe": 5}
    methods = []
    for number, line in enumerate(lines, 1):
        if len(line) - len(line.lstrip()) != indent:
            continue
        text = re.sub(r"^\s*(?:\[[^\]]+\]\s*)+", "", line)
        method = re.match(r"\s*(?:(?:public|protected|internal|private|static|virtual|abstract|override|sealed|async)\s+)*[\w.<>,?\[\]]+\s+(?P<interface>(?:\w+\.)*)(?P<name>\w+)(?:<[^>()]*>)?\s*\(", text)
        if not method:
            continue
        name = method.group("name")
        modifiers = text[:method.start("interface")].split()
        public = "public" in modifiers
        overriding = "override" in modifiers
        if dialogue and overriding and name == "Hide":
            yield from dialogue_hide_base_first(lines, added, number)
        if overriding and name not in lifecycle_order and (not dialogue or not public or name.startswith("On")):
            continue
        if "protected" in modifiers and {"virtual", "abstract"} & set(modifiers):
            continue
        role = "interface" if method.group("interface") else "public" if public else "method"
        if name in lifecycle_order and "static" not in modifiers:
            role = "lifecycle"
        elif name == "Construct":
            role = "construct"
        elif public and "static" not in modifiers and re.match(r"Set[A-Z]", name) and all(previous in ("construct", "setup") for _, _, previous in methods):
            role = "setup"

        methods.append((number, name, role))

    lifecycle = [(number, name) for number, name, role in methods if role == "lifecycle"]
    if not lifecycle:
        return

    ranks = {"construct": 0, "setup": 0.5, "interface": 3, "public": 4, "method": 6}
    unordered = set() if dialogue else {3, 4, 6}
    ordered = []
    for number, name, role in methods:
        rank = lifecycle_order[name] if role == "lifecycle" else ranks[role]
        if any(previous_rank > rank and not {previous_rank, rank} <= unordered and added.intersection({previous, number}) for previous, previous_rank in ordered):
            yield number, f"{name} нарушает порядок диалога: Construct и разовый Set создателя, Initialize/SubscribeUpdates, интерфейсы и публичные методы, CleanUp, остальные методы"
        ordered.append((number, rank))


def condition_operands(condition):
    depth, current, operands, index = 0, "", [], 0
    while index < len(condition):
        char = condition[index]
        depth += (char == "(") - (char == ")")
        if depth == 0 and condition[index:index + 2] in ("&&", "||"):
            operands.append(current.strip())
            current, index = "", index + 2
            continue
        current += char
        index += 1
    return operands + [current.strip()]


MEMBER_OPERAND = re.compile(r"^!?(_\w+|[A-Z]\w*)(\.\w+(\(\))?)*(\s*(==|!=|<=|>=|<|>)\s*(null|true|false|\d+(\.\d+)?f?|_\w+(\.\w+)*|[A-Z]\w*(\.\w+)*))?$")


def inline_member_condition(lines, added):
    # CODE_STYLE.md, «Поля и свойства»: условие из двух и больше проверок, собранное только из полей, свойств и
    # сервисов класса, выносится в приватное свойство-вопрос; в строке остаются условия на локальных и параметрах.
    declared = {match.group(1) for match in (re.match(r"\s*(?:\[.*\]\s*)*(?:public|private|protected|internal)\b[^(=]*?\b(\w+)\s*(?:\{|=>|=|;|$)", line) for line in lines) if match}
    for index, line in enumerate(lines):
        match = re.match(r"\s*(?:else )?if \((.*)$", line)
        if not match:
            continue
        condition, end = match.group(1), index
        while condition.count("(") + 1 > condition.count(")") and end + 1 < len(lines):
            end += 1
            condition += " " + lines[end].strip()
        operands = condition_operands(condition[:condition.rfind(")")])
        if len(operands) < 2 or not all(MEMBER_OPERAND.match(operand) for operand in operands):
            continue
        roots = [re.match(r"!?(\w+)", operand).group(1) for operand in operands]
        if all(root.startswith("_") or root in declared for root in roots) and added.intersection(range(index + 1, end + 2)):
            yield index + 1, "условие из полей и свойств класса: вынеси в приватное свойство-вопрос (CanShowDialogue, CanDeal)"


GUARD_HEAD = re.compile(r"^(?P<indent>[ \t]*)if \((?P<condition>.*)\)[ \t]*$")
GUARD_EXIT = re.compile(r"^[ \t]*(?:continue|break|return|throw)\b[^;]*;[ \t]*$")


def and_operands(condition):
    top = top_level_expression(condition)
    return set(condition_operands(condition)) if top and "&&" in top and "||" not in top else set()


def split_guard(lines, added):
    # CODE_STYLE.md, «Методы»: подряд идущие guard-ы с одним выходом по умолчанию объединяют в один if. Без исключений ловится
    # пара, где оба условия требуют один и тот же операнд &&: это одна проверка A && (X || Y), разрезанная на две.
    text = blank_literals("\n".join(lines)).split("\n")
    guards = [(index, head["indent"], and_operands(head["condition"]), lines[index + 1].strip())
              for index in range(len(text) - 1)
              if (head := GUARD_HEAD.match(text[index])) and GUARD_EXIT.match(text[index + 1])]
    for (first, indent, operands, exit), (second, next_indent, next_operands, next_exit) in zip(guards, guards[1:]):
        shared = sorted(operands & next_operands)
        if (not shared or indent != next_indent or exit != next_exit or any(line.strip() for line in text[first + 2:second])
                or not added.intersection(range(first + 1, second + 3))):
            continue
        yield second + 1, (f"guard с тем же выходом «{exit}» снова требует {shared[0]}: объедини с предыдущим в один if — "
                           f"{shared[0]} && (… || …)")


PROCESS_MARKER = re.compile(r"(ing|Processing)$")
CODE_EXCLUDES = ("/Generated/", "/Tests/", "/Plugins/")


def closing(text, start):
    depth = 0
    for index in range(start, len(text)):
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        if depth == 0:
            return index
    return len(text)


def matcher_clauses(text, matcher):
    for match in re.finditer(rf"\.{matcher}\(", text):
        close = closing(text, match.end() - 1)
        yield text.count("\n", 0, match.start()) + 1, text.count("\n", 0, close) + 1, re.findall(r"\w+Matcher\.(\w+)", text[match.start():close + 1])


def anyof_matcher(lines, added, path):
    if any(part in f"/{path}" for part in CODE_EXCLUDES):
        return
    for start, end, _ in matcher_clauses("\n".join(lines), "AnyOf"):
        if added.intersection(range(start, end + 1)):
            yield start, "AnyOf в ручной ECS-выборке: используй матчер компонента, AllOf или NoneOf; альтернативы проверяй у владельца"


def noneof_clauses(text):
    return matcher_clauses(text, "NoneOf")


def code_feature(path):
    folder = os.path.dirname(path)
    return os.path.dirname(folder) if os.path.basename(folder) in ("Systems", "Factory", "Extensions") else folder


def noneof_problems(code, changed):
    # OPTIONAL_ENTITAS.md, «Системы»: NoneOf — по флагу «уже сделано», который ставит сама исключающая фича,
    # по идущему процессу (…ing, …Processing) или у близнецов одного модуля: одна система берёт AllOf(X), другая
    # NoneOf(X), а X — вид сущности, его нигде не снимают. Снятие (= false, Remove) фичу владельцем флага не делает:
    # LevelStartRequired снимает Level, а ставит LevelVictory. Компонент с данными (LevelAttempt) — состояние сущности,
    # его меняют и другие фичи: решение «уже есть» — проверкой в теле. Флаг, который исключают в фиче, где его
    # не ставят и где нет близнеца, — фильтр «кто в выборке» (выход из игры): положительным <Субъект>Active в AllOf.
    # View ставит фабрика вида, у Destructed отдельное правило.
    excluders, includers = {}, {}
    for path, text in code.items():
        for _, _, components in noneof_clauses(text):
            for component in components:
                excluders.setdefault(component, set()).add(path)
        for _, _, components in matcher_clauses(text, "AllOf"):
            for component in components:
                includers.setdefault(component, set()).add(code_feature(path))
    verdicts, data = {}, None
    for path, added in changed.items():
        for start, end, components in noneof_clauses(code[path]):
            if not added.intersection(range(start, end + 1)):
                continue
            for component in components:
                if component in ("View", "Destructed") or PROCESS_MARKER.search(component):
                    continue
                if component not in verdicts:
                    setter = re.compile(rf"\bis{component}\s*=\s*(?!false\b)\w+|\.(Add|Replace){component}\(")
                    remover = re.compile(rf"\bis{component}\s*=\s*false\b|\.Remove{component}\(")
                    data = data_components(code) if data is None else data
                    setters = {code_feature(other) for other, text in code.items() if setter.search(text)}
                    twins = includers.get(component, set()) if not any(remover.search(text) for text in code.values()) else set()
                    flag = component not in data
                    verdicts[component] = all(feature in twins or flag and feature in setters
                                              for feature in {code_feature(other) for other in excluders[component]})
                if not verdicts[component]:
                    yield path, start, (f"NoneOf({component}) — это не флаг «уже сделано» своей фичи: компонент с данными или флаг ставят вне фичи, "
                                        "где его исключают, а близнеца с AllOf в этом модуле нет. Решение системы («уже есть», «ещё ждём») — "
                                        "проверкой в теле по выборке AllOf, выход из игры — положительным <Субъект>Active в AllOf")


def blank_literals(source):
    return re.sub(r'(?P<raw>"{3,})[\s\S]*?(?P=raw)|@"(?:""|[^"])*"|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/',
                  lambda match: re.sub(r"[^\n]", " ", match.group()), source)


def brace_blocks(text):
    blocks, opened = [], []
    for match in re.finditer(r"[{}]", text):
        if match.group() == "{":
            opened.append(match.start())
        elif opened:
            blocks.append((opened.pop(), match.start()))
    return blocks


def class_spans(text, blocks):
    closes = dict(blocks)
    return [(match.group(1), match.end() - 1, closes.get(match.end() - 1, len(text)))
            for match in re.finditer(r"\b(?:class|struct)\s+(\w+)[^{;]*\{", text)]


def statement_start(text, position):
    return max(text.rfind(mark, 0, position) for mark in ";{}") + 1


def matcher_components(text):
    excluded = {component for _, _, components in noneof_clauses(text) for component in components}
    return set(re.findall(r"\w+Matcher\s*\.\s*(\w+)", text)) - excluded - {"AllOf", "AnyOf", "NoneOf"}


def system_order(sources):
    # Плоский порядок систем каждого корня: фича добавляет системы и вложенные фичи через Add(systems.Create<X>()).
    # Одноимённые фичи разных модулей (Gameplay и Meta BoostersFeature) различаются по namespace и using родителя.
    features = {}
    for text in sources.values():
        if "Create<" in text:
            namespace = next(iter(re.findall(r"\bnamespace\s+([\w.]+)", text)), "")
            usings = set(re.findall(r"^\s*using\s+([\w.]+)\s*;", text, re.M))
            for name, start, end in class_spans(text, brace_blocks(text)):
                added = re.findall(r"\bAdd\s*\(\s*\w+\s*\.\s*Create\s*<\s*(\w+)\s*>", text[start:end])
                if added:
                    features.setdefault(name, []).append((namespace, usings, added))

    def walk(feature, seen, flat):
        namespace, usings, added = feature
        for name in added:
            flat.append(name)
            options = features.get(name, []) if name not in seen else []
            visible = [option for option in options
                       if option[0] in usings or namespace == option[0] or namespace.startswith(option[0] + ".")]
            for option in visible or options:
                walk(option, seen | {name}, flat)
        return flat

    order = {}
    for root in features.keys() - {name for options in features.values() for _, _, added in options for name in added}:
        for index, name in enumerate(name for feature in features[root] for name in walk(feature, {root}, [])):
            order.setdefault(name, []).append((root, index))
    return order


DESTRUCT_MARK = re.compile(r"\b(\w+)\s*\.\s*isDestructed\s*=\s*true\b")
CALLBACK_HEADER = re.compile(r"=>\s*$|\bdelegate\s*(?:\([^()]*\))?\s*$")
LOCAL_FUNCTION = re.compile(r"^\s*(?!(?:if|else|for|foreach|while|switch|using|lock|catch|fixed|return|new|await|yield)\b)"
                            r"(?:(?:static|async|unsafe)\s+)*[\w.<>\[\],?]+\s+\w+\s*\([^()]*\)\s*$")
GROUP_FIELD = re.compile(r"\b(\w+)\s*=\s*[\w.]*\bGetGroup\s*\(")
CONDITIONAL = re.compile(r"\b(?:if|else|for|foreach|while|case|default)\b|\?|=>")
DestructMark = namedtuple("DestructMark", "path line owner member callback components removed loops")


def destruct_marks(sources):
    # Где сущность помечают isDestructed = true: класс и метод, callback ли это, какие компоненты у сущности видны
    # (обход группы, чтение is/has/значения) и какие снимаются безусловно рядом с пометкой.
    for path, text in sources.items():
        sites = list(DESTRUCT_MARK.finditer(text))
        if not sites:
            continue
        blocks = brace_blocks(text)
        classes = class_spans(text, blocks)
        for site in sites:
            position, variable = site.start(), re.escape(site.group(1))
            line = text.count("\n", 0, position) + 1
            owners = [span for span in classes if span[1] < position < span[2]]
            if not owners:
                yield DestructMark(path, line, None, "", True, set(), set(), set())
                continue
            owner, class_start, class_end = max(owners, key=lambda span: span[1])
            chain = sorted(block for block in blocks if class_start < block[0] < position < block[1])
            headers = [text[statement_start(text, start):start] for start, _ in chain]
            callback = (not chain or "=>" in text[statement_start(text, position):position]
                        or any(CALLBACK_HEADER.search(header) or LOCAL_FUNCTION.match(header) for header in headers[1:]))
            member = next(iter(re.findall(r"(\w+)\s*\(", headers[0])), "") if chain else ""
            scope_start, scope_end = chain[0] if chain else (statement_start(text, position), site.end())
            scope = text[scope_start:scope_end]
            groups = {match.group(1): matcher_components(text[match.end():closing(text, match.end() - 1)])
                      for match in GROUP_FIELD.finditer(text, class_start, class_end)}
            components = set(re.findall(rf"\b{variable}\s*\.\s*(?:is|has)([A-Z]\w*)\b(?!\s*=(?!=))", scope))
            components |= set(re.findall(rf"\b{variable}\s*\.\s*([A-Z]\w*)\b(?!\s*(?:[(<]|=(?!=)))", scope))
            loops = set(re.findall(rf"\b{variable}\s+in\s+(?:this\s*\.\s*)?(\w+)", scope))
            for field in loops | set(re.findall(rf"\b{variable}\s*=\s*(?:this\s*\.\s*)?(\w+)\s*\.", scope)):
                components |= groups.get(field, set())
            removed = set()
            for removal in re.finditer(rf"\b{variable}\s*\.\s*(?:is(\w+)\s*=\s*false\b|Remove(\w+)\s*\()", scope):
                at = scope_start + removal.start()
                block = max((block for block in blocks if block[0] < at < block[1]), default=(0, len(text)))
                if block[0] < position < block[1] and not CONDITIONAL.search(text[statement_start(text, at):at]):
                    removed.add(removal.group(1) or removal.group(2))
            yield DestructMark(path, line, owner, member, callback, components - {"Destructed", "Enabled"}, removed, loops)


def reaches(mark, system, field, order):
    # Пометку видит выборка system, если её ставят вне ECS-цикла или раньше в Execute-проходе; пометку из Cleanup
    # и из систем после system убирает Cleanup-проход того же кадра. Сама system, гася сущность при обходе этой же
    # выборки, пути не создаёт: обход идёт по снимку группы.
    if mark.callback or mark.owner not in order:
        return True
    if mark.member == "Cleanup":
        return False
    if mark.owner == system:
        return field is None or field not in mark.loops
    if system not in order:
        return True
    return any(root != other or index < position for root, index in order[mark.owner] for other, position in order[system])


def destructed_filter_problems(code, changed):
    # OPTIONAL_ENTITAS.md, «Системы»: NoneOf(Destructed) — только при достижимом пути. Без пути, при снятии компонента
    # из AllOf той же пометкой и у выборки, которую система только гасит, фильтр мёртвый.
    added = []
    for path, lines in changed.items():
        text = blank_literals(code[path])
        for match in re.finditer(r"\.NoneOf\(", text):
            close = closing(text, match.end() - 1)
            start, end = text.count("\n", 0, match.start()) + 1, text.count("\n", 0, close) + 1
            if "Destructed" in re.findall(r"\w+Matcher\.(\w+)", text[match.start():close + 1]) and lines.intersection(range(start, end + 1)):
                added.append((path, start, text, match.start(), close))
    if not added:
        return
    sources = {path: blank_literals(text) for path, text in code.items()}
    order = system_order(sources)
    marks = list(destruct_marks(sources))
    for path, line, text, position, close in added:
        statement = text[statement_start(text, position):text.find(";", close)]
        components = matcher_components(statement)
        owners = [span for span in class_spans(text, brace_blocks(text)) if span[1] < position < span[2]]
        if not components or not owners:
            continue
        system, class_start, class_end = max(owners, key=lambda span: span[1])
        field = next(iter(re.findall(r"^\s*(?:this\s*\.\s*)?(\w+)\s*=(?!=)", statement)), None)
        if field:
            body = text[class_start:class_end]
            uses = [use for use in re.finditer(rf"\b{field}\b", body) if not re.match(r"\s*(?:;|=(?!=))", body[use.end():])]
            extinguished = re.findall(rf"\bforeach\s*\(\s*[\w.]+\s+(\w+)\s+in\s+(?:this\s*\.\s*)?{field}\b[^;{{}}]*?\)\s*"
                                      rf"(?:\{{\s*\1\s*\.\s*isDestructed\s*=\s*true\s*;\s*\}}|\1\s*\.\s*isDestructed\s*=\s*true\s*;)", body)
            if uses and len(extinguished) == len(uses):
                yield path, line, "NoneOf(Destructed) у выборки, которую система только гасит: повторная пометка ничего не меняет, убери фильтр (OPTIONAL_ENTITAS.md, «Системы»)"
                continue
        found = [mark for mark in marks if mark.components & components]
        if any(reaches(mark, system, field, order) for mark in found if not mark.removed & components):
            continue
        reasons = [f"{mark.owner}.{mark.member} снимает {', '.join(sorted(mark.removed & components))}" if mark.removed & components
                   else f"{mark.owner}.Cleanup" if mark.member == "Cleanup" else f"{system} гасит обходом этой выборки" if mark.owner == system
                   else f"{mark.owner} после {system}" for mark in found]
        yield path, line, (f"NoneOf(Destructed) без пути: сущность с {', '.join(sorted(components))} не помечают раньше в Execute-проходе "
                           f"или вне ECS-цикла" + (f" ({'; '.join(dict.fromkeys(reasons))})" if reasons else "") +
                           ". OPTIONAL_ENTITAS.md, «Системы»: пути нет — убери фильтр; путь есть, а хук его не видит, — покажи путь владельцу")


def noneof_marker(cwd):
    changed = {path: added_lines(cwd, path) for path in run(["git", "diff", "--cached", "--name-only", "--diff-filter=AM"], cwd).split()
               if path.endswith(".cs") and not any(part in path for part in CODE_EXCLUDES)}
    if not changed:
        return []
    code = {}
    for path in run(["git", "ls-files", "*.cs"], cwd).split():
        if not any(part in path for part in CODE_EXCLUDES) and os.path.isfile(os.path.join(cwd, path)):
            code[path] = open(os.path.join(cwd, path), encoding="utf-8-sig", errors="ignore").read()
    for path in changed:
        code[path] = run(["git", "show", f":{path}"], cwd)
    return [f"{path}:{number}: {message}" for path, number, message in
            [*noneof_problems(code, changed), *destructed_filter_problems(code, changed), *group_kind_problems(code, changed),
              *record_component_problems(code, changed)]]


def system_direct_save(cwd):
    # OPTIONAL_ENTITAS.md, «Реквесты, очистка и финализаторы»: в проекте с запросом isSaveProgressRequest система
    # сохраняет им; сервис зовут только системы самого сохранения.
    if not run(["git", "grep", "-l", "isSaveProgressRequest = true", "--", "*.cs"], cwd).strip():
        return []
    found = []
    for path in run(["git", "diff", "--cached", "--name-only", "--diff-filter=AM"], cwd).split():
        name = os.path.basename(path)
        if not name.endswith("System.cs") or "SaveProgress" in name or "/Tests/" in path:
            continue
        lines = run(["git", "show", f":{path}"], cwd).split("\n")
        for number in sorted(added_lines(cwd, path)):
            if number <= len(lines) and re.search(r"\.SaveProgress\(\)", lines[number - 1]):
                found.append(f"{path}:{number}: система сохраняет напрямую: ставь CreateEntity.Empty().isSaveProgressRequest = true")
    return found


def duplicate_save_stage(cwd):
    # Общая фаза сохранения одна; тестовая композиция не считается регистрацией runtime.
    paths = run(["git", "grep", "--cached", "-l", "SaveProgressOnRequestSystem", "--", "*.cs",
                 ":!*/Tests/*", ":!*/Generated/*", ":!*/Editor/*", ":!*/Plugins/*"], cwd).splitlines()
    registrations = []
    for path in paths:
        source = run(["git", "show", f":{path}"], cwd)
        source = re.sub(r"//[^\n]*|/\*[\s\S]*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group()), source)
        for match in re.finditer(r"\b(?:Create\s*<\s*SaveProgressOnRequestSystem\s*>\s*|new\s+SaveProgressOnRequestSystem\s*)\(", source):
            start = source.count("\n", 0, match.start()) + 1
            end = source.count("\n", 0, match.end()) + 1
            registrations.append((path, start, end))
    if len(registrations) < 2:
        return []
    found = []
    for path, start, end in registrations:
        if added_lines(cwd, path).intersection(range(start, end + 1)):
            found.append(f"{path}:{start}: повторная общая фаза сохранения: оставь единственный SaveProgressOnRequestSystem, зависимые действия ставь после него")
    return found


MEMBER_MODIFIERS = r"(?:(?:static|readonly|const|virtual|override|abstract|sealed|new|async|unsafe|extern)\s+)*"
INTERNAL_MEMBER = re.compile(r"^\s+(?:\[[^\]]*\]\s*)*" + MEMBER_MODIFIERS + r"internal\s+" + MEMBER_MODIFIERS
                             + r"(?!(?:class|struct|interface|enum|record|delegate)\b)[\w<>\[\],.?]+(?:\s*<[^>]*>)?\s+(\w+)"
                             r"\s*(?:<[^>]*>)?\s*(?:\(|=(?!=)|;|\{|=>)")


def unread_internal(cwd):
    # GIT_WORKFLOW.md, «Ревью pull request»: видимость шире private — для читателя снаружи. internal-член, имя
    # которого не встречается ни в одном другом файле, никто извне не читает, и он остаётся private.
    found = []
    for path in run(["git", "diff", "--cached", "--name-only", "--diff-filter=AM"], cwd).splitlines():
        if not path.endswith(".cs") or "Assets/Code/" not in path or "/Generated/" in path or "/Plugins/" in path:
            continue
        lines = run(["git", "show", f":{path}"], cwd).split("\n")
        for number in sorted(added_lines(cwd, path)):
            match = INTERNAL_MEMBER.match(lines[number - 1]) if number <= len(lines) else None
            if not match:
                continue
            readers = run(["git", "grep", "--cached", "-l", "-w", "-e", match.group(1), "--", "*.cs"], cwd).splitlines()
            if not [reader for reader in readers if reader != path]:
                found.append(f"{path}:{number}: internal {match.group(1)} не читают вне файла: оставь private, "
                             "видимость расширяет PR, в котором появляется читатель")
    return found


ENUM_DECLARATION = re.compile(r"^[ \t]*(?:\[[^\]\n]*\]\s*)*(?:(?:public|internal)\s+)?enum\s+(\w+)", re.M)
NAMESPACE_HEAD = re.compile(r"\bnamespace\s+[\w.]+\s*$")
TYPED_MEMBER = re.compile(r"(?:^|[{;])[ \t]*(?:\[[^\]\n]*\]\s*)*(?:(?:public|private|protected|internal)\s+)+" + MEMBER_MODIFIERS
                          + r"(?P<type>[\w.<>\[\],? ]+?)\s+\w+\s*(?:;|=|\{)")
TYPED_PROPERTY = re.compile(r"^[ \t]*(?P<type>[\w.<>\[\],?]+)\s+\w+\s*\{\s*(?:get|set|init)\b")


def enum_owner_folder(cwd):
    # ARCHITECTURE_GUIDELINES.md, «Структура модулей»: перечисление лежит рядом с типами, которые его хранят, а без полей
    # своего типа — рядом с кодом, который его использует. Новое объявление, которое нужно только классам одной подпапки
    # его папки, а другие места его не упоминают, переносится в эту подпапку. Тесты и редактор не в счёт.
    found = []
    for path in run(["git", "diff", "--cached", "--name-only", "--diff-filter=AMR"], cwd).splitlines():
        if not path.endswith(".cs") or "Assets/Code/" not in path or any(part in path for part in (*CODE_EXCLUDES, "/Editor/")):
            continue
        text = blank_literals(run(["git", "show", f":{path}"], cwd))
        blocks = brace_blocks(text)
        added = added_lines(cwd, path)
        folder = os.path.dirname(path) + "/"
        for match in ENUM_DECLARATION.finditer(text):
            number = text.count("\n", 0, match.start(1)) + 1
            if number not in added or any(not NAMESPACE_HEAD.search(text, 0, start) for start, end in blocks
                                          if start < match.start(1) < end):
                continue

            name = match.group(1)
            word = re.compile(rf"\b{name}\b")
            mentions = [line.split(":", 2) for line in run(["git", "grep", "--cached", "--full-name", "-n", "-w", "-e", name,
                                                             "--", "*.cs"], cwd).splitlines()]
            runtime = [(file, line) for file, _, line in mentions if "/Tests/" not in file and "/Editor/" not in file]
            holders = {file for file, line in runtime
                       if any(word.search(member["type"]) for member in [*TYPED_MEMBER.finditer(line), *TYPED_PROPERTY.finditer(line)])}
            owners = holders or {file for file, _ in runtime if file != path}
            if not owners or any(not file.startswith(folder) for file, _ in runtime):
                continue

            home = os.path.commonpath([os.path.dirname(file) for file in owners]) + "/"
            if home != folder:
                found.append(f"{path}:{number}: перечисление {name} нужно только классам из {home}: положи его туда; выше "
                             "лежит перечисление, которое хранят разные подпапки или используют другие модули")
    return found


TYPE_DECLARATION = re.compile(r"\b(class|struct|enum|interface|record)\s+([A-Z]\w*)")
NAME_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+")
TYPED_NAME = re.compile(r"(?<![\w.<\[])(?P<type>[A-Z]\w*)\??\s+(?P<name>@?_?[A-Za-z]\w*)\s*(?==>|[=;,){:]|\bin\b)")


def type_head(name, kind):
    words = NAME_WORDS.findall(name[1:] if kind == "interface" and re.match(r"I[A-Z]", name) else name)
    return words[-2] if len(words) > 1 and words[-1] == "Base" else words[-1]


def type_head_word(cwd):
    # CODE_STYLE.md, «Поля и свойства»: имя хранит главное слово своего типа — последнее слово его имени, сокращают только
    # уточнения перед ним: ShopPurchaseData purchaseData, IStaticDataService staticDataService. Сущности и контексты ECS
    # называются по виду, поле компонента — Value, у абстрактной базы главное слово стоит перед Base.
    staged = [path for path in run(["git", "diff", "--cached", "--name-only", "--diff-filter=AMR"], cwd).splitlines()
              if path.endswith(".cs") and "Assets/Code/" in path and "/Generated/" not in path and "/Plugins/" not in path]
    if not staged:
        return []

    types = {}
    for line in run(["git", "grep", "--cached", "-h", "-E", r"(class|struct|enum|interface|record) [A-Z]", "--",
                     "*Assets/Code/*.cs", ":!*/Generated/*", ":!*/Plugins/*"], cwd).splitlines():
        for kind, name in TYPE_DECLARATION.findall(blank_literals(line)):
            if not name.endswith(("Entity", "Context")):
                types.setdefault(name, kind)
    found = []
    for path in staged:
        added = added_lines(cwd, path)
        for number, line in enumerate(blank_literals(run(["git", "show", f":{path}"], cwd)).split("\n"), 1):
            if number not in added:
                continue

            for match in TYPED_NAME.finditer(line):
                type_name, name = match.group("type", "name")
                if type_name not in types or name == "Value":
                    continue

                head = type_head(type_name, types[type_name])
                if head.lower() not in (word.lower() for word in NAME_WORDS.findall(name.lstrip("_@"))):
                    found.append(f"{path}:{number}: имя {name} теряет главное слово типа {type_name} — {head}: сокращай "
                                 f"уточнения перед ним, а {head} оставь, иначе не видно, что это за объект (ShopPurchaseData purchaseData)")
    return found


def vendor_package_files(cwd):
    # INTEGRATION_GUIDELINES.md, «Установка SDK»: пакет Google (Firebase, EDM, Google Mobile Ads) кладёт список своих файлов
    # `*_version-*_manifest.txt`. Коммит, который трогает такой пакет, оставляет в индексе каждый файл списка, кроме
    # исключённых `.gitignore` и копии EDM в Assets, когда EDM стоит пакетом UPM. Папку пакета можно перенести глубже,
    # например в `Assets/Plugins`: файл с тем же путём внутри пакета считается на месте.
    staged = set(run(["git", "diff", "--cached", "--name-only", "--no-renames", "-z"], cwd).split("\0"))
    indexed = set(run(["git", "ls-files", "--cached", "-z"], cwd).split("\0"))
    found = []
    for manifest in sorted(path for path in indexed if re.search(r"_version-[^/]*_manifest\.txt$", path) and "Assets/" in path):
        root = manifest[:manifest.index("Assets/")]
        listed = {root + line.strip() for line in run(["git", "show", f":{manifest}"], cwd).splitlines() if line.strip()}
        if manifest not in staged and not staged & listed:
            continue

        upm_edm = "com.google.external-dependency-manager" in run(["git", "show", f":{root}Packages/manifest.json"], cwd)
        missing = sorted(path for path in listed - indexed
                         if not (upm_edm and path.startswith(root + "Assets/ExternalDependencyManager/"))
                         and not any(other.endswith("/" + path[len(root + "Assets/"):]) for other in indexed))
        ignored = set(run(["git", "check-ignore", "--no-index", "--", *missing], cwd).splitlines()) if missing else set()
        found.extend(f"{path}: нет файла из списка пакета {manifest}: ставь пакет целиком из официального дистрибутива, "
                     f"большие бинарники — через LFS" for path in missing if path not in ignored)
    return found


def foreign_app_config(cwd):
    # INTEGRATION_GUIDELINES.md, «Установка SDK»: файл приложения из кабинета Firebase (`google-services.json`,
    # `GoogleService-Info.plist`) относится к одному приложению. Коммит, который трогает такой файл или настройки
    # проекта Unity, оставляет в индексе только файлы с Android- или iOS-идентификатором этого проекта.
    staged = set(run(["git", "diff", "--cached", "--name-only", "--no-renames", "-z"], cwd).split("\0"))
    indexed = run(["git", "ls-files", "--cached", "-z"], cwd).split("\0")
    found = []
    for settings in (path for path in indexed if path.endswith("ProjectSettings/ProjectSettings.asset")):
        root = settings[:-len("ProjectSettings/ProjectSettings.asset")]
        configs = [path for path in indexed if path.startswith(root + "Assets/")
                   and path.rsplit("/", 1)[-1] in ("google-services.json", "GoogleService-Info.plist")]
        if settings not in staged and not staged.intersection(configs):
            continue

        block = re.search(r"^  applicationIdentifier:\n((?:    .*\n)*)", run(["git", "show", f":{settings}"], cwd), re.M)
        identifiers = dict(re.findall(r"^    (\w+): (\S+)$", block.group(1), re.M)) if block else {}
        for config in configs:
            text = run(["git", "show", f":{config}"], cwd)
            if config.endswith(".json"):
                clients = json.loads(text).get("client", [])
                names = {client.get("client_info", {}).get("android_client_info", {}).get("package_name") for client in clients}
                expected = identifiers.get("Android")
            else:
                names = set(re.findall(r"<key>BUNDLE_ID</key>\s*<string>([^<]*)</string>", text))
                expected = identifiers.get("iPhone")
            if expected and expected not in names:
                found.append(f"{config}: файл приложения {', '.join(sorted(filter(None, names)))}, а у проекта {expected}: "
                             f"ключи и файлы из кабинетов добавляй при регистрации этого приложения, чужие убери")
    return found

def ecs_group_usage(lines, added):
    # OPTIONAL_ENTITAS.md: у Entity-итератора доменное имя.
    source = blank_literals("\n".join(lines))
    groups = set(re.findall(r"\bIGroup\s*<\s*(?:\w+\.)*\w*Entity\s*>\s+(\w+)\b", source))
    rules = (
        (r"\bforeach\s*\(\s*(?:\w+\.)*\w*Entity\s+_+\s+in\s+[^;{}]*?\)", False,
         "итератор Entity _/__/___: сохрани доменное имя, даже если сущность не читается"),
        (r"\bforeach\s*\(\s*var\s+_+\s+in\s+(?:this\s*\.\s*)?(\w+)"
         r"(?:\s*\.\s*(?:GetEntities\s*\([^()]*\)|AsEnumerable\s*\(\s*\)))?\s*\)", True,
         "итератор Entity _/__/___: сохрани доменное имя, даже если сущность не читается"),
    )
    for pattern, requires_group, message in rules:
        for match in re.finditer(pattern, source):
            if requires_group and match.group(1) not in groups:
                continue
            start = source.count("\n", 0, match.start()) + 1
            end = source.count("\n", 0, match.end()) + 1
            if added.intersection(range(start, end + 1)):
                yield start, message


def analytics_text_constant(lines, added, path):
    # ARCHITECTURE_GUIDELINES.md, «Границы зависимостей»: имя события, ключ и значение параметра аналитики
    # пишутся текстом в вызове; константа с именем, которое повторяет текст, только уводит от вызова.
    analytics_call = re.compile(r"Analytics|DTDUserCard|AdjustEvent")
    production = "/Tests/" not in path and "/Generated/" not in path
    for index, line in enumerate(lines):
        match = re.match(r"\s*(?:(?:public|private|protected|internal|static)\s+)*const\s+string\s+(\w+)\s*=", line)
        if not match or index + 1 not in added:
            continue
        name = match.group(1)
        usages = [other for number, other in enumerate(lines) if number != index and re.search(rf"\b{name}\b", other)]
        sender = production and "Analytics" in path and not name.endswith("FORMAT")
        if "ANALYTICS" in name or sender or production and any(analytics_call.search(usage) for usage in usages):
            yield index + 1, "текст аналитики в const: имя события, ключ и значение параметра пиши текстом в вызове"


def text_template(lines, added, path):
    # UI_HIERARCHY_GUIDELINES.md, «Текст и ссылки»: шаблон текста, который видит игрок, — сериализованное поле рядом
    # с текстом и шаблоном по умолчанию, а не константа и не интерполяция в коде.
    if "/Assets/Code/" not in "/" + path or re.search(r"/(Tests|Editor|Generated)/", path):
        return
    target = r"(?:\.text\s*=|\.SetText\s*\()\s*"
    for number in sorted(number for number in added if number <= len(lines)):
        line = lines[number - 1]
        constant = re.search(target + r"string\.Format\(\s*[A-Z][A-Z0-9_]*\s*,", line)
        interpolated = re.search(target + r'\$"((?:[^"\\]|\\.)*)"', line)
        if constant or interpolated and re.sub(r"\{[^{}]*\}", "", interpolated.group(1)):
            yield number, "шаблон текста в коде: вынеси его в сериализованное поле рядом с текстом, шаблон — значением по умолчанию"


def singular(word):
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes", "zes", "oes", "uses")):
        return word[:-2]
    return word[:-1] if word.endswith("s") and not word.endswith(("ss", "us")) else word


def camel_tail(name):
    return re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name)[-1].lower()


def group_matchers(source):
    for match in re.finditer(r"\b(_\w+)\s*=\s*\w+\s*\.\s*GetGroup\s*\(", source):
        close = closing(source, match.end() - 1)
        matcher = source[match.end():close]
        allof = [component for _, _, components in matcher_clauses(matcher, "AllOf") for component in components]
        components = allof or ([] if "NoneOf" in matcher else re.findall(r"\w+Matcher\s*\.\s*(\w+)", matcher)[:1])
        yield match.group(1), source.count("\n", 0, match.start()) + 1, source.count("\n", 0, close) + 1, components


def ecs_group_iterators(lines, added):
    # OPTIONAL_ENTITAS.md, «Именование»: итератор — имя выборки в единственном числе. _levelAttempts с итератором
    # levelProgress называет одну сущность двумя именами. Пара вложенных обходов одной выборки называет итераторы по роли.
    source = blank_literals("\n".join(lines))
    groups = set(re.findall(r"\bIGroup\s*<\s*(?:\w+\.)*\w*Entity\s*>\s+(\w+)\b", source))
    iterators = {}
    for match in re.finditer(r"\bforeach\s*\(\s*(?:(?:\w+\.)*\w*Entity|var)\s+(\w+)\s+in\s+(?:this\s*\.\s*)?(_\w+)\b", source):
        if match.group(2) in groups:
            iterators.setdefault(match.group(2), []).append((match.group(1), source.count("\n", 0, match.start()) + 1))
    for name, uses in iterators.items():
        base = name.lstrip("_")
        if len({iterator for iterator, _ in uses}) > 1:
            continue
        for iterator, line in uses:
            if singular_words(iterator) != singular_words(base) and iterator.strip("_") and line in added:
                yield line, (f"итератор {iterator} у выборки {name}: итератор — имя выборки в единственном числе ({singular(base)}); "
                             "если перебираешь другую сущность, назови выборку по её виду")


def singular_words(name):
    return "".join(singular(word.lower()) for word in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name))


def data_components(code):
    found = set()
    for text in code.values():
        for match in re.finditer(r"\bclass\s+(\w+)\s*:\s*[^{;]*\bI\w*Component\b[^{;]*\{([^{}]*)\}", blank_literals(text)):
            if re.search(r"\bpublic\s+[\w<>\[\],.? ]+\s+\w+\s*[;=]", match.group(2)):
                found.add(match.group(1))
    return found


def record_component_problems(code, changed):
    # OPTIONAL_ENTITAS.md, «Канон именования»: значение компонента — один факт. Компонент, чей Value — свой класс или
    # структура из нескольких полей, хранит запись: её раскладывают на компоненты, и в запросе тоже. Запись допустима
    # только элементом списка (List<BroomMove>, List<EffectSetup>), поэтому списки и массивы не проверяются, как и view.
    records = {}
    for text in code.values():
        source = blank_literals(text)
        for match in re.finditer(r"\b(?:class|struct)\s+(\w+)\b([^{;]*)\{", source):
            if re.search(r"\bI\w*Component\b|MonoBehaviour|ScriptableObject", match.group(2)):
                continue
            body = source[match.end():block_end(source, match.end())]
            fields = re.findall(r"\bpublic\s+(?!static\b|const\b|override\b|virtual\b|abstract\b)[\w.<>\[\],? ]+?\s+\w+\s*(?:;|=|\{\s*get)", body)
            records.setdefault(match.group(1), len(fields))
    for path, added in changed.items():
        source = blank_literals(code[path])
        for match in re.finditer(r"\bclass\s+(\w+)\s*:[^{]*\bI\w*Component\b[^{]*\{\s*public\s+([\w.]+)\??\s+Value\s*;", source):
            start, end = source.count("\n", 0, match.start()) + 1, source.count("\n", 0, match.end()) + 1
            record = match.group(2).split(".")[-1]
            if records.get(record, 0) >= 2 and added.intersection(range(start, end + 1)):
                yield path, start, (f"{match.group(1)} хранит запись {record} из {records[record]} полей: каждый факт сделай своим "
                                    "компонентом, запись допустима только элементом списка")


def marker_components(code):
    found = set()
    for text in code.values():
        for match in re.finditer(r"\bclass\s+(\w+)\s*:\s*[^{;]*\bI\w*Component\b[^{;]*\{([^{}]*)\}", blank_literals(text)):
            if not re.search(r"\bpublic\s+[\w<>\[\],.? ]+\s+\w+\s*[;=]", match.group(2)):
                found.add(match.group(1))
    return found


def named_marker(name, components, markers):
    # Самый длинный хвост имени выборки, который назван маркером: _coinUnlocks -> CoinUnlock, _unlockedSlots -> Slot.
    # Хвост, которым заканчивается компонент матчера, закрывает проверку: имя опирается на матчер, в том числе на
    # частный вид (_effects по HealEffect, _boltArmaments по BouncingVegetableBoltArmament).
    words = [word[:1].upper() + word[1:] for word in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name.lstrip("_"))]
    for index in range(len(words)):
        tail = "".join(words[index:])
        single = "".join(words[index:-1]) + singular(words[-1])
        if any(component.endswith((tail, single)) for component in components):
            return None
        if single in markers:
            return single
    return None


def group_kind_problems(code, changed):
    # OPTIONAL_ENTITAS.md, «Именование»: выборку называют по виду сущности — первому компоненту матчера. Компонент
    # с данными, который система читает или требует, имя не меняет: AllOf(LevelProgress, LevelAttempt) — это
    # _levelProgress, а не _levelAttempts. Уточнение-состояние перед видом (_animatingSlots) решается на ревью.
    # Имя, которое называет маркер, обязано найти его в матчере: _coins по AllOf(ParentSlotId) держится на совпадении
    # данных, а не на виде, и подхватит сущность другого вида, которой повесят тот же компонент.
    data = data_components(code)
    markers = marker_components(code)
    for path, added in changed.items():
        for name, start, end, components in group_matchers(blank_literals(code[path])):
            if not components or not added.intersection(range(start, end + 1)):
                continue
            absent = named_marker(name, components, markers)
            if absent:
                yield path, start, (f"выборка {name} названа по {absent}, а в матчере его нет: вид сущности ставь первым в AllOf, "
                                    f"даже если выборку уже сужает компонент с данными, иначе назови выборку по виду из матчера "
                                    f"(OPTIONAL_ENTITAS.md, «Канон именования», поле группы)")
                continue
            word = singular(name.lstrip("_")).lower()
            kind = components[0]
            if word.endswith((singular(camel_tail(kind)), kind.lower())):
                continue
            named_by = [component for component in components[1:] if component in data and word.endswith(singular(camel_tail(component)))]
            if named_by:
                yield path, start, (f"выборка {name} названа по компоненту данных {named_by[0]}, а сущность в ней — {kind}: называй выборку "
                                    f"по виду сущности, данные, которые система читает, имя не меняют")


METHOD_HEAD = re.compile(r"^[ \t]*(?:\[[^\]\n]*\]\s*)*(?P<modifiers>(?:(?:public|private|protected|internal|static|override|virtual|"
                         r"abstract|async|sealed|new|partial|unsafe|extern)\s+)+)(?P<type>[\w.<>,\[\]?() ]+?)\s+(?P<name>\w+)\s*"
                         r"(?:<[^>\n]*>)?\s*\(", re.M)
BRANCHING = re.compile(r"\b(?:if|else|for|foreach|while|do|switch|try|catch|return|yield|throw|lock|using|goto)\b|=>|\?(?!\.)")
EXECUTE_LIMIT = 30
GUIDE_WIDTH = 120
WRAP_WIDTH = 140


def method_spans(text):
    # Методы с телом в тексте без литералов: имя, модификаторы, тип, строка объявления, строки скобок тела и само тело.
    for match in METHOD_HEAD.finditer(text):
        if match["type"].strip() in ("class", "struct", "interface", "record", "enum", "delegate", "event", "return", "new", "else"):
            continue
        parameters_end = closing(text, match.end() - 1)
        head = re.match(r"[^{;=]*?(?=\{|;|=>)", text[parameters_end + 1:])
        if not head:
            continue
        start = parameters_end + 1 + head.end()
        if text.startswith(";", start):
            continue
        expression = text.startswith("=>", start)
        end = text.find(";", start) if expression else block_end(text, start + 1)
        yield {"name": match["name"], "modifiers": match["modifiers"].split(), "type": match["type"].strip(),
               "declaration": text.count("\n", 0, match.start("name")) + 1, "opening": text.count("\n", 0, start) + 1,
               "closing": text.count("\n", 0, end) + 1, "body": text[start + (2 if expression else 1):end], "expression": expression}


def execute_lengths(source):
    return [method["closing"] - method["opening"] - 1 for method in method_spans(blank_literals(source))
            if method["name"] == "Execute" and not method["expression"]]


def execute_length(cwd):
    # OPTIONAL_ENTITAS.md, «Системы»: тело Execute — не больше 30 строк, считая пустые строки и внутренние скобки.
    # Ловит Execute, который правка удлинила сверх предела или создала таким; прежняя длина, которую правка не растит, не мешает.
    found, merging = [], merge_head(cwd)
    for path in run(["git", "diff", "--cached", "--name-only", "--diff-filter=AM"], cwd).split():
        if not path.endswith("System.cs") or any(part in f"/{path}" for part in CODE_EXCLUDES):
            continue
        before = [length for revision in ("HEAD", merging) if revision
                  for length in execute_lengths(run(["git", "show", f"{revision}:{path}"], cwd))]
        for method in method_spans(blank_literals(run(["git", "show", f":{path}"], cwd))):
            length = method["closing"] - method["opening"] - 1
            if method["name"] != "Execute" or method["expression"] or length <= EXECUTE_LIMIT or before and length <= max(before):
                continue
            grown = f"было {max(before)}" if before else "новый"
            found.append(f"{path}:{method['declaration']}: Execute — {length} строк тела ({grown}), предел {EXECUTE_LIMIT}: сначала убери "
                         "лишнее — проверку без сценария, переменную-синоним, перенос, — затем вынеси шаг с собственным решением; "
                         "обёртку ради длины не вводи")
    return found


def one_line_wrapper(lines, added, path):
    # CODE_STYLE.md, «Методы»: однострочный метод, который вызывается в одном месте, не заводят — вызов пишется на месте.
    # Многострочная цепочка создания сущности остаётся шагом, привязки инсталлеров — их форма.
    if any(part in f"/{path}" for part in CODE_EXCLUDES) or path.endswith("Installer.cs"):
        return
    text = blank_literals("\n".join(lines))
    spans = list(method_spans(text))
    names = Counter(method["name"] for method in spans)
    for method in spans:
        body = method["body"].strip()
        if ("private" not in method["modifiers"] or method["type"] != "void" or names[method["name"]] > 1 or "\n" in body
                or body.count(";") > 1 or BRANCHING.search(body)):
            continue
        uses = [use.start() for use in re.finditer(rf"\b{method['name']}\b", text) if text.count("\n", 0, use.start()) + 1 != method["declaration"]]
        if len(uses) != 1 or not re.match(rf"{method['name']}\s*\(", text[uses[0]:]):
            continue
        call = text.count("\n", 0, uses[0]) + 1
        if call in added or added.intersection(range(method["declaration"], method["closing"] + 1)):
            yield method["declaration"], f"{method['name']} — однострочный метод с одним вызовом (строка {call}): пиши вызов на месте"


CONDITION_HEAD = re.compile(r"^[ \t]*(?:\}\s*)?(?P<head>(?:else\s+)?(?:if|while))\s*\(", re.M)
BOOL_RELAY = re.compile(r"^[ \t]*bool\s+(?P<name>\w+)\s*=\s*(?P<value>[^;{}]+);[ \t]*\n[ \t]*(?P<head>if)\s*\(\s*(?P<negation>!?)\s*(?P=name)\s*\)", re.M)
STATEMENT_KEYWORD = re.compile(r"(?:if|else|for|foreach|while|do|switch|using|lock|fixed|try|catch|finally|case|default|checked|unchecked|get|set)\b")
SIGNATURE_HEAD = re.compile(r"(?!(?:return|await|throw|new|yield)\b)(?:[\w.<>\[\],?]+\s+)+\w+\s*(?:<[^>]*>)?\s*\(")


def needless_wrap(lines, added):
    # CODE_STYLE.md, «Форматирование» и «Методы»: условие, вызов и присваивание, которые с отступом помещаются в направляющую
    # 120 символов, пишутся одной строкой; до 140 одной строкой можно. bool-переменная, которая только переносит перенесённый
    # вызов в следующий if, не заводится, если условие с вызовом помещается в 140.
    text = re.sub(r'(?P<raw>"{3,})[\s\S]*?(?P=raw)|@"(?:""|[^"])*"|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/',
                  lambda match: re.sub(r"[^\n]", "#" if match.group().startswith("/") or "\n" in match.group() else "x", match.group()), "\n".join(lines))

    def line_of(position):
        return text.count("\n", 0, position) + 1

    def prefix(start):
        return start - text.rfind("\n", 0, start) - 1

    def joined(start, end):
        return re.sub(r"(?<=[(\[]) | (?=[)\]])", "", re.sub(r"\s*\n\s*", " ", text[start:end].strip()))

    def one_line(start, end):
        return prefix(start) + len(joined(start, end))

    for match in CONDITION_HEAD.finditer(text):
        opening = match.end() - 1
        end = closing(text, opening)
        condition = text[opening:end + 1]
        if line_of(opening) == line_of(end) or "=>" in condition or "{" in condition or "#" in condition:
            continue
        length = one_line(match.start("head"), end + 1)
        if length <= GUIDE_WIDTH and added.intersection(range(line_of(opening), line_of(end) + 1)):
            yield line_of(opening), f"условие помещается в строку: одной строкой {length} символов, направляющая {GUIDE_WIDTH}"
    depth, start = 0, 0
    for index, character in enumerate(text):
        depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(character, 0)
        if character in "{}" and not depth:
            start = index + 1
        if character != ";" or depth:
            continue
        statement_start, start = start, index + 1
        while keyword := STATEMENT_KEYWORD.match(text[statement_start:index].lstrip()):
            statement_start += len(text[statement_start:index]) - len(text[statement_start:index].lstrip())
            header = text.find("(", statement_start, index)
            if keyword[0] in ("if", "while", "for", "foreach", "using", "lock", "fixed") and header >= 0:
                statement_start = closing(text, header) + 1
            elif keyword[0] in ("case", "default"):
                statement_start = text.find(":", statement_start) + 1
            elif keyword[0] == "else":
                statement_start += keyword.end()
            else:
                break
        statement_start += len(text[statement_start:index]) - len(text[statement_start:index].lstrip())
        statement = text[statement_start:index + 1]
        parts = statement.split("\n")
        if (len(parts) < 2 or "=>" in statement or re.search(r"[#{}]", statement) or STATEMENT_KEYWORD.match(statement)
                or SIGNATURE_HEAD.match(statement) and "=" not in statement.split("(")[0]
                or any(part.strip().startswith("[") for part in parts)
                or any(part.strip().startswith((".", "?", ":", "+", "&&", "||")) for part in parts[1:])
                or any(part.rstrip().endswith(("&&", "||", "+", "?", ":", "??")) for part in parts[:-1])
                or not added.intersection(range(line_of(statement_start), line_of(index) + 1))):
            continue
        length = one_line(statement_start, index + 1)
        if length <= GUIDE_WIDTH:
            yield line_of(statement_start), f"перенос не нужен: одной строкой {length} символов, направляющая {GUIDE_WIDTH}"
    for match in BOOL_RELAY.finditer(text):
        value = match["value"]
        top = top_level_expression(value) or "?"
        if ("\n" not in value.strip() or "#" in value or len(re.findall(rf"\b{match['name']}\b", text)) != 2
                or match["negation"] and re.search(r"&&|\|\||[=!<>]=|[<>?]|\bis\b|\bas\b", top)):
            continue
        length = prefix(match.start("head")) + len(f"if ({match['negation']}{joined(match.start('value'), match.end('value'))})")
        if length <= WRAP_WIDTH and added.intersection(range(line_of(match.start()), line_of(match.end()) + 1)):
            yield line_of(match.start()), (f"{match['name']} только переносит перенесённый вызов в if: пиши вызов в условии, одной "
                                           f"строкой {length} символов, допуск {WRAP_WIDTH}")


CHECKS = (jump_spacing, config_extension, component_name, expression_wrapping, request_before_save,
          single_return_getter, awake_method, inline_member_condition, inserted_in_list, component_value_initializer, component_record,
          serialized_property_order, dialogue_lifecycle, mono_lifecycle_order, dialogue_lifecycle_order, ecs_group_usage,
          ecs_group_iterators, expression_body_line, needless_wrap, component_context, split_guard)
PATH_CHECKS = (refresh_method_name, unity_time, self_update, animator_lifecycle, anyof_matcher, analytics_text_constant, one_line_wrapper,
               purchase_word, text_template)


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
    # что видит игрок. Слова нет в коде проекта до коммита — оно новое: источник находит и одобряет ассистент, владельца не спрашивают.
    # В первом коммите репозитория словаря ещё нет, сверять не с чем.
    if not run(["git", "rev-parse", "-q", "--verify", "HEAD"], cwd).strip():
        return []
    revisions = ["HEAD"] + ([merge_head(cwd)] if merge_head(cwd) else [])
    identifiers = "\n".join(run(["git", "grep", "-h", "-o", "-I", "-E", "[A-Za-z][A-Za-z0-9_]*", revision, "--", "*.cs"] + VOCABULARY_EXCLUDES, cwd)
                            for revision in revisions)
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
                             f"его SDK, MonoBehaviour или того, что видит игрок. Источник нашёлся — одобри сам из папки проекта: "
                             f"`python3 -B {shlex.quote(os.path.realpath(__file__))} --approve-word {' '.join(words)}`, "
                             f"нет — переименуй. Владельца об отдельном слове не спрашивай")
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


def term_translations(lines, added):
    # UI_HIERARCHY_GUIDELINES.md, «Текст и ссылки»: новый или изменённый ключ заполнен на всех языках проекта.
    languages = sum(1 for line in lines if re.match(r"    - Name: ", line))
    found = []
    for number, line in enumerate(lines, 1):
        term = re.match(r"    - Term: (.+)", line)
        if not term:
            continue
        end = next((i for i in range(number, len(lines)) if re.match(r"    \S", lines[i])), len(lines))
        values = [value.strip() for value in lines[number:end] if re.match(r"      - ", value)]
        if added & set(range(number, end + 1)) and (len(values) < languages or any(value in ("-", '- ""') for value in values)):
            found.append((number, f"ключ {term.group(1)} заполнен не на всех {languages} языках: переведи его сразу"))
    return found


def untranslated_terms(cwd):
    found = []
    for path in run(["git", "diff", "--cached", "--name-only", "--diff-filter=AM"], cwd).split():
        if path.endswith("I2Languages.asset"):
            lines = run(["git", "show", f":{path}"], cwd).split("\n")
            found.extend(f"{path}:{number}: {message}" for number, message in term_translations(lines, added_lines(cwd, path)))
    return found


def paths_equal_to_merge_base(cwd, paths, revision):
    if not paths or not revision:
        return set()

    base = run(["git", "merge-base", "HEAD", revision], cwd).strip()
    if not base:
        return set()

    diff = subprocess.run(["git", "--literal-pathspecs", "diff", "--cached", "--name-only",
                           "--no-renames", "-z", base, "--", *paths], cwd=cwd, capture_output=True, text=True)
    return set(paths) - set(diff.stdout.split("\0")) if diff.returncode == 0 else set()


def restored_pr_base_paths(cwd, paths):
    # Сравниваем целые записи индекса. В paths только файлы, реально отличающиеся от HEAD.
    if not paths:
        return set()

    candidates = paths_equal_to_merge_base(cwd, paths, "refs/remotes/origin/HEAD")
    if not candidates:
        return set()

    # По умолчанию ветка может быть частью стека, поэтому подтверждаем базу открытого PR.
    try:
        result = subprocess.run(["gh", "pr", "view", "--json", "baseRefOid,headRefName,state"],
                                cwd=cwd, capture_output=True, text=True, timeout=4)
        pr = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return set()

    if pr.get("state") != "OPEN" or pr.get("headRefName") != run(["git", "branch", "--show-current"], cwd).strip():
        return set()

    return paths_equal_to_merge_base(cwd, candidates, pr.get("baseRefOid"))


def violations(cwd):
    found = {}
    changes = iter(run(["git", "diff", "--cached", "--name-status", "-M", "--diff-filter=AMR", "-z"], cwd).split("\0"))
    for status in changes:
        if not status:
            break
        previous_path = next(changes)
        renamed = status.startswith("R")
        path = next(changes) if renamed else previous_path
        if not path.endswith(".cs") or "/Generated/" in path:
            continue
        lines = run(["git", "show", f":{path}"], cwd).split("\n")
        added = added_lines(cwd, path)
        problems = list(added_production_comments(cwd, path, lines, added, previous_path))
        if not renamed:
            problems.extend(problem for check in CHECKS for problem in check(lines, added))
            problems.extend(problem for check in PATH_CHECKS for problem in check(lines, added, path))
        messages = [f"{path}:{number}: {message}" for number, message in problems]
        if messages:
            found[path] = messages

    restored = restored_pr_base_paths(cwd, found)
    code_problems = [message for path, messages in found.items() if path not in restored for message in messages]
    return code_problems + new_type_words(cwd) + nested_feature(cwd) + untranslated_terms(cwd) + noneof_marker(cwd) + system_direct_save(cwd) + duplicate_save_stage(cwd) + unread_internal(cwd) + enum_owner_folder(cwd) + type_head_word(cwd) + vendor_package_files(cwd) + foreign_app_config(cwd) + execute_length(cwd)


def self_test():
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    shop_branch = ["if (!TryPurchaseTray(storage))", "{", "    SendStoreOpen();", "    OpenShop();", "    continue;", "}"]
    assert list(jump_spacing(shop_branch, {5})), "continue after two lines needs a blank line"
    assert not list(jump_spacing(["if (!TryPurchaseTray(storage))", "{", "    OpenShop();", "    continue;", "}"], {4})), "Two-line branch keeps continue close"
    assert not list(jump_spacing(["    if (skip)", "        continue;"], {2})), "Braceless guard is not a block"
    multiline_guard = ["{", "    Read();", "    if (first ||", "        second)", "        continue;", "}"]
    assert not list(jump_spacing(multiline_guard, {5})), "Braceless guard with a wrapped condition is not a block"
    switch_sections = ["switch (kind)", "{", "    case First:", "        Apply();", "        break;", "    case Second:",
                       "        Apply();", "        Notify();", "        break;", "    default:", "        throw new InvalidOperationException();", "}"]
    assert [number for number, _ in jump_spacing(switch_sections, set(range(1, 13)))] == [9], "Each case section is its own block"
    failed_load = ["{", "    Debug.LogError(message);", "    Reset();", "    throw new InvalidOperationException(message);", "}"]
    assert list(jump_spacing(failed_load, {4})), "throw after two lines needs a blank line"
    finished_step = ["foreach (GameEntity slot in slots)", "{", "    if (slot.Id != targetId)", "        continue;", "",
                     "    slot.FinishSlotAnimation();", "    break;", "}"]
    assert not list(jump_spacing(finished_step, {7})), "One line after a blank line keeps break close"
    assert not list(jump_spacing(finished_step[:6] + [""] + finished_step[6:], {8})), "A blank line before that break is allowed too"
    assert list(jump_spacing(finished_step[:6] + ["    Notify();"] + finished_step[6:], {8})), "Two lines after a blank line need a blank line"
    nested_blank = ["{", "    if (ready)", "    {", "        Apply();", "", "        Notify();", "    }", "    break;", "}"]
    assert list(jump_spacing(nested_blank, {8})), "A blank line inside a nested block does not split the outer block"
    wrapped = [
        ["if", "    (ready) return;"],
        ["service.Do", "    (first, second);"],
        ["if (first &&", "    second) return;"],
        ["else if (first", "    || second) { }"],
        ["while (TryRead(out value) &&", "    IsReady(value)) Apply(value);"],
        ["do { Apply(); } while (first", "    && second);"],
        ["if (Check(first,", "    second) && third)", "{", "}"],
        ["if (first && second &&", "    third)", "{", "}"],
        ["if (first ||", "    second &&", "    third)", "{", "}"],
        ["for (int index = 0;", "    index < Count(); index++) { }"],
        ["foreach (Entity entity in", "    Entities()) { }"],
        ["foreach (Entity entity in Find(value =>", "    value.Ready)) { }"],
        ["bool prepared =", "    TryPrepare(first, second);"],
        ["bool sameRequest = request.isEnabled &&", "    request.creationIndex == creationIndex;"],
        ["bool enabled = entity", "    .isEnabled;"],
        ["Apply(", "    first,", "    second);"],
        ["Apply(first,", "    second,", "    third);"],
        ["return Other(", "    first, second);"],
        ["service.Do<Foo<Bar>>(", "    first,", "    second);"],
        ["Apply(", '    $"{Format(")")}", second);'],
        ["dialogue.Play(() =>", "    Apply(", "        first,", "        second));"],
        ["private bool IsBusy =>", "    First || Second ||", "    Third;"],
        ["private bool IsBusy =>", "    First &&", "    Second ||", "    Third;"],
        ["private bool IsBusy =>", "    Check(first,", "        second) ||", "    Third;"],
        ["ownedTrays.Sort((left, right) => left.Ready", "    ? Compare(first,", "        second)", "    : Other());"],
        ["Apply(", "    ready", "        ? GetReady()", "        : GetWaiting());"],
        ["return ready", "    ? Read<Foo, Bar>(first,", "        second)", "    : Other();"],
        ["var group = game.GetGroup(GameMatcher", "    .AllOf(GameMatcher.Slot, GetMatcher<Foo<Bar>>(", "        first,", "        second)));"],
        ["bool ready = request.IsEnabled &&", "    request.HasCoins()", "        .Equals(true);"],
        ["var values = source", "    .Where(IsReady)", "    .ToList();"],
        ["private bool TryPrepare(GameEntity entity, MetaEntity storage,", "    out int boardId)", "{", "    boardId = 0;", "    return true;", "}"],
        ["public Dialogue(", "    Service first, Service second) { }"],
        ["public Dialogue(Service first,", "    Service second) { }"],
        ["private bool IsReady(int first,", "    int second) =>", "    first > second;"],
    ]
    for sample in wrapped:
        assert list(expression_wrapping(sample, set(range(1, len(sample) + 1)))), sample
        assert not list(expression_wrapping(sample, {len(sample) + 1})), "Untouched wrapping stays outside the change"
    allowed = [
        ["bool prepared = TryPrepareBoosterUse(storage, booster, needsTarget, ref requestWaitsForBoard, out int boardId, out List<BroomMove> moves);"],
        ["private bool IsBusy =>", "    First || Second;"],
        ["private bool IsBusy() =>", "    First || Second;"],
        ["private bool IsBoardBusy =>", "    _animatingSlots.count > 0 ||", "    _selectedSlots.count > 0 ||", "    _interactActions.count > 0 ||", "    _broomRequests.count > 0;"],
        ["private bool IsBusy() =>", "    First ||", "    Second;"],
        ["return First &&", "    Second &&", "    Third;"],
        ["dialogue.Closed += () =>", "    OnDialogueClosed(dialogue);"],
        ["dialogue.Play(onComplete: () =>", "    OnDialogueClosed(dialogue));"],
        ["ownedTrays.Sort((left, right) => left.Denomination != right.Denomination", "    ? right.Denomination.CompareTo(left.Denomination)", "    : left.Tray.CompareTo(right.Tray));"],
        ["return ready", "    ? GetReady()", "    : GetWaiting();"],
        ['return ready', '    ? "ready"', '    : "waiting";'],
        ["int index = ready", "    ? GetReady()", "    : GetWaiting();"],
        ["Apply(ready", "    ? GetReady()", "    : GetWaiting());"],
        ["Apply(first, ready", "    ? GetReady()", "    : GetWaiting());"],
        ["Apply(first,", "    ready", "        ? GetReady()", "        : GetWaiting());"],
        ["return ready", "    ? cached ?? fallback", "    : value;"],
        ["return CanUse(allowOverride: false) &&", "    CanPay();"],
        ["return Check(a && b) &&", "    IsReady();"],
        ["int value = ready", "    ? Read<Foo, Bar>()", "    : Other();"],
        ["return ready", "    ? Read<Foo, Bar>()", "    : Other();"],
        ["Apply(ready", "    ? Read<Foo, Bar>()", "    : Other());"],
        ["if (fromSlot.ParentBoardId != boardId ||", "    !fromSlot.isSlotUnlocked ||", "    (fromSlot.isSlotFilled && fromSlot.isSlotSingleCoinNumber))", "{", "    continue;", "}"],
        ["else if (first ||", "    second)", "{", "}"],
        ["while (TryRead(out value) &&", "    IsReady(value))", "{", "    Apply(value);", "}"],
        ["do { Apply(); } while (first &&", "    second);"],
        ["bool prepared = TryPrepare(first, second,", "    third);"],
        ["return new SaveBoardChangeProgressSystem(game, meta, staticData, randomProvider, progressProvider,", "    dialogueService, curtainService, introLoadingService);"],
        ["GiveUpLevelSystem system = new(Resolve<GameContext>(), Resolve<MetaContext>(),", "    Resolve<IStaticDataService>(), stateMachine);"],
        ["_system = new ActivateBoosterSystem(game, meta, staticData,", "    dialogueService, progressProvider);"],
        ["return Other(first,", "    second);"],
        ["service.Do(first,", "    () => Finish());"],
        ["dialogue.Play(() =>", "    Apply(first,", "        second));"],
        ['Apply($"{Format(")")}",', '    second);'],
        ["service.Do<Foo<Bar>>(first,", "    second);"],
        ["void SendLevelComplete(int attempt, int seconds, int deals,", "    int moves, int boostersUsed);"],
        ["protected abstract void Apply(int first,", "    int second);"],
        ["private bool TryPrepare(", "    GameEntity entity,", "    Dictionary<int, string> names,", "    out int boardId)", "{", "    boardId = 0;", "    return true;", "}"],
        ["public void Execute(GameEntity entity, int count) { }"],
        ["private void Construct(", "    Service first,", "    Service second) { }"],
        ["[Inject] private void Construct(", "    Service first,", "    Service second) { }"],
        ["GameEntity Create(", "    Config config,", "    int count) { return null; }"],
        ["public Dialogue(", "    Service first,", "    Service second) { }"],
        ["delegate void Callback(", "    int first,", "    int second);"],
        ["if (contextAvailable && TryRead(out value)) { Apply(value); }"],
        ["while (TryRead(out value) && IsReady(value)) { Apply(value); }"],
        ["_slots = game.GetGroup(GameMatcher", "    .AllOf(", "        GameMatcher.Slot,", "        GameMatcher.Id));"],
        ["dialogue.Play(onComplete: () =>", "{", "    Apply();", "});"],
        ["Data value = new Data", "{", "    Count = 1,", "    Ready = true", "};"],
        ["List<int> values = new()", "{", "    1,", "    2", "};"],
        ["GameEntity request = CreateEntity.Empty()", "    .AddWorldPosition(position)", "    .AddActivateBoosterRequest(booster);"],
        ["_doTweenService.CreateSequence(ref _sequence)", "    .Append(Body.DOScale(1f, ShowDuration))", "    .SetLoops(-1);"],
        ["Tween tween = DOTween.To(() => alpha, value => alpha = value, 1f, duration)", "    .SetEase(Ease.Linear);"],
        ["Tween tween = DOVirtual.Float(0f, 1f, duration, SetAlpha)", "    .SetEase(Ease.Linear);"],
        ["Tween tween = DOVirtual.Int(0, lastFrame, duration, SetFrame)", "    .SetEase(Ease.Linear);"],
        ["Tween tween = DOTween.ToAlpha(GetColor, SetColor, 1f, duration)", "    .SetEase(Ease.Linear);"],
        ["Tween tween = DOVirtual.Vector3(start, end, duration, SetPosition)", "    .SetEase(Ease.Linear);"],
        ["_progressTween = DOTween", "    .To(() => progress, SetProgress, value, duration)", "    .SetEase(Ease.Linear);"],
        [r'string text = "if (first &&\nsecond)";'],
        ['string text = $"{Format("(")}";', 'Apply(first, second);'],
        ['string text = @"first', 'second";'],
        ['string text = """', 'first', 'second', '""";'],
        ["// if (first &&", "// second) { }"],
    ]
    for sample in allowed:
        assert not list(expression_wrapping(sample, set(range(1, len(sample) + 1)))), sample
    safe_string = ['string text = $"{Format("(")}";', 'Apply(first, second);']
    assert not list(expression_wrapping(safe_string, {2})), "String punctuation must not affect the next changed statement"
    nested_matcher = ["var group = game.GetGroup(GameMatcher", "    .AllOf(GameMatcher.Slot, GetMatcher<Foo<Bar>>(", "        first,", "        second)));"]
    assert list(expression_wrapping(nested_matcher, {3})), "Matcher composition does not exempt ordinary generic calls"
    nested = ["dialogue.Play(() =>", "{", "    Apply(", "        first,", "        second);", "});"]
    assert list(expression_wrapping(nested, {4})), "Structural callbacks do not exempt their ordinary calls"
    nested_tween = ["Tween tween = DOTween.To(() => Read(", "    first,", "    second), SetAlpha, 1f, duration)", "    .SetEase(Ease.Linear);"]
    assert list(expression_wrapping(nested_tween, {2})), "Tween builders do not exempt nested ordinary calls"
    lone_tween = ["_progressTween = DOTween", "    .To(() => progress, SetProgress, value, duration);"]
    assert list(expression_wrapping(lone_tween, {1, 2})), "A tween without settings is one call: DOTween.To(...)"
    print("expression-layout self-test ok")

    matcher = ["_slots = game.GetGroup(GameMatcher", "    .AnyOf(", "        GameMatcher.Animating,", "        GameMatcher.Selected));"]
    assert len(list(anyof_matcher(matcher, {3}, "Assets/Code/SlotSystem.cs"))) == 1
    assert not list(anyof_matcher(matcher, {1}, "Assets/Code/SlotSystem.cs"))
    assert not list(anyof_matcher(matcher, {3}, "Assets/Code/Generated/Game/GameMatcher.cs"))
    assert not list(anyof_matcher([line.replace("AnyOf", "AllOf") for line in matcher], {3}, "Assets/Code/SlotSystem.cs"))
    print("anyof-matcher self-test ok")

    group_usage = ["private readonly IGroup<GameEntity> _boardChangeAnimations;",
                   "private bool HasBoardChange => _boardChangeAnimations.count > 0;",
                   "private bool CanCollect => _boardChangeAnimations.Count == 0;",
                   "foreach (var _ in _boardChangeAnimations) { }"]
    assert not list(ecs_group_usage(group_usage, {2, 3})), "Pure group-existence checks are allowed"
    assert len(list(ecs_group_usage(group_usage, {4}))) == 1, "Group iterators still require domain names"
    print("ecs-group-usage self-test ok")

    booster_use = ["class ActivateBoosterSystem", "{",
                   '    private const string BROOM_ANALYTICS_NAME = "broom";',
                   "    void Use() => _gameAnalyticsSender.SendBoosterUse(BROOM_ANALYTICS_NAME);", "}"]
    assert len(list(analytics_text_constant(booster_use, {3}, "Assets/Code/ActivateBoosterSystem.cs"))) == 1
    assert not list(analytics_text_constant(booster_use, {4}, "Assets/Code/ActivateBoosterSystem.cs")), "Old constants stay outside the change"
    user_card = ["class DevToDevService", "{",
                 '    private const string AD_TRACKER_ID_PROPERTY_NAME = "ad_tracker_id";',
                 "    void Match(string adid) => DTDUserCard.Set(AD_TRACKER_ID_PROPERTY_NAME, adid);", "}"]
    assert len(list(analytics_text_constant(user_card, {3}, "Assets/Code/DevToDevService.cs"))) == 1
    currency = ["class DevToDevAnalyticsSender", "{", '    private const string IAP_GEMS_SOURCE = "iap";',
                "    bool Bought(string source) => source == IAP_GEMS_SOURCE;", "}"]
    assert len(list(analytics_text_constant(currency, {3}, "Assets/Code/Meta/DevToDev/AnalyticsSender/DevToDevAnalyticsSender.cs"))) == 1
    date = ["class AppMetricaAnalyticsSender", "{", '    private const string DATE_FORMAT = "yyyy-MM-dd";',
            "    string Day(DateTime time) => time.ToString(DATE_FORMAT);", "}"]
    assert not list(analytics_text_constant(date, {3}, "Assets/Code/Meta/AppMetrica/AnalyticsSender/AppMetricaAnalyticsSender.cs")), "Format strings are technical"
    fixture = ["class ShopFeatureTests", "{",
               '    private const string SMALL_BUNDLE_ID = "small_bundle";',
               "    void Buy() => store.Buy(SMALL_BUNDLE_ID);",
               "    void Check() => gameAnalyticsSender.Received().SendIAPPurchase(SMALL_BUNDLE_ID);", "}"]
    assert not list(analytics_text_constant(fixture, {3}, "Assets/Code/Tests/EditMode/ShopFeatureTests.cs")), "Test fixtures may name shared data"
    save_key = ["class SaveLoadService", "{", '    private const string PROGRESS_KEY = "CoinMergeProgress";',
                "    void Save(string json) => PlayerPrefs.SetString(PROGRESS_KEY, json);", "}"]
    assert not list(analytics_text_constant(save_key, {3}, "Assets/Code/SaveLoadService.cs")), "Non-analytics constants stay"
    print("analytics-text-constant self-test ok")

    iterators = ["private readonly IGroup<MetaEntity> _levelAttempts;",
                 "private readonly IGroup<MetaEntity> _hearts;",
                 "private readonly IGroup<GameEntity> _slots;",
                 "private readonly IGroup<GameEntity> _heroes;",
                 "foreach (MetaEntity levelProgress in _levelAttempts) { }",
                 "foreach (MetaEntity hearts in _hearts) { }",
                 "foreach (GameEntity fromSlot in _slots)",
                 "foreach (GameEntity toSlot in _slots) { }",
                 "foreach (GameEntity hero in _heroes) { }"]
    found = list(ecs_group_iterators(iterators, set(range(1, len(iterators) + 1))))
    assert [line for line, _ in found] == [5] and "(levelAttempt)" in found[0][1], found
    assert not list(ecs_group_iterators(iterators, {1, 6, 7, 8, 9})), "Matching names, a nested pair and untouched lines pass"
    print("ecs-group-iterators self-test ok")

    records = ["LevelAttemptData levelAttempt = levelProgress.LevelAttempt;",
               "levelAttempt.Deals++;",
               "levelProgress.ReplaceLevelAttempt(levelAttempt);",
               "LevelAttemptData nextAttempt = new() { Number = 2, StartTimeUtc = now };",
               "levelProgress.ReplaceLevelAttempt(nextAttempt);",
               "levelProgress.AddLevelAttempt(new LevelAttemptData { Number = 1, StartTimeUtc = now });",
               "request.AddShopProductPurchasedRequest(new ShopPurchaseData { ProductId = productId, OrderId = orderId });",
               "Vector3 position = coin.WorldPosition;",
               "position.y = 0;",
               "coin.ReplaceWorldPosition(position);",
               "List<int> coins = slot.SlotCoins;",
               "coins.Add(3);",
               "slot.ReplaceSlotCoins(coins);",
               "request.AddUseBroomRequest(new List<BroomMove> { new BroomMove { CoinId = 1, ToSlotId = 2 } });",
               "levelProgress.ReplaceLevelAttemptDeals(levelProgress.LevelAttemptDeals + 1);"]
    found = list(component_record(records, set(range(1, len(records) + 1))))
    assert [line for line, _ in found] == [3, 5, 6, 7], found
    assert [line for line, _ in component_record(records, {2})] == [3], "A changed field line flags its Replace"
    assert not list(component_record(records, set(range(8, len(records) + 1)))), "Vector fields, list changes, list records and one-fact components pass"
    print("component-record self-test ok")

    components = {"Assets/Code/Meta/LevelProgressComponents.cs": ("[Meta] public class LevelProgress : IComponent { }\n"
                                                                    "[Meta] public class LevelAttempt : ISavedComponent { public int Value; }\n"
                                                                    "[Game] public class Slot : IComponent { }\n"
                                                                    "[Game] public class Animating : IComponent { }\n"
                                                                    "[Game] public class Coin : IComponent { }\n"
                                                                    "[Game] public class ParentSlotId : IComponent { public int Value; }\n"
                                                                    "[Meta] public class CoinUnlock : ISavedComponent { }\n"
                                                                    "[Meta] public class MaxUnlockedCoinNumber : ISavedComponent { public int Value; }\n"
                                                                    "[Game] public class Effect : IComponent { }\n"
                                                                    "[Game] public class HealEffect : IComponent { }\n"
                                                                    "[Game] public class EffectValue : IComponent { public float Value; }")}
    system = "Assets/Code/Gameplay/CountSystem.cs"
    def kind(group):
        code = components | {system: group}
        return [message for _, _, message in group_kind_problems(code, {system: {1, 2, 3}})]
    attempt = "_levelAttempts = meta.GetGroup(MetaMatcher.AllOf(MetaMatcher.LevelProgress, MetaMatcher.LevelAttempt));"
    assert "по компоненту данных LevelAttempt" in kind(attempt)[0]
    assert not kind(attempt.replace("_levelAttempts", "_levelProgress")), "A group named after its kind passes"
    assert not kind("_animatingSlots = game.GetGroup(GameMatcher.AllOf(GameMatcher.Slot, GameMatcher.Animating));"), "A state qualifier is a review matter"
    assert "названа по Coin" in kind("_coins = game.GetGroup(GameMatcher.ParentSlotId);")[0], "A kind name needs its marker in the matcher"
    assert "названа по CoinUnlock" in kind("_coinUnlocks = meta.GetGroup(MetaMatcher.MaxUnlockedCoinNumber);")[0]
    assert not kind("_coins = game.GetGroup(GameMatcher.AllOf(GameMatcher.Coin, GameMatcher.ParentSlotId));"), "The kind in AllOf passes"
    assert not kind("_effects = game.GetGroup(GameMatcher.AllOf(GameMatcher.HealEffect, GameMatcher.EffectValue));"), "A narrower kind passes"
    print("group-kind self-test ok")

    record_code = {"Assets/Code/Meta/ShopComponents.cs": ("[Meta] public class ShopProductPurchasedRequest : IComponent { public ShopPurchaseData Value; }\n"
                                                         "[Meta] public class Gems : ISavedComponent { public int Value; }\n"
                                                         "[Game] public class UseBroomRequest : IComponent { public List<BroomMove> Value; }\n"
                                                         "[Game] public class CoinAnimator : IComponent { public CoinAnimator Value; }\n"
                                                         "[Game] public class BoardSlots : IComponent { public BoardSlot[] Value; }"),
                   "Assets/Code/Meta/ShopPurchaseData.cs": "public class ShopPurchaseData\n{\n    public string ProductId;\n    public string OrderId;\n}",
                   "Assets/Code/Gameplay/BroomMove.cs": "public class BroomMove { public int CoinId; public int ToSlotId; }",
                   "Assets/Code/Gameplay/CoinAnimator.cs": "public class CoinAnimator : MonoBehaviour { public float Speed; public float Delay; }"}
    found = list(record_component_problems(record_code, {"Assets/Code/Meta/ShopComponents.cs": {1, 2, 3, 4, 5}}))
    assert [(line, "ShopPurchaseData из 2 полей" in message) for _, line, message in found] == [(1, True)], found
    assert not list(record_component_problems(record_code, {"Assets/Code/Meta/ShopComponents.cs": {2, 3, 4, 5}})), "Values, lists, arrays and views pass"
    print("record-component self-test ok")

    def noneof(code, path):
        return [message for _, _, message in noneof_problems(code, {path: set(range(1, 50))})]

    start = "Assets/Code/Level/Systems/StartAttemptSystem.cs"
    reached = {start: ("_movers = game.GetGroup(GameMatcher.AllOf(GameMatcher.Mover).NoneOf(GameMatcher.Reached));\n"
                       "mover.isReached = true;"),
               "Assets/Code/Armaments/Systems/CleanupReachedSystem.cs": "armament.isReached = false;"}
    assert not noneof(reached, start), "A flag set by the excluding feature is a leaf, even if another feature clears it"
    attempt = {start: ("_levelProgress = meta.GetGroup(MetaMatcher.AllOf(MetaMatcher.LevelProgress).NoneOf(MetaMatcher.LevelAttempt));\n"
                       "levelProgress.AddLevelAttempt(attempt);"),
               "Assets/Code/Meta/LevelProgressComponents.cs": "[Meta] public class LevelAttempt : ISavedComponent { public int Value; }"}
    assert "NoneOf(LevelAttempt)" in noneof(attempt, start)[0], "A data component is entity state, not a done-marker"
    waiting = {start: "_levelProgress = meta.GetGroup(MetaMatcher.AllOf(MetaMatcher.LevelProgress).NoneOf(MetaMatcher.LevelStartRequired));",
               "Assets/Code/Level/Systems/FinishLevelStartSystem.cs": "levelProgress.isLevelStartRequired = false;",
               "Assets/Code/LevelVictory/Systems/WinSystem.cs": "levelProgress.isLevelStartRequired = true;"}
    assert "NoneOf(LevelStartRequired)" in noneof(waiting, start)[0], "Clearing a marker does not make the feature its owner"
    print("noneof-marker self-test ok")

    same_line = ["private LevelAttemptData Attempt => Data.Attempt;",
                 "    public Foo(IBar bar) => _bar = bar;",
                 "    [Inject] private void Construct(IBar bar) => _bar = bar;",
                 "    public static bool operator ==(Coin left, Coin right) => Equals(left, right);",
                 "    public void Apply(int count = 1) => Add(count);"]
    assert len(list(expression_body_line(same_line, set(range(1, len(same_line) + 1))))) == len(same_line)
    assert not list(expression_body_line(same_line, set())), "Only added lines are checked"
    allowed = ["public bool HasCoins =>",
               "    _coins > 0;",
               "private readonly Action _apply = () => Apply();",
               "    PaymentVerification.Valid => \"valid\",",
               "dialogue.Closed += () => OnDialogueClosed(dialogue);",
               "public int Count { get => _count; set => _count = value; }",
               "    private set => _progress.Level = value;",
               "int Local() => 1;"]
    assert not list(expression_body_line(allowed, set(range(1, len(allowed) + 1))))
    print("expression-body-line self-test ok")

    def destructed(read_body, mark_body, systems=("ReadSystem", "MarkSystem"), changed=True):
        feature = "public class GameplayFeature : Feature\n{\n    public GameplayFeature(ISystemFactory systems)\n    {\n" + "".join(
            f"        Add(systems.Create<{system}>());\n" for system in systems) + "    }\n}\n"
        group = "        _requests = game.GetGroup(GameMatcher.AllOf(GameMatcher.Request).NoneOf(GameMatcher.Destructed));\n"
        system = "public class {0} : IExecuteSystem\n{{\n    private readonly IGroup<GameEntity> _requests;\n\n    public {0}(GameContext game)\n    {{\n{1}    }}\n\n{2}\n}}\n"
        code = {"GameplayFeature.cs": feature, "ReadSystem.cs": system.format("ReadSystem", group, read_body),
                "MarkSystem.cs": system.format("MarkSystem", group.replace(".NoneOf(GameMatcher.Destructed)", ""), mark_body)}
        return [message for _, _, message in destructed_filter_problems(code, {"ReadSystem.cs": {7 if changed else 1}})]

    read = "    public void Execute() => Apply(_requests.count);"
    mark = "    public void Execute()\n    {\n        foreach (GameEntity request in _requests.GetEntities(_buffer))\n            request.isDestructed = true;\n    }"
    assert "MarkSystem после ReadSystem" in destructed(read, mark)[0], "A later system's mark is removed by Cleanup"
    assert not destructed(read, mark, ("MarkSystem", "ReadSystem")), "An earlier system's mark reaches the group"
    assert not destructed(read, mark, changed=False), "Untouched filters are not checked"
    removal = mark.replace("            request.isDestructed", "        {\n            request.isRequest = false;\n            request.isDestructed").replace("= true;\n    }", "= true;\n        }\n    }")
    assert "снимает Request" in destructed(read, removal, ("MarkSystem", "ReadSystem"))[0]
    assert not destructed(read, removal.replace("request.isRequest = false;", "if (Done) request.isRequest = false;"), ("MarkSystem", "ReadSystem")), "A conditional removal keeps the path"
    assert "MarkSystem.Cleanup" in destructed(read, mark.replace("Execute()", "Cleanup()"), ("MarkSystem", "ReadSystem"))[0]
    assert not destructed(read, mark.replace("request.isDestructed = true;", "Play(() => request.isDestructed = true);")), "A callback marks outside the ECS loop"
    assert "только гасит" in destructed(mark, mark)[0]
    assert "ReadSystem гасит обходом" in destructed(mark.replace("            request.isDestructed", "        {\n            Apply(request);\n            request.isDestructed")
                                                       .replace("= true;\n    }", "= true;\n        }\n    }"), "", ("ReadSystem",))[0]
    print("destructed-filter self-test ok")

    for base in ("DialogueBase", "Code.Gameplay.Dialogues.UI.DialogueBase", "ScreenBase", "BaseWindow"):
        for callback in ("Awake", "OnEnable", "Start", "Update", "LateUpdate", "FixedUpdate", "OnDisable", "OnDestroy", "OnUpdate"):
            modifier = "protected override" if callback == "OnUpdate" else "private"
            lines = [f"public class VictoryDialogue : {base}", "{", f"    {modifier} void {callback}() {{ }}", "}"]
            assert len(list(dialogue_lifecycle(lines, {3}))) == 1, (base, callback)
            assert not list(dialogue_lifecycle(lines, {4})), "Untouched legacy callbacks do not block a change"
    lines = ["public class VictoryDialogue : DialogueBase", "{", "    private async void OnEnable() { }", "}"]
    assert len(list(dialogue_lifecycle(lines, {3}))) == 1
    lines[2] = "    private System.Collections.IEnumerator Start() { yield break; }"
    assert len(list(dialogue_lifecycle(lines, {3}))) == 1
    for owner in ("public class DialogueBase : MonoBehaviour", "public class SettingsEditor : EditorWindow"):
        lines = [owner, "{", "    private void OnEnable() { }", "}"]
        assert not list(dialogue_lifecycle(lines, {3})), "The rule is scoped to derived game windows"
    for method in ("Initialize", "SubscribeUpdates", "CleanUp"):
        lines = ["public class VictoryDialogue : DialogueBase", "{", f"    protected override void {method}() {{ }}", "}"]
        assert not list(dialogue_lifecycle(lines, {3})), "Base lifecycle extension points are allowed"
    print("dialogue-lifecycle self-test ok")

    for base in ("MonoBehaviour", "UnityEngine.MonoBehaviour"):
        ordered = [f"public class BoardHUD : {base}", "{",
                   "    public int Count => 1;",
                   "    [Inject] private void Construct() { }",
                   "    private void Start() { Initialize(); }",
                   "    private void OnDestroy() { CleanUp(); }",
                   "    public void Initialize() { }",
                   "    private void SubscribeUpdates() { }",
                   "    public void Display() { }",
                   "    public void CleanUp() { }",
                   "    private void UpdateText() { }", "}"]
        assert not list(mono_lifecycle_order(ordered, set(range(1, len(ordered) + 1))))
        wrong = ordered[:]
        wrong.insert(4, wrong.pop(6))
        assert list(mono_lifecycle_order(wrong, {6})), "New Start must precede Initialize"
        assert list(mono_lifecycle_order(wrong, {5})), "New Initialize must follow Unity callbacks"
        assert not list(mono_lifecycle_order(wrong, {2})), "Untouched legacy declarations do not fail"
        wrong[0] = "public class BoardService"
        assert not list(mono_lifecycle_order(wrong, set(range(1, len(wrong) + 1)))), "Services are not Unity objects"
        wrong = ordered[:]
        wrong[5], wrong[6] = wrong[6], wrong[5]
        assert list(mono_lifecycle_order(wrong, {7})), "Unity callbacks precede Initialize"
        setter = ordered[:4] + ["    public void SetSlot(int slot) { }"] + ordered[4:]
        assert not list(mono_lifecycle_order(setter, set(range(1, len(setter) + 1)))), "The creator's Set follows Construct"
        setter[3], setter[4] = setter[4], setter[3]
        assert list(mono_lifecycle_order(setter, {4})), "Construct precedes the creator's Set"
        setter = ordered[:5] + ["    public void SetSlot(int slot) { }"] + ordered[5:]
        assert list(mono_lifecycle_order(setter, {6})), "A Set does not split Unity callbacks"
        setter = ordered[:8] + ["    public void SetSlot(int slot) { }"] + ordered[8:]
        assert not list(mono_lifecycle_order(setter, set(range(1, len(setter) + 1)))), "A Set called during work is a public method"
        wrong = ordered[:]
        wrong[8], wrong[9] = wrong[9], wrong[8]
        assert list(mono_lifecycle_order(wrong, {9})), "CleanUp follows public methods"
        wrong = ordered[:]
        wrong[9], wrong[10] = wrong[10], wrong[9]
        assert list(mono_lifecycle_order(wrong, {10})), "Private methods follow CleanUp"
        without_cleanup = ordered[:8] + [ordered[10], ordered[8]] + ordered[11:]
        assert not list(mono_lifecycle_order(without_cleanup, set(range(1, len(without_cleanup) + 1)))), "Public and private methods are compared only through CleanUp"
        wrong = ordered[:]
        wrong[3], wrong[4] = wrong[4], wrong[3]
        assert list(mono_lifecycle_order(wrong, {5})), "Construct precedes Unity callbacks"
        ordered[4] = "    private void Start(int count) { }"
        assert list(mono_lifecycle_order(ordered, {5})), "An overload with arguments is an own method, not a Unity callback"
        ordered[4] = "    private static void Start() { }"
        assert list(mono_lifecycle_order(ordered, {5})), "Static methods are not Unity callbacks"
    layout = ["public class RectScaleFitter : MonoBehaviour", "{",
              "    private void Reset() { }", "    private void OnValidate() { }",
              "    private void OnTransformParentChanged() { }", "    private void OnRectTransformDimensionsChange() { }",
              "    private void LateUpdate() { Apply(); }", "    private void Apply() { }", "}"]
    assert not list(mono_lifecycle_order(layout, set(range(1, len(layout) + 1)))), "Layout callbacks stay in the Unity block"
    application = ["public class ApplicationProvider : MonoBehaviour", "{",
                   "    private void OnApplicationPause(bool paused) { }",
                   "    private void OnApplicationFocus(bool focused) { }",
                   "    private void OnDestroy() { }",
                   "    public void Initialize() { }", "}"]
    assert not list(mono_lifecycle_order(application, set(range(1, len(application) + 1))))
    disposal = ["public class Board : MonoBehaviour, IDisposable", "{",
                "    private void OnDestroy() { }", "    void IDisposable.Dispose() { }", "}"]
    assert not list(mono_lifecycle_order(disposal, {3, 4})), "Explicit Dispose belongs to the cleanup block"
    disposal[2], disposal[3] = disposal[3], disposal[2]
    assert list(mono_lifecycle_order(disposal, {3, 4})), "Explicit Dispose follows Unity callbacks"
    unknown_callbacks = ["public class BoardHUD : MonoBehaviour", "{",
                         "    private void Construct() { }",
                         "    private void OnCollisionEnter(Collision collision) { }",
                         "    private void OnDrawGizmos() { }",
                         "    private void Start() { Initialize(); }",
                         "    private void OnDestroy() { CleanUp(); }",
                         "    public void Initialize() { }",
                         "    public void CleanUp() { }", "}"]
    assert not list(mono_lifecycle_order(unknown_callbacks, set(range(1, len(unknown_callbacks) + 1)))), "Unknown On-handlers are not evidence of an own method before Unity callbacks"
    contract = ["public abstract class DialogueBase : MonoBehaviour", "{",
                "    [Inject] private void Construct() { }",
                "    private void Start() { Initialize(); SubscribeUpdates(); }",
                "    private void OnDestroy() => CleanUp();",
                "    public void Show() { }",
                "    protected virtual void Initialize() { }",
                "    protected virtual void SubscribeUpdates() { }",
                "    protected abstract void CleanUp();",
                "    protected void SetCloseButtonVisible(bool isVisible) { }", "}"]
    assert not list(mono_lifecycle_order(contract, set(range(1, len(contract) + 1)))), "A base keeps its declared extension points in one contract block"
    contract[6] = "    protected override void Initialize() { }"
    assert list(mono_lifecycle_order(contract, {7})), "Overrides follow the common order"
    print("mono-lifecycle-order self-test ok")

    for base in ("DialogueBase", "Code.Gameplay.Dialogues.UI.DialogueBase", "ScreenBase", "BaseWindow"):
        dialogue = base.endswith("DialogueBase")
        ordered = [f"public class VictoryDialogue : {base}", "{",
                   "    public List<VideoClip> VideoClips => GetClips();",
                   "    [Inject] private void Construct() { }",
                   "    protected override void Initialize() { }",
                   "    protected override void SubscribeUpdates() { }",
                   "    public void DisplayCompletedLevel(int completedLevel) { }",
                   "    protected override void CleanUp() { }",
                   "    private void PlayContent() { }",
                   "    protected override void OnCloseButtonClick() { }", "}"]
        assert not list(dialogue_lifecycle_order(ordered, set(range(1, len(ordered) + 1))))
        wrong = ordered[:]
        wrong.insert(4, wrong.pop(6))
        assert list(dialogue_lifecycle_order(wrong, {5})), "Display must follow initialization"
        assert not list(dialogue_lifecycle_order(wrong, {2})), "Untouched declarations do not fail"
        payload = "    public void SetSlotIdAndPriceGems(int slotId, int priceGems) { }"
        before = ordered[:4] + [payload] + ordered[4:]
        assert not list(dialogue_lifecycle_order(before, set(range(1, len(before) + 1)))), "The creator's Set follows Construct"
        before_construct = ordered[:3] + [payload] + ordered[3:]
        assert list(dialogue_lifecycle_order(before_construct, {4, 5})), "Construct precedes the creator's Set"
        split = ordered[:5] + [payload] + ordered[5:]
        assert list(dialogue_lifecycle_order(split, {6})), "A Set does not split initialization"
        after = ordered[:6] + [payload] + ordered[6:]
        assert not list(dialogue_lifecycle_order(after, {7})), "A Set called during work stays with public methods"
        late = ordered[:8] + [payload] + ordered[8:]
        assert list(dialogue_lifecycle_order(late, {9})), "Setters precede cleanup"
        for method in ("public static void SetItem(int item) { }", "private void SetItem(int item) { }",
                       "public int DisplayItem(int item) { return item; }"):
            before = ordered[:4] + [f"    {method}"] + ordered[4:]
            assert list(dialogue_lifecycle_order(before, {5})), method
        hooks = ordered[:7] + ["    protected virtual void PlayReward() { }", "    protected abstract void ShowReward();"] + ordered[7:]
        assert not list(dialogue_lifecycle_order(hooks, set(range(1, len(hooks) + 1)))), "Declared extension points stay in the contract block"
        if dialogue:
            hide = "    public override void Hide(bool animate) { }"
            assert not list(dialogue_lifecycle_order(ordered[:6] + [hide] + ordered[6:], {7}))
            assert list(dialogue_lifecycle_order(ordered[:8] + [hide] + ordered[8:], {9})), "Hide precedes CleanUp"
        wrong = ordered[:]
        wrong.insert(5, wrong.pop(3))
        assert list(dialogue_lifecycle_order(wrong, set(range(1, len(wrong) + 1)))), "Construct comes before lifecycle"
        wrong = ordered[:]
        wrong.insert(7, "    private void PlayContent() { }")
        assert list(dialogue_lifecycle_order(wrong, {8})), "Helpers follow cleanup"
        wrong = ordered[:]
        wrong[4], wrong[5] = wrong[5], wrong[4]
        assert list(dialogue_lifecycle_order(wrong, {5, 6})), "Initialize precedes SubscribeUpdates"
        contract = ordered[:]
        contract[4] = "    public void Initialize() { }"
        contract[5] = "    void IWindow.SubscribeUpdates() { }"
        contract.insert(6, "    void IPointerDownHandler.OnPointerDown(PointerEventData eventData) { }")
        contract.insert(9, "    void IDisposable.Dispose() { }")
        assert not list(dialogue_lifecycle_order(contract, set(range(1, len(contract) + 1)))), "Interface lifecycle methods stay in the lifecycle block"
        late_contract = contract[:]
        late_contract[6], late_contract[7] = late_contract[7], late_contract[6]
        if dialogue:
            assert list(dialogue_lifecycle_order(late_contract, {7, 8})), "Interface methods precede public API"
        contract.insert(5, "    public void Play(Action onComplete) { }")
        assert list(dialogue_lifecycle_order(contract, {6})), "Play does not split initialization"
        static = ordered[:]
        static.insert(5, "    public static void Initialize() { }")
        assert list(dialogue_lifecycle_order(static, {6})), "Static methods are not lifecycle implementations"
        unrelated = ordered[:]
        unrelated[0] = "public class VictoryDialogue : OtherBase"
        unrelated.insert(4, unrelated.pop(6))
        assert not list(dialogue_lifecycle_order(unrelated, set(range(1, len(unrelated) + 1))))
    print("dialogue-lifecycle-order self-test ok")

    ordered = [
        "public class VictoryDialogue : DialogueBase",
        "{",
        "    protected override void Initialize() { }",
        "    public override void Hide(bool needCloseAnimation, Action onHidden)",
        "    {",
        "        base.Hide(needCloseAnimation, onHidden);",
        "        _titleReveal?.Kill();",
        "    }",
        "    protected override void CleanUp() { }",
        "    private void UpdateTitleReveal(float time) { }",
        "    private void OnVideoFrameReady(VideoPlayer player, long frame) { }",
        "    protected override void OnCloseButtonClick() { }",
        "}",
    ]
    assert not list(dialogue_lifecycle_order(ordered, set(range(1, len(ordered) + 1))))
    late_hide = ordered[:]
    late_hide[5], late_hide[6] = late_hide[6], late_hide[5]
    assert len(list(dialogue_lifecycle_order(late_hide, {6}))) == 1, "Local callbacks run after base.Hide"
    assert not list(dialogue_lifecycle_order(late_hide, {10})), "Untouched Hide body stays out of scope"
    ordered_steps = late_hide[:5] + ["        foreach (DebugStepper stepper in Steppers())", "            stepper.StopRepeat();"] + late_hide[6:]
    assert not list(dialogue_lifecycle_order(ordered_steps, set(range(1, len(ordered_steps) + 1)))), "Preceding control flow requires manual review"
    print("dialogue-hide-base-first self-test ok")

    with tempfile.TemporaryDirectory() as cwd:
        def git(*args):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

        def stage(path, source):
            file = Path(cwd, path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(source)
            git("add", "--", path)

        owner = "Assets/Code/GameplayFeature.cs"
        shop = "Assets/Code/ShopFeature.cs"
        registration = "Add(systems.Create<SaveProgressOnRequestSystem>());\n"
        git("init", "-q")
        stage(owner, registration)
        git("-c", "core.hooksPath=/dev/null", "-c", "user.name=Hook test", "-c", "user.email=hook@example.invalid",
            "commit", "-qm", "Fixture")
        assert not duplicate_save_stage(cwd)
        stage(shop, registration)
        assert len(duplicate_save_stage(cwd)) == 1
        Path(cwd, shop).write_text("")
        assert len(duplicate_save_stage(cwd)) == 1, "Check the index, not the working copy"
        stage(shop, "")
        stage("Assets/Code/Tests/EditMode/ShopFeatureTests.cs", registration)
        stage("Assets/Code/Editor/Example.cs", registration)
        stage("Assets/Code/Example.cs", "// " + registration + "/* " + registration + "*/\n")
        assert not duplicate_save_stage(cwd)
        stage(shop, "Add(systems.Create<\n    SaveProgressOnRequestSystem\n>());\n")
        assert len(duplicate_save_stage(cwd)) == 1
        stage(shop, "Add(new SaveProgressOnRequestSystem(game, saveLoad));\n")
        assert len(duplicate_save_stage(cwd)) == 1
        git("rm", "--", owner)
        assert not duplicate_save_stage(cwd), "Moving the single owner is allowed"
    print("duplicate-save-stage self-test ok")

    with tempfile.TemporaryDirectory() as cwd:
        def git(*args):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

        def stage(path, source):
            file = Path(cwd, path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(source)
            git("add", "--", path)

        loader = "Assets/Code/StaticDataService.cs"
        constants = ("class StaticDataService\n{\n    private const string SHOP_CONFIG_PATH = \"Configs/shop\";\n"
                     "    internal const string ADJUST_CONFIG_PATH = \"Configs/adjust\";\n"
                     "    private AdjustConfig Load() => Load(ADJUST_CONFIG_PATH);\n}\n")
        git("init", "-q")
        stage(loader, constants)
        problems = unread_internal(cwd)
        assert len(problems) == 1 and "ADJUST_CONFIG_PATH" in problems[0], problems
        stage("Assets/Code/Tests/EditMode/AdjustConfigTests.cs", "Resources.Load(StaticDataService.ADJUST_CONFIG_PATH);\n")
        assert not unread_internal(cwd), "A test reader in another file justifies internal"
        stage(loader, constants.replace("internal const", "private const"))
        git("rm", "-qf", "--", "Assets/Code/Tests/EditMode/AdjustConfigTests.cs")
        assert not unread_internal(cwd)
        stage(loader, "class Box\n{\n    internal sealed class Entry { }\n    internal int Count => _items.Count;\n"
                      "    internal void Clear() { }\n}\n")
        assert sorted(problem.split(": internal ")[1].split()[0] for problem in unread_internal(cwd)) == ["Clear", "Count"], \
            "Nested types are not members"
        stage("Assets/Code/Generated/Lookup.cs", "class Lookup\n{\n    internal const int Total = 1;\n}\n")
        assert all("Generated" not in problem for problem in unread_internal(cwd))
        stage("Assets/GoogleMobileAds/Editor/Utils.cs", "class Utils\n{\n    internal const string AndroidPluginsDir = \"Assets\";\n}\n")
        assert all("GoogleMobileAds" not in problem for problem in unread_internal(cwd)), "Vendor SDK code is not ours"
    print("unread-internal self-test ok")

    with tempfile.TemporaryDirectory() as cwd:
        def git(*args):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

        def stage(path, source):
            file = Path(cwd, path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(source)
            git("add", "--", path)

        shop = "src/game/Assets/Code/Meta/Shop/"
        group = "namespace Code.Meta.Shop\n{\n    public enum ShopGroup\n    {\n        Bundles = 0,\n    }\n}\n"
        def commit():
            git("-c", "core.hooksPath=/dev/null", "-c", "user.name=Hook test", "-c", "user.email=hook@example.invalid",
                "commit", "-qm", "Fixture")

        git("init", "-q")
        stage(shop + "Config/ShopProductData.cs", "public class ShopProductData\n{\n    public ShopGroup Group;\n}\n")
        stage(shop + "Config/ShopConfig.cs", "public class ShopConfig\n{\n    public List<ShopProductData> VisibleProductsOf(ShopGroup group) => null;\n}\n")
        stage(shop + "UI/ShopDialogue.cs", "public class ShopDialogue\n{\n    private void Show() => ShowGroup(ShopGroup.Bundles);\n}\n")
        stage("src/game/Assets/Code/Tests/EditMode/ShopConfigTests.cs", "public class ShopConfigTests\n{\n    private ShopGroup _group;\n}\n")
        stage(shop + "ShopGroup.cs", group)
        problems = enum_owner_folder(cwd)
        assert len(problems) == 1 and problems[0].startswith(shop + "ShopGroup.cs:3:") and shop + "Config/:" in problems[0], problems
        commit()
        stage(shop + "ShopGroup.cs", group.replace("Bundles = 0,", "Bundles = 0,\n        GemsPacks = 1,"))
        assert not enum_owner_folder(cwd), "An old declaration the change does not touch stays"
        git("mv", shop + "ShopGroup.cs", shop + "Config/ShopGroup.cs")
        assert not enum_owner_folder(cwd), "An enum next to its holders passes"
        commit()
        git("mv", shop + "Config/ShopGroup.cs", shop + "ShopGroup.cs")
        assert len(enum_owner_folder(cwd)) == 1, "Moving the enum above its holders is a new declaration there"
        stage(shop + "ShopComponents.cs", "[Meta] public class ShopGroupComponent : IComponent { public ShopGroup Value; }\n")
        assert not enum_owner_folder(cwd), "Holders in different subfolders make the enum module API"
        git("rm", "-qf", "--", shop + "ShopComponents.cs")
        stage("src/game/Assets/Code/Gameplay/Board/BoardHUD.cs", "public class BoardHUD\n{\n    private bool _bundles = Group == ShopGroup.Bundles;\n}\n")
        assert not enum_owner_folder(cwd), "A reader in another module makes the enum module API"
        git("rm", "-qf", "--", "src/game/Assets/Code/Gameplay/Board/BoardHUD.cs")
        stage(shop + "ShopGroup.cs", "public class ShopProductItem\n{\n    public enum ShopGroup { Bundles = 0 }\n}\n")
        assert not enum_owner_folder(cwd), "A nested enum stays with its owner"
        stage(shop + "ShopItemState.cs", "public enum ShopItemState { Locked = 0 }\n")
        stage(shop + "UI/ShopProductItem.cs", "public class ShopProductItem\n{\n    [SerializeField] private ShopItemState _state;\n}\n")
        stage(shop + "Systems/UnlockShopItemSystem.cs", "public class UnlockShopItemSystem\n{\n    private bool Locked => State == ShopItemState.Locked;\n}\n")
        problems = enum_owner_folder(cwd)
        assert len(problems) == 1 and shop + "UI/:" in problems[0], ("Not only config: the enum goes to its holders", problems)
        stage(shop + "UI/ShopProductItem.cs", "public class ShopProductItem\n{\n    private void Show() => Paint(ShopItemState.Locked);\n}\n")
        git("rm", "-qf", "--", shop + "Systems/UnlockShopItemSystem.cs")
        problems = enum_owner_folder(cwd)
        assert len(problems) == 1 and shop + "UI/:" in problems[0], ("Without holders the enum goes to its users", problems)
    print("enum-owner-folder self-test ok")

    with tempfile.TemporaryDirectory() as cwd:
        def git(*args):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

        def stage(path, source):
            file = Path(cwd, path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(source)
            git("add", "--", path)

        shop = "src/game/Assets/Code/Meta/Shop/"
        git("init", "-q")
        stage(shop + "ShopPurchaseData.cs", "public class ShopPurchaseData { }\npublic enum BoosterType { Broom = 0 }\n")
        stage(shop + "IStaticDataService.cs", "public interface IStaticDataService { }\npublic abstract class DialogueBase { }\n")
        stage(shop + "ShopComponents.cs", "public class ShopProductPurchasedRequest : IComponent { public ShopPurchaseData Value; }\n")
        stage("src/game/Assets/Code/Generated/Meta/MetaEntity.cs", "public partial class MetaEntity { }\n")
        stage(shop + "Systems/CollectShopProductSystem.cs", "\n".join([
            "public class CollectShopProductSystem",
            "{",
            "    public CollectShopProductSystem(IStaticDataService staticData, IStaticDataService staticDataService) { }",
            "    private void Collect(MetaEntity storage, DialogueBase dialogue, BoosterType booster)",
            "    {",
            "        ShopPurchaseData purchase = storage.ShopProductPurchasedRequest;",
            "        ShopPurchaseData purchaseData = storage.ShopProductPurchasedRequest;",
            "        string text = \"ShopPurchaseData product = \";",
            "        foreach ((BoosterType boosterType, int count) in Boosters) { }",
            "    }",
            "}",
            ""]))
        problems = type_head_word(cwd)
        flagged = sorted(problem.split(": имя ")[1].split()[0] for problem in problems)
        assert flagged == ["booster", "purchase", "staticData"], problems
    print("type-head-word self-test ok")

    with tempfile.TemporaryDirectory() as cwd:
        def git(*args):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

        def stage(path, source):
            file = Path(cwd, path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(source)
            git("add", "--", path)

        project = "src/game/"
        manifest = project + "Assets/Firebase/Editor/FirebaseAnalytics_version-13.2.0_manifest.txt"
        editor_library = project + "Assets/Firebase/Plugins/x86_64/FirebaseCppApp-13_2_0.bundle"
        git("init", "-q")
        Path(cwd, ".gitignore").write_text("*.pdb\n")
        stage(project + "Packages/manifest.json", '{"dependencies": {"com.google.external-dependency-manager": "1.2.186"}}')
        stage(manifest, "Assets/Firebase/Plugins/Firebase.App.dll\nAssets/Firebase/Plugins/Firebase.App.pdb\n"
                        "Assets/ExternalDependencyManager/Editor/Google.VersionHandler.dll\n"
                        "Assets/Firebase/Plugins/x86_64/FirebaseCppApp-13_2_0.bundle\n")
        stage(project + "Assets/Firebase/Plugins/Firebase.App.dll", "")
        assert vendor_package_files(cwd) == [f"{editor_library}: нет файла из списка пакета {manifest}: ставь пакет целиком "
                                             f"из официального дистрибутива, большие бинарники — через LFS"]
        stage(editor_library, "")
        assert not vendor_package_files(cwd), "Ignored symbols and the Assets copy of UPM EDM are not required"
        git("-c", "core.hooksPath=/dev/null", "-c", "user.name=Hook test", "-c", "user.email=hook@example.invalid",
            "commit", "-qm", "Fixture")
        git("rm", "-q", "--", editor_library)
        assert len(vendor_package_files(cwd)) == 1, "Removing a listed file touches the package"
        git("reset", "-q", "--", editor_library)
        stage(project + "Assets/Code/Example.cs", "")
        assert not vendor_package_files(cwd), "Commits outside the package are not checked"
        git("rm", "-q", "--cached", "--", editor_library)
        assert len(vendor_package_files(cwd)) == 1
        stage(project + "Assets/Plugins/Firebase/Plugins/x86_64/FirebaseCppApp-13_2_0.bundle", "")
        assert not vendor_package_files(cwd), "A package moved under Assets/Plugins keeps its files"
    print("vendor-package-files self-test ok")

    with tempfile.TemporaryDirectory() as cwd:
        def git(*args):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

        def stage(path, source):
            file = Path(cwd, path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(source)
            git("add", "--", path)

        settings = "src/game/ProjectSettings/ProjectSettings.asset"
        android, ios = "src/game/Assets/google-services.json", "src/game/Assets/GoogleService-Info.plist"

        def identifiers(name):
            return f"PlayerSettings:\n  applicationIdentifier:\n    Android: {name}\n    iPhone: {name}.ios\n  buildNumber:\n"

        def client(name):
            return json.dumps({"client": [{"client_info": {"android_client_info": {"package_name": name}}}]})

        git("init", "-q")
        stage(settings, identifiers("com.studio.game"))
        stage(android, client("com.studio.other"))
        assert foreign_app_config(cwd) == [f"{android}: файл приложения com.studio.other, а у проекта com.studio.game: "
                                           f"ключи и файлы из кабинетов добавляй при регистрации этого приложения, чужие убери"]
        stage(android, client("com.studio.game"))
        stage(ios, "<dict>\n\t<key>BUNDLE_ID</key>\n\t<string>com.studio.other</string>\n</dict>\n")
        assert [problem.split(":")[0] for problem in foreign_app_config(cwd)] == [ios]
        stage(ios, "<dict>\n\t<key>BUNDLE_ID</key>\n\t<string>com.studio.game.ios</string>\n</dict>\n")
        assert not foreign_app_config(cwd), "Files of this application pass"
        git("-c", "core.hooksPath=/dev/null", "-c", "user.name=Hook test", "-c", "user.email=hook@example.invalid",
            "commit", "-qm", "Fixture")
        stage("src/game/Assets/Code/Example.cs", "")
        assert not foreign_app_config(cwd), "Commits outside the settings and the files are not checked"
        stage(settings, identifiers("com.studio.renamed"))
        assert len(foreign_app_config(cwd)) == 2, "Changing the identifiers checks the files left in the project"
        git("rm", "-q", "--", android, ios)
        assert not foreign_app_config(cwd), "Removing the files of the old application is allowed"
    print("foreign-app-config self-test ok")

    with tempfile.TemporaryDirectory() as cwd:
        def git(*args):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

        def commit(message):
            git("-c", "core.hooksPath=/dev/null", "-c", "user.name=Hook test", "-c", "user.email=hook@example.invalid",
                "commit", "-qm", message)

        def stage(path, source):
            Path(cwd, path).write_text(source)
            git("add", "--", path)

        restored, edited = "Restored.cs", "Edited.cs"
        before = "class Example {\n    private int Value() =>\n        1 +\n        2;\n}\n"
        after = "class Example {\n    private int Value() =>\n        3;\n}\n"
        git("init", "-q")
        for path in (restored, edited):
            stage(path, before)
        commit("Base")
        default = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/main", default)
        git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        git("checkout", "-qb", "stack-base")
        stage(restored, after)
        commit("Stack base")
        stacked_base = git("rev-parse", "HEAD")
        git("checkout", "-qb", "task")
        stage(edited, after)
        commit("Task")
        stage(restored, before)
        stage(edited, before.replace("2;", "3;"))

        real_run = subprocess.run
        pr = {"baseRefOid": default, "headRefName": "task", "state": "OPEN"}

        def with_pr(args, **kwargs):
            if args[0] == "gh":
                return subprocess.CompletedProcess(args, 0, json.dumps(pr), "")
            return real_run(args, **kwargs)

        with patch.object(subprocess, "run", side_effect=with_pr):
            assert restored_pr_base_paths(cwd, [restored, edited]) == {restored}
            assert any(problem.startswith(edited + ":") for problem in violations(cwd))
            assert not any(problem.startswith(restored + ":") for problem in violations(cwd))
            pr["baseRefOid"] = stacked_base
            assert not restored_pr_base_paths(cwd, [restored, edited])
            assert any(problem.startswith(restored + ":") for problem in violations(cwd))
    print("exact-pr-base-restoration self-test ok")

    def touched(sample):
        return set(range(1, len(sample) + 1))

    fits = ["            if (slot.Id == target.Id ||",
            "                slot.ParentBoardId != target.ParentBoardId ||",
            "                !slot.isSlotUnlocked)",
            "            {", "                continue;", "            }"]
    assert len(list(needless_wrap(fits, touched(fits)))) == 1, "A wrapped condition that fits the 120 guide is joined"
    assert not list(needless_wrap(fits, {5})), "Untouched conditions stay"
    tolerated_condition = ["            if (sourceSlot.Id == target.Id ||",
                           "                sourceSlot.ParentBoardId != target.ParentBoardId ||",
                           "                !sourceSlot.isSlotUnlocked)", "            {", "                continue;", "            }"]
    assert not list(needless_wrap(tolerated_condition, touched(tolerated_condition))), "A condition of 121–140 may stay wrapped"
    long_condition = ["                if (fromSlot.ParentBoardId != boardId ||", "                    !fromSlot.isSlotUnlocked ||",
                      "                    (fromSlot.isSlotFilled && fromSlot.isSlotSingleCoinNumber))", "                {",
                      "                    continue;", "                }"]
    assert not list(needless_wrap(long_condition, touched(long_condition))), "A condition over 140 is wrapped by connectives"
    call = ["            _introLoadingService.SetProgress(IntroLoadingStage.SceneInitialization,", "                progress);"]
    assert len(list(needless_wrap(call, touched(call)))) == 1, "A wrapped call that fits the 120 guide is joined"
    tolerated = ["            _introLoadingService.SetProgress(IntroLoadingStage.SceneInitialization,",
                 "                (float)completedSteps / INITIALIZATION_STEPS_COUNT);"]
    assert not list(needless_wrap(tolerated, touched(tolerated))), "Between 120 and 140 one line is allowed, not required"
    column = ["                _heartsUIService.Update(", "                    hearts.Hearts,", "                    _heartsConfig.MaxHearts,",
              "                    TimeLeftToFullRestore(hearts));"]
    assert len(list(needless_wrap(column, touched(column)))) == 1, "Arguments one per line that fit the guide are joined"
    tail = ["                    bool hasExtraTray = storage.TryBuyBoosterIfNeeded(BoosterType.ExtraTray, extraTray.PriceGems,",
            "                        extraTray.PurchaseCount, out bool bought);"]
    assert not list(needless_wrap(tail, touched(tail))), "A compact tail over 140 stays"
    text_argument = ["            EditorUtility.DisplayDialog(\"Invalid Folder\", \"Select a folder inside this Unity project's Assets directory.\",",
                     "                \"OK\", \"Cancel\", \"Never ask again in this editor session\");"]
    assert not list(needless_wrap(text_argument, touched(text_argument))), "Text literals count by their length"
    structural = [
        ["            return lowerCoin != null &&", "                   lowerCoin.CoinNumber == coin.CoinNumber;"],
        ["            int hiddenCoins = isHidden", "                ? coinsToDeal - openTopCoins", "                : 0;"],
        ["            CreateEntity.Empty()", "                .isSaveProgressRequest = true;"],
        ["            _group = game.GetGroup(GameMatcher", "                .AllOf(GameMatcher.Slot));"],
        ["            if (ready)", "                return;"],
        ["            foreach (GameEntity slot in _slots)", "                slot.isSlotFilled = false;"],
        ["        [Header(\"Sounds\")]", "        [SerializeField] private AudioClip _clip;"],
        ["        bool TryGetPraiseBefore(int step,", "            out LocalizedString praise);"],
        ["            dialogue.Closed += () =>", "                OnDialogueClosed(dialogue);"],
        ["            if (slotSnapshot.Obstacles is { Count: 0 })", "                slotSnapshot.Obstacles = null;"],
        ["            string message = \"first part\" +", "                \"second part\";"],
        ["            string text = @\"first", "second\";"],
    ]
    for sample in structural:
        assert not list(needless_wrap(sample, touched(sample))), sample
    relay = ["                    bool hasReroll = storage.TryBuyBoosterIfNeeded(BoosterType.Reroll, reroll.PriceGems,",
             "                        reroll.PurchaseCount, out bool bought);", "                    if (!hasReroll)", "                        return false;"]
    assert any("hasReroll" in message for _, message in needless_wrap(relay, touched(relay))), "A bool relayed to the next if goes into the condition"
    used_twice = relay + ["                    Log(hasReroll);"]
    assert not any("hasReroll" in message for _, message in needless_wrap(used_twice, touched(used_twice))), "A bool read again is a name"
    print("needless-wrap self-test ok")

    wrapper = ["public class LobbyEnterState", "{", "    public void Enter()", "    {", "        InitializeAudio();", "    }", "",
               "    private void InitializeAudio()", "    {", "        _audioService.PreloadLobbySounds();", "    }", "}"]
    path = "Assets/Code/Infrastructure/States/LobbyEnterState.cs"
    assert len(list(one_line_wrapper(wrapper, {8, 9, 10, 11}, path))) == 1, "A one-line method with one call is written in place"
    assert len(list(one_line_wrapper(wrapper, {5}, path))) == 1, "A new call to a one-line method is caught"
    assert not list(one_line_wrapper(wrapper, {3}, path)), "Untouched wrappers stay"
    expression = wrapper[:7] + ["    private void InitializeAudio() =>", "        _audioService.PreloadLobbySounds();", "}"]
    assert len(list(one_line_wrapper(expression, touched(expression), path))) == 1, "Expression bodies count as one line"
    allowed = [
        wrapper[:4] + ["        InitializeAudio();", "        InitializeAudio();"] + wrapper[5:],
        wrapper[:4] + ["        _button.onClick.AddListener(InitializeAudio);"] + wrapper[5:],
        wrapper[:9] + ["        CreateEntity.Empty()", "            .isSaveProgressRequest = true;"] + wrapper[10:],
        wrapper[:7] + ["    private bool IsReady()", "    {", "        return _audioService.IsReady();", "    }", "}"],
    ]
    for sample in allowed:
        assert not list(one_line_wrapper(sample, touched(sample), path)), sample
    assert not list(one_line_wrapper(wrapper, touched(wrapper), "Assets/Code/Infrastructure/Installers/BootstrapInstaller.cs")), "Installer bindings"
    assert not list(one_line_wrapper(wrapper, touched(wrapper), "Assets/Code/Tests/PlayMode/LobbyTests.cs")), "Tests"
    print("one-line-wrapper self-test ok")

    with tempfile.TemporaryDirectory() as cwd:
        def git(*args):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

        def stage(path, source):
            file = Path(cwd, path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(source)
            git("add", "--", path)

        def system(lines):
            body = "\n".join(f"        Step{index}();" for index in range(lines))
            return f"public class BuySlotSystem\n{{\n    public void Execute()\n    {{\n{body}\n    }}\n}}\n"

        buy = "Assets/Code/Interact/Systems/BuySlotSystem.cs"
        deal = "Assets/Code/Deal/Systems/DealCoinsSystem.cs"
        git("init", "-q")
        stage(buy, system(30))
        stage(deal, system(43))
        git("-c", "core.hooksPath=/dev/null", "-c", "user.name=Hook test", "-c", "user.email=hook@example.invalid", "commit", "-qm", "Fixture")
        stage(buy, system(31))
        assert len(execute_length(cwd)) == 1, "Execute grown over 30"
        stage(buy, system(30))
        stage(deal, system(43).replace("Step0", "Renamed"))
        assert not execute_length(cwd), "An old long Execute the change does not grow stays"
        stage(deal, system(42))
        assert not execute_length(cwd), "Shortening a long Execute passes"
        stage(deal, system(44))
        assert len(execute_length(cwd)) == 1, "Growing a long Execute fails"
        stage(deal, system(43))
        stage("Assets/Code/Board/Systems/NewSystem.cs", system(31))
        assert len(execute_length(cwd)) == 1, "A new Execute over 30"
        stage("Assets/Code/Board/Systems/NewSystem.cs", system(30))
        stage("Assets/Code/Tests/PlayMode/FakeSystem.cs", system(40))
        assert not execute_length(cwd), "Tests stay out"
    print("execute-length self-test ok")

    component_lines = ["[Game, Meta] public class Gems : ISavedComponent { public int Value; }",
                       "[Game] public class DebugGems : IComponent { public int Value; }",
                       "[Game, Meta] public class Id : IComponent { [PrimaryEntityIndex] public int Value; }",
                       "[Game, Input, Meta] public class Destructed : IComponent { }",
                       "[Game, Event(EventTarget.Self)] public class Animating : IComponent { }",
                       "[Meta, ComponentName(\"Game\")] public class Hearts : ISavedComponent { public int Value; }",
                       "[Game][Meta] public class Hearts : IComponent { }"]
    flagged = [number for number, _ in component_context(component_lines, set(range(1, len(component_lines) + 1)))]
    assert flagged == [1, 7], flagged
    print("component-context self-test ok")

    purchase_lines = ["public class BuyHeartsSystem : IExecuteSystem",
                      "private bool TryBuyExtraTrayIfNeeded(MetaEntity storage)",
                      "if (!storage.TryPurchaseBoosterIfNeeded(boosterType, out bool bought))",
                      "foreach (GameEntity buyAction in _buyActions)",
                      "public int BoughtCount => _boughtCount;",
                      "DTDAccrualType accrualType = source == \"iap\" ? DTDAccrualType.Bought : DTDAccrualType.Earned;",
                      "_dialogueService.Open<BuyTrayDialogue>(DialogueOpenMode.PushHidePrevious);",
                      "string eventName = \"buy_tray\"; // buy",
                      "if (!TryPurchaseTray(storage, trayUnlock))",
                      "return bought;",
                      "private readonly IGroup<GameEntity> _buyActions;"]
    every_line = set(range(1, len(purchase_lines) + 1))
    flagged = [number for number, _ in purchase_word(purchase_lines, every_line, "src/game/Assets/Code/Meta/Shop/PurchaseSystem.cs")]
    assert flagged == [1, 2, 3, 4, 5, 11], flagged
    assert not list(purchase_word(purchase_lines, every_line, "src/game/Assets/Plugins/Store/Store.cs")), "SDK code stays out"
    print("purchase-word self-test ok")

    reroll = ["if (request.isUseRerollRequest && slotSnapshots == null)", "    continue;", "",
              "if (request.isUseRerollRequest && !TrySpendReroll())", "    continue;"]
    assert [number for number, _ in split_guard(reroll, {4})] == [4], "Guards sharing an && operand and the exit are one check"
    assert not list(split_guard(reroll, set())), "Untouched guards stay"
    assert not list(split_guard(reroll[:4] + ["    break;"], {4})), "Different exits are different checks"
    assert not list(split_guard(reroll[:3] + ["Read();"] + reroll[3:], set(range(1, 7)))), "A statement between guards splits them"
    assert not list(split_guard(["if (a && b || c)", "    return;", "if (a && d)", "    return;"], {1, 3})), "A top-level || is no && chain"
    stages = ["if (!coin.hasCoinObstacleType || coin.CoinObstacleType != obstacle)", "    continue;", "",
              "if (obstacle == ObstacleType.Resin && !coin.isResinCoin)", "    continue;", "",
              "if (obstacle == ObstacleType.HiddenColor && ContinuesHiddenGroup(coin))", "    continue;"]
    assert not list(split_guard(stages, set(range(1, 9)))), "Guards without a shared operand are separate stages"
    print("split-guard self-test ok")

    texts = ['RemainingCoinsText.text = string.Format(REMAINING_COINS_FORMAT, count);', 'ProgressText.text = $"{percent}%";',
             'CountText.text = $"{count}";', 'RemainingCoinsText.text = string.Format(RemainingCoinsFormat, count);',
             'LevelText.text = string.Format(LevelFormatLocalizedString, level);', 'TimerText.SetText($"{minutes:00}:{seconds:00}");']
    hud = "Assets/Code/Gameplay/Features/Level/Behaviours/LevelHUD.cs"
    assert [number for number, _ in text_template(texts, set(range(1, 7)), hud)] == [1, 2, 6], "Constant and interpolated templates go to a field"
    assert not list(text_template(texts, {3, 4, 5}, hud)), "A bare value, a field and a LocalizedString stay"
    assert not list(text_template(texts, {1, 2}, "Assets/Code/Tests/PlayMode/LevelTests.cs")), "Tests are not player text"
    assert not list(text_template(texts, {2}, "Assets/ResourcesRaw/Epic Toon FX/Demo/Scripts/Projectile.cs")), "Imported assets are not project code"
    print("text-template self-test ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
    elif len(sys.argv) > 2 and sys.argv[1] == "--approve-word":
        approve_words(sys.argv[2:], os.getcwd())
    else:
        print("\n".join(violations(sys.argv[1] if len(sys.argv) > 1 else ".")))
