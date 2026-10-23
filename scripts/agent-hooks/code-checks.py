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


def awake_subscription(lines, added):
    # CODE_STYLE.md, «Паттерны Unity»: подписки MonoBehaviour делаются в Start (или Initialize из Start) и снимаются
    # в OnDestroy; Awake — только подготовка, без которой объект нельзя использовать сразу после Instantiate.
    for index, line in enumerate(lines):
        if not re.match(r"\s*(private|protected)?\s*void Awake\(\)", line):
            continue
        end = index + 2 if line.rstrip().endswith("=>") else block_body_end(lines, index + 1)
        body = lines[index:end]
        if any(re.search(r"\+=|AddListener\(", text) for text in body) and added.intersection(range(index + 1, end + 1)):
            yield index + 1, "подписка в Awake: подписывайся в Start, снимай в OnDestroy"


def refresh_method_name(lines, added, path=""):
    # CODE_STYLE.md, «Типы»: Refresh в имени — только у ECS-системы, которая проталкивает состояние (Refresh…System);
    # метод view, окна или сервиса, который перерисовывает или принимает данные, называется Update….
    if path.endswith("System.cs") or "/Editor/" in path:
        return
    for number in sorted(added):
        if number <= len(lines) and re.search(r"\b(void|bool|int|float|string|Task)\s+Refresh\w*\(", lines[number - 1]):
            yield number, "метод с Refresh вне системы: называй его Update… по обновляемой части"


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


def matcher_clauses(text, matcher):
    for match in re.finditer(rf"\.{matcher}\(", text):
        depth, close = 0, match.end() - 1
        while close < len(text):
            depth += {"(": 1, ")": -1}.get(text[close], 0)
            if depth == 0:
                break
            close += 1
        yield text.count("\n", 0, match.start()) + 1, text.count("\n", 0, close) + 1, re.findall(r"\w+Matcher\.(\w+)", text[match.start():close + 1])


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
    return [f"{path}:{number}: {message}" for path, number, message in noneof_problems(code, changed)]


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


CHECKS = (return_spacing, config_extension, component_name, wrapped_return, vertical_call, multiline_expression_method, request_before_save,
          single_return_getter, awake_subscription, inline_member_condition)
PATH_CHECKS = (refresh_method_name,)


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


def violations(cwd):
    found = []
    for path in run(["git", "diff", "--cached", "--name-only", "--diff-filter=AM"], cwd).split():
        if not path.endswith(".cs") or "/Generated/" in path:
            continue
        lines = run(["git", "show", f":{path}"], cwd).split("\n")
        added = added_lines(cwd, path)
        for check in CHECKS:
            found.extend(f"{path}:{number}: {message}" for number, message in check(lines, added))
        for check in PATH_CHECKS:
            found.extend(f"{path}:{number}: {message}" for number, message in check(lines, added, path))
    return found + new_type_words(cwd) + nested_feature(cwd) + untranslated_terms(cwd) + noneof_marker(cwd) + system_direct_save(cwd)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--approve-word":
        approve_words(sys.argv[2:], os.getcwd())
    else:
        print("\n".join(violations(sys.argv[1] if len(sys.argv) > 1 else ".")))
