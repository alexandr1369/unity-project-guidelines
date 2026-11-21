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


def return_spacing(lines, added):
    # CODE_STYLE.md: перед return пустая строка, если в блоке больше двух строк на экране; при двух — вплотную.
    # Строки считаются по экрану: return с инициализатором объекта или цепочкой на несколько строк — уже не «две строки».
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
        count = before + executable_lines(lines, index, statement_end + 1)
        blank = not lines[index - 1].strip()
        if count <= 2 and blank:
            yield index + 1, "перед return лишняя пустая строка: в блоке две строки"
        elif count > 2 and not blank:
            yield index + 1, "перед return нужна пустая строка: в блоке больше двух строк"


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
    # CODE_STYLE.md: обычные выражения и заголовки — одной строкой, структурная раскладка сохраняется.
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
        chain = builder and re.search(r"\n\s*\.\w+(?:<[^\n]+>)?\s*\(", expression)
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

    for opening, closing in sorted(pairs.items()):
        prefix_start = source.rfind("\n", 0, len(source[:opening].rstrip())) + 1
        prefix = source[prefix_start:opening].strip()
        prefix = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", prefix)
        declaration = re.match(r"(?:(?:public|private|protected|internal|static|override|abstract|virtual|async|sealed|partial|new|delegate)\s+)+[^=;{}]*$", prefix)
        local_declaration = re.fullmatch(r"[\w.<>,?\[\]]+\s+\w+(?:<[^>]+>)?", prefix) and prefix.split()[0] not in ("return", "await", "new", "throw", "yield", "else")
        if (declaration and not prefix.startswith("new ") or local_declaration) and "=>" not in prefix:
            declarations.append((opening, closing))
            continue
        control = re.search(r"\b(if|while|for|foreach)\s*$", prefix)
        if "\n" not in source[prefix_start:closing]:
            continue
        if control:
            report(prefix_start, closing, "заголовок управления перенесён: оставь его одной физической строкой, сохрани частоту вычислений цикла")
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
        report(prefix_start, closing, "аргументы вызова перенесены: обычный вызов записывай одной физической строкой независимо от ширины")

    for match in expression_heads:
        if any(start < match.start() < end for start, end in declarations):
            continue
        end = expression_end(match.end())
        expression = source[match.end():end]
        inspected = layout[match.end():end].strip() if match["arrow"] else layout[match.end():end].rstrip()
        if "\n" not in inspected or structural(expression):
            continue
        report(match.start(), end, "выражение перенесено: присваивание, return или выражение после => записывай одной физической строкой")
    yield from (problem for _, _, problem in sorted(problems))


def inserted_in_list(lines, added):
    # CODE_STYLE.md, «Порядок объявления»: новая привязка в инсталлере дописывается в конец своего метода. Поля,
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
    # CODE_STYLE.md, «Поля и свойства»: свойство из одного выражения пишется через =>, даже если условие
    # переносится по строкам; get { return ...; } с единственным return не пишется.
    for index, line in enumerate(lines):
        if line.strip() != "get" or index + 1 >= len(lines) or lines[index + 1].strip() != "{":
            continue
        end = block_body_end(lines, index + 1)
        body = " ".join(text.strip() for text in lines[index + 2:end] if text.strip())
        if body.startswith("return ") and body.count(";") == 1 and added.intersection(range(index + 1, end + 2)):
            yield index + 1, "свойство из одного выражения: пиши через =>, условие можно перенести по строкам"


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
    return os.path.dirname(folder) if os.path.basename(folder) == "Systems" else folder


def noneof_problems(code, changed):
    # OPTIONAL_ENTITAS.md, white-list и black-list: NoneOf — по маркеру «уже сделано», который ставят и исключают системы
    # одной фичи, по идущему процессу (…ing, …Processing) или у близнецов одного модуля: одна система берёт AllOf(X),
    # другая NoneOf(X), а X — вид сущности, его нигде не снимают. Маркер, который исключают в фиче, где его не ставят
    # и где нет близнеца, — фильтр «кто в выборке» (выход из игры): только положительным <Субъект>Active в AllOf.
    # View ставит фабрика вида, у Destructed отдельное правило.
    excluders, includers = {}, {}
    for path, text in code.items():
        for _, _, components in noneof_clauses(text):
            for component in components:
                excluders.setdefault(component, set()).add(path)
        for _, _, components in matcher_clauses(text, "AllOf"):
            for component in components:
                includers.setdefault(component, set()).add(code_feature(path))
    verdicts = {}
    for path, added in changed.items():
        for start, end, components in noneof_clauses(code[path]):
            if not added.intersection(range(start, end + 1)):
                continue
            for component in components:
                if component in ("View", "Destructed") or PROCESS_MARKER.search(component):
                    continue
                if component not in verdicts:
                    setter = re.compile(rf"\bis{component}\s*=\s*\w+|\.(Add|Replace){component}\(")
                    remover = re.compile(rf"\bis{component}\s*=\s*false\b|\.Remove{component}\(")
                    features = {code_feature(other) for other, text in code.items() if setter.search(text)}
                    twins = includers.get(component, set()) if not any(remover.search(text) for text in code.values()) else set()
                    verdicts[component] = all(code_feature(other) in features | twins for other in excluders[component])
                if not verdicts[component]:
                    yield path, start, f"NoneOf({component}) — маркер исключают и вне фичи, где его ставят, а близнеца с AllOf в этом модуле нет: это фильтр «кто в выборке»: выход из игры — положительным <Субъект>Active в AllOf, «ещё не» и правило игры — проверкой в теле"


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
    return [f"{path}:{number}: {message}" for path, number, message in [*noneof_problems(code, changed), *destructed_filter_problems(code, changed)]]


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


