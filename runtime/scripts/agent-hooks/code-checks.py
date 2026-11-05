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
    # OPTIONAL_ENTITAS.md: extension не заводят на конфиг и данные; чтение/миграция корневого ProgressData — исключение ARCHITECTURE_GUIDELINES.md.
    for number in sorted(added):
        if number > len(lines):
            continue
        line = lines[number - 1]
        if not re.search(r"static\s+[^(]*\(\s*this\s+\w+(Config|Data|Definition)\b", line):
            continue
        if re.search(r"\bValidate\(\s*this\s+ProgressData\b", line) or re.search(
                r"\bReadFrom\(\s*this\s+ProgressData\s+\w+\s*,\s*string\s+\w+\s*\)", line):
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
    # CODE_STYLE.md: точки входа Unity стоят перед собственными методами MonoBehaviour, включая Initialize/CleanUp.
    owner = next((match for line in lines if (match := re.match(
        r"^([ \t]*)(?:(?:public|internal|abstract|sealed|partial)\s+)*class\s+\w+\s*:\s*(?:UnityEngine\.)?MonoBehaviour\b", line))), None)
    if not owner:
        return
    indent = len(owner.group(1)) + 4
    callbacks, own_methods = [], []
    for number, line in enumerate(lines, 1):
        if len(line) - len(line.lstrip()) != indent:
            continue
        text = re.sub(r"^\s*(?:\[[^\]]+\]\s*)+", "", line)
        method = re.match(r"\s*(?:(?:public|protected|internal|private|static|virtual|override|sealed|async)\s+)*[\w.<>,?\[\]]+\s+(\w+)(?:<[^>()]*>)?\s*\(", text)
        if not method:
            continue
        name = method.group(1)
        if name == "Construct":
            continue
        callback = re.match(r"\s*(?:(?:public|protected|internal|private|virtual|override|sealed|async)\s+)*(?:void|(?:System\.Collections\.)?IEnumerator)\s+"
                            r"(?:(?:Awake|OnEnable|Start|FixedUpdate|Update|LateUpdate|OnDisable|OnDestroy|Reset|OnValidate|OnTransformParentChanged|OnTransformChildrenChanged|OnRectTransformDimensionsChange|OnApplicationQuit)\s*\(\s*\)"
                            r"|(?:OnApplicationFocus|OnApplicationPause)\s*\(\s*bool\s+\w+\s*\))", text)
        if callback:
            callbacks.append(number)
        elif not re.match(r"On[A-Z]", name):
            own_methods.append((number, name))
    if not callbacks:
        return
    for number, name in own_methods:
        if number < callbacks[-1] and added.intersection(set(callbacks) | {number}):
            yield number, f"{name} перед Unity callbacks: сначала точки входа Unity, затем собственные Initialize/CleanUp и остальные методы"