CHECKS = (return_spacing, config_extension, component_name, expression_wrapping, request_before_save,
          single_return_getter, awake_method, inline_member_condition, inserted_in_list, component_value_initializer,
          serialized_property_order, dialogue_lifecycle, mono_lifecycle_order, dialogue_lifecycle_order, ecs_group_usage)
PATH_CHECKS = (refresh_method_name, unity_time, self_update, animator_lifecycle, anyof_matcher)


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
                             f"его SDK, MonoBehaviour или того, что видит игрок. Источник нашёлся — одобри сам: code-checks.py "
                             f"--approve-word {' '.join(words)}, нет — переименуй. Владельца об отдельном слове не спрашивай")
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
    return code_problems + new_type_words(cwd) + nested_feature(cwd) + untranslated_terms(cwd) + noneof_marker(cwd) + system_direct_save(cwd) + duplicate_save_stage(cwd)


def self_test():
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    wrapped = [
        ["if (first &&", "    second)", "    return;"],
        ["if", "    (ready) return;"],
        ["service.Do", "    (first, second);"],
        ["else if (first ||", "    second) { }"],
        ["while (TryRead(out value) &&", "    IsReady(value)) { }"],
        ["do { Apply(); } while (first &&", "    second);"],
        ["for (int index = 0;", "    index < Count(); index++) { }"],
        ["foreach (Entity entity in", "    Entities()) { }"],
        ["bool prepared = TryPrepare(first,", "    second, third);"],
        ["bool prepared =", "    TryPrepare(first, second);"],
        ["bool sameRequest = request.isEnabled &&", "    request.creationIndex == creationIndex;"],
        ["return Other(first,", "    second);"],
        ["bool enabled = entity", "    .isEnabled;"],
        ["service.Do(first,", "    () => Finish());"],
        ["dialogue.Play(() =>", "    Apply(first,", "        second));"],
        ["private bool IsBusy =>", "    First || Second ||", "    Third;"],
        ["private bool IsBusy =>", "    First &&", "    Second ||", "    Third;"],
        ["private bool IsBusy =>", "    Check(first,", "        second) ||", "    Third;"],
        ["ownedTrays.Sort((left, right) => left.Ready", "    ? Compare(first,", "        second)", "    : Other());"],
        ["Apply(first,", "    ready", "        ? GetReady()", "        : GetWaiting());"],
        ["Apply(", "    ready", "        ? GetReady()", "        : GetWaiting());"],
        ["foreach (Entity entity in Find(value =>", "    value.Ready)) { }"],
        ['Apply($"{Format(")")}",', '    second);'],
        ["service.Do<Foo<Bar>>(first,", "    second);"],
        ["return ready", "    ? Read<Foo, Bar>(first,", "        second)", "    : Other();"],
        ["var group = game.GetGroup(GameMatcher", "    .AllOf(GameMatcher.Slot, GetMatcher<Foo<Bar>>(first,", "        second)));"],
        ["bool ready = request.IsEnabled &&", "    request.HasCoins()", "        .Equals(true);"],
        ["var values = source", "    .Where(IsReady)", "    .ToList();"],
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
        ["return ready", "    ? cached ?? fallback", "    : value;"],
        ["return CanUse(allowOverride: false) &&", "    CanPay();"],
        ["return Check(a && b) &&", "    IsReady();"],
        ["int value = ready", "    ? Read<Foo, Bar>()", "    : Other();"],
        ["return ready", "    ? Read<Foo, Bar>()", "    : Other();"],
        ["Apply(ready", "    ? Read<Foo, Bar>()", "    : Other());"],
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
    nested_matcher = ["var group = game.GetGroup(GameMatcher", "    .AllOf(GameMatcher.Slot, GetMatcher<Foo<Bar>>(first,", "        second)));"]
    assert list(expression_wrapping(nested_matcher, {3})), "Matcher composition does not exempt ordinary generic calls"
    nested = ["dialogue.Play(() =>", "{", "    Apply(first,", "        second);", "});"]
    assert list(expression_wrapping(nested, {4})), "Structural callbacks do not exempt their ordinary calls"
    nested_tween = ["Tween tween = DOTween.To(() => Read(first,", "    second), SetAlpha, 1f, duration)", "    .SetEase(Ease.Linear);"]
    assert list(expression_wrapping(nested_tween, {2})), "Tween builders do not exempt nested ordinary calls"
    print("single-line-expression self-test ok")

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


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
    elif len(sys.argv) > 2 and sys.argv[1] == "--approve-word":
        approve_words(sys.argv[2:], os.getcwd())
    else:
        print("\n".join(violations(sys.argv[1] if len(sys.argv) > 1 else ".")))