def dialogue_lifecycle_order(lines, added):
    # CODE_STYLE.md: lifecycle базы идёт единым блоком после DI и исходных данных диалога.
    owner = next((match for line in lines if (match := re.match(
        r"^([ \t]*)(?:(?:public|internal|abstract|sealed|partial)\s+)*class\s+\w+\s*:\s*(?:\w+\.)*(DialogueBase|ScreenBase|BaseWindow)\b", line))), None)
    if not owner:
        return
    indent = len(owner.group(1)) + 4
    lifecycle_names = ("Initialize", "SubscribeUpdates", "CleanUp")
    methods = []
    for number, line in enumerate(lines, 1):
        if len(line) - len(line.lstrip()) != indent:
            continue
        text = re.sub(r"^\s*(?:\[[^\]]+\]\s*)+", "", line)
        method = re.match(r"\s*(public|protected|internal|private)\s+([^=;{}()]+\s+)(\w+)(?:<[^>()]*>)?\s*\(", text)
        if not method:
            continue
        name = method.group(3)
        overriding = "override" in method.group(2).split()
        if overriding and name not in lifecycle_names:
            continue
        role = "method"
        if overriding:
            role = "lifecycle"
        elif name == "Construct":
            role = "construct"
        elif owner.group(2) == "DialogueBase" and method.group(1) == "public":
            modifiers_and_type = method.group(2).split()
            if modifiers_and_type[-1] == "void" and "static" not in modifiers_and_type and re.match(r"(?:Set(?!On[A-Z])|Display)[A-Z]", name):
                arguments = text[method.end():].strip()
                if not arguments:
                    arguments = next((following.strip() for following in lines[number:] if following.strip()), "")
                if arguments and not arguments.startswith(")"):
                    role = "payload"

        methods.append((number, name, role))

    lifecycle = [(number, name) for number, name, role in methods if role == "lifecycle"]
    if not lifecycle:
        return

    first_lifecycle, last_lifecycle = lifecycle[0][0], lifecycle[-1][0]
    last_construct = max((number for number, _, role in methods if role == "construct"), default=0)
    boundaries = {number for number, _ in lifecycle}
    for number, name, role in methods:
        changed_lines = boundaries | {number}
        if role == "payload":
            wrong_place = number < last_construct or first_lifecycle < number < last_lifecycle
            changed_lines.add(last_construct)
        else:
            wrong_place = role == "construct" and number > first_lifecycle or role == "method" and number < last_lifecycle

        if wrong_place and added.intersection(changed_lines):
            yield number, f"{name} нарушает блок lifecycle: Construct, исходные данные диалога, Initialize/SubscribeUpdates/CleanUp, остальные методы"
    for previous, current in zip(lifecycle, lifecycle[1:]):
        if lifecycle_names.index(previous[1]) > lifecycle_names.index(current[1]) and added.intersection({previous[0], current[0]}):
            yield current[0], "порядок lifecycle базы: Initialize, SubscribeUpdates, CleanUp"


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
    # OPTIONAL_ENTITAS.md: у Entity-итератора доменное имя; наличие группы проверяют обходом, не count.
    source = "\n".join(lines)
    source = re.sub(r'(?P<raw>"{3,})[\s\S]*?(?P=raw)|@"(?:""|[^"])*"|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/',
                    lambda match: re.sub(r"[^\n]", " ", match.group()), source)
    groups = set(re.findall(r"\bIGroup\s*<\s*(?:\w+\.)*\w*Entity\s*>\s+(\w+)\b", source))
    group_count = r"(?<![\w.])(?:this\s*\.\s*)?(\w+)\s*\.\s*(?:count|Count)\b"
    comparison = r"(?:==|!=|<=|>=|<|>)"
    rules = (
        (r"\bforeach\s*\(\s*(?:\w+\.)*\w*Entity\s+_+\s+in\s+[^;{}]*?\)", False,
         "итератор Entity _/__/___: сохрани доменное имя, даже если сущность не читается"),
        (r"\bforeach\s*\(\s*var\s+_+\s+in\s+(?:this\s*\.\s*)?(\w+)"
         r"(?:\s*\.\s*(?:GetEntities\s*\([^()]*\)|AsEnumerable\s*\(\s*\)))?\s*\)", True,
         "итератор Entity _/__/___: сохрани доменное имя, даже если сущность не читается"),
        (group_count + r"\s*" + comparison + r"\s*0(?![\w.])", True,
         "наличие ECS-группы через count/Count: используй foreach с доменным именем и ранним выходом или флагом"),
        (r"(?<![\w.])0\s*" + comparison + r"\s*" + group_count, True,
         "наличие ECS-группы через count/Count: используй foreach с доменным именем и ранним выходом или флагом"),
    )
    for pattern, requires_group, message in rules:
        for match in re.finditer(pattern, source):
            if requires_group and match.group(1) not in groups:
                continue
            start = source.count("\n", 0, match.start()) + 1
            end = source.count("\n", 0, match.end()) + 1
            if added.intersection(range(start, end + 1)):
                yield start, message


CHECKS = (return_spacing, config_extension, component_name, wrapped_return, vertical_call, multiline_expression_method, request_before_save,
          single_return_getter, awake_method, inline_member_condition, inserted_in_list, component_value_initializer,
          serialized_property_order, dialogue_lifecycle, mono_lifecycle_order, dialogue_lifecycle_order, ecs_group_usage)
PATH_CHECKS = (refresh_method_name, unity_time, self_update, animator_lifecycle)


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
    return found + new_type_words(cwd) + nested_feature(cwd) + untranslated_terms(cwd) + noneof_marker(cwd) + system_direct_save(cwd) + duplicate_save_stage(cwd)


def self_test():
    import tempfile
    from pathlib import Path

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
                   "    public void CleanUp() { }", "}"]
        assert not list(mono_lifecycle_order(ordered, set(range(1, len(ordered) + 1))))
        wrong = ordered[:]
        wrong.insert(4, wrong.pop(6))
        assert len(list(mono_lifecycle_order(wrong, {5}))) == 1, "Initialize must not precede Unity entry points"
        assert len(list(mono_lifecycle_order(wrong, {6}))) == 1, "A new Unity callback checks earlier method declarations"
        assert not list(mono_lifecycle_order(wrong, {2})), "Untouched legacy declarations do not fail"
        wrong[0] = "public class BoardService"
        assert not list(mono_lifecycle_order(wrong, set(range(1, len(wrong) + 1)))), "Services are not Unity objects"
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
                   "    private void OnDestroy() { }", "    public void Initialize() { }", "}"]
    assert not list(mono_lifecycle_order(application, set(range(1, len(application) + 1))))
    unknown_callbacks = ["public class BoardHUD : MonoBehaviour", "{",
                         "    private void Construct() { }",
                         "    private void OnCollisionEnter(Collision collision) { }",
                         "    private void OnDrawGizmos() { }",
                         "    private void Start() { Initialize(); }",
                         "    private void OnDestroy() { CleanUp(); }",
                         "    public void Initialize() { }", "    public void CleanUp() { }", "}"]
    assert not list(mono_lifecycle_order(unknown_callbacks, set(range(1, len(unknown_callbacks) + 1)))), "Unknown On-handlers are not evidence of an own method before Unity callbacks"
    print("mono-lifecycle-order self-test ok")

    for base in ("DialogueBase", "Code.Gameplay.Dialogues.UI.DialogueBase", "ScreenBase", "BaseWindow"):
        ordered = [f"public class VictoryDialogue : {base}", "{",
                   "    public List<VideoClip> VideoClips => GetClips();",
                   "    [Inject] private void Construct() { }",
                   "    protected override void Initialize() { }",
                   "    protected override void SubscribeUpdates() { }",
                   "    protected override void CleanUp() { }",
                   "    public void DisplayCompletedLevel() { }",
                   "    protected override void OnCloseButtonClick() { }", "}"]
        assert not list(dialogue_lifecycle_order(ordered, set(range(1, len(ordered) + 1))))
        wrong = ordered[:]
        wrong.insert(4, wrong.pop(7))
        assert len(list(dialogue_lifecycle_order(wrong, {5}))) == 1, "Public method before lifecycle must fail"
        assert not list(dialogue_lifecycle_order(wrong, {2})), "Untouched declarations do not fail"
        for payload in ("public void SetSlotIdAndPriceGems(int slotId, int priceGems) { }",
                        "public void DisplayCompletedLevel(int completedLevel) { }"):
            before = ordered[:4] + [f"    {payload}"] + ordered[4:]
            assert bool(list(dialogue_lifecycle_order(before, {5}))) == (not base.endswith("DialogueBase"))
            before_construct = ordered[:3] + [f"    {payload}"] + ordered[3:]
            assert len(list(dialogue_lifecycle_order(before_construct, {4, 5}))) == 1, "DI precedes opening data"
            split = ordered[:5] + [f"    {payload}"] + ordered[5:]
            assert len(list(dialogue_lifecycle_order(split, {6}))) == 1, "Opening data does not split lifecycle"
            after = ordered[:7] + [f"    {payload}"] + ordered[7:]
            assert not list(dialogue_lifecycle_order(after, {8})), "Ongoing setters can follow lifecycle"
        for method in ("public static void SetItem(int item) { }", "private void SetItem(int item) { }",
                       "public int DisplayItem(int item) { return item; }",
                       "public void SetOnFlowCompleted(Action callback) { }"):
            before = ordered[:4] + [f"    {method}"] + ordered[4:]
            assert len(list(dialogue_lifecycle_order(before, {5}))) == 1, method
        wrong = ordered[:]
        wrong.insert(5, wrong.pop(3))
        assert list(dialogue_lifecycle_order(wrong, set(range(1, len(wrong) + 1)))), "Construct comes before lifecycle"
        wrong = ordered[:]
        wrong.insert(5, "    private void PlayContent() { }")
        assert len(list(dialogue_lifecycle_order(wrong, {6}))) == 1, "Helpers do not split lifecycle"
        wrong = ordered[:]
        wrong[4], wrong[6] = wrong[6], wrong[4]
        assert list(dialogue_lifecycle_order(wrong, {5, 7})), "Lifecycle follows base order"
        unrelated = ordered[:]
        unrelated[0] = "public class VictoryDialogue : OtherBase"
        unrelated.insert(4, unrelated.pop(7))
        assert not list(dialogue_lifecycle_order(unrelated, set(range(1, len(unrelated) + 1))))
    print("dialogue-lifecycle-order self-test ok")

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


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
    elif len(sys.argv) > 2 and sys.argv[1] == "--approve-word":
        approve_words(sys.argv[2:], os.getcwd())
    else:
        print("\n".join(violations(sys.argv[1] if len(sys.argv) > 1 else ".")))
