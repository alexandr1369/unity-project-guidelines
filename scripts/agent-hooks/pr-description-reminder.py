#!/usr/bin/env python3
# Хук Claude Code и Codex (PostToolUse): после любого git push и gh pr create называет открытые PR,
# в которых есть коммиты новее последней правки описания, и кладёт агенту правила описания из GIT_WORKFLOW.md.
# После git commit перечисляет новые типы и публичные поля и свойства коммита и просит назвать, откуда взято каждое
# слово (CODE_STYLE.md).
# Напоминает снова, пока описание не обновлено или сверка не отмечена: pr-description-reminder.py --checked <номер PR>...
# После gh pr merge напоминает синхронизацию из GIT_WORKFLOW.md, «После merge».
import json
import os
import re
import subprocess
import sys
from datetime import datetime

QUERY = """query($owner:String!,$name:String!){repository(owner:$owner,name:$name){pullRequests(states:OPEN,first:30){nodes{
number title body createdAt lastEditedAt baseRefName headRefName}}}}"""


def run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=6)


def state_path(cwd):
    git_dir = run(["git", "rev-parse", "--git-common-dir"], cwd).stdout.strip()
    return os.path.join(cwd, git_dir, "pr-description-checked.json")


def load_state(cwd):
    try:
        return json.load(open(state_path(cwd), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def only_tasks_file(oid, cwd):
    return run(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", oid], cwd).stdout.split() == ["TASKS.md"]


def stale_pull_requests(cwd):
    result = run(["gh", "api", "graphql", "-F", "owner={owner}", "-F", "name={repo}", "-f", "query=" + QUERY], cwd)
    if result.returncode != 0:
        return None
    checked = load_state(cwd)
    stale = []
    for pr in json.loads(result.stdout)["data"]["repository"]["pullRequests"]["nodes"]:
        since = pr["lastEditedAt"] or pr["createdAt"]
        since_ts = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        args = ["git", "log", "--no-merges", "--format=%H %ct %s", f"origin/{pr['baseRefName']}..origin/{pr['headRefName']}"]
        acked = checked.get(str(pr["number"]))
        if acked and run(["git", "cat-file", "-e", acked], cwd).returncode == 0:
            args.append("^" + acked)
        fresh = []
        for line in run(args, cwd).stdout.splitlines():
            oid, timestamp, headline = line.split(" ", 2)
            if int(timestamp) > since_ts and not only_tasks_file(oid, cwd):
                fresh.append((oid, headline))
        if fresh:
            stale.append((pr, since, fresh))
    return stale


def mark_checked(numbers, cwd):
    state = load_state(cwd)
    for number in numbers:
        branch = run(["gh", "pr", "view", number, "--json", "headRefName", "-q", ".headRefName"], cwd).stdout.strip()
        head = run(["git", "rev-parse", "--verify", "-q", f"origin/{branch}"], cwd).stdout.strip() if branch else ""
        if not head:
            sys.exit(f"PR #{number} не найден")
        state[number] = head
        print(f"Сверка описания PR #{number} отмечена на {head[:9]}")
    json.dump(state, open(state_path(cwd), "w", encoding="utf-8"), indent=1)


def new_type_names(cwd):
    if len(run(["git", "rev-list", "--parents", "-n", "1", "HEAD"], cwd).stdout.split()) > 2:
        return []
    names = []
    path = ""
    for line in run(["git", "show", "--format=", "-U0", "HEAD", "--", "*.cs"], cwd).stdout.splitlines():
        if line.startswith("+++ "):
            path = line[4:]
            continue
        match = (re.match(r"\+.*\b(?:class|struct|enum|interface)\s+(\w+)", line)
                 or re.match(r"\+\s*(?:\[[^\]]*\]\s*)*public\s+(?!class|struct|enum|interface|static|override)[\w<>\[\],. ?]+?\s+(\w+)\s*(?:[;={]|=>)", line))
        if match and "/Generated/" not in path and "/Tests/" not in path and match.group(1) not in names:
            names.append(match.group(1))
    return names


def new_components(cwd):
    if len(run(["git", "rev-list", "--parents", "-n", "1", "HEAD"], cwd).stdout.split()) > 2:
        return []
    components = []
    path = ""
    for line in run(["git", "show", "--format=", "-U0", "HEAD", "--", "*.cs"], cwd).stdout.splitlines():
        if line.startswith("+++ "):
            path = line[4:]
            continue
        match = re.match(r"\+.*\bclass\s+(\w+)\s*:\s*I(?:Saved)?Component\b", line)
        if match and "/Generated/" not in path and "/Tests/" not in path:
            components.append(match.group(1))
    return components


def merged_branches(cwd):
    # GIT_WORKFLOW.md, «После merge»: влитые task-ветки удаляются, а не копятся. Ветки чужих worktree не трогаем.
    main = run(["git", "rev-parse", "-q", "--verify", "origin/main"], cwd).stdout.strip()
    if not main:
        return [], []
    busy = {line.split("refs/heads/", 1)[1] for line in run(["git", "worktree", "list", "--porcelain"], cwd).stdout.splitlines()
            if line.startswith("branch refs/heads/")}
    local = []
    for line in run(["git", "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"], cwd).stdout.splitlines():
        name, oid = line.split()
        if name == "main" or name in busy or oid == main:
            continue
        if subprocess.run(["git", "merge-base", "--is-ancestor", oid, main], cwd=cwd).returncode == 0:
            local.append(name)
    remote = [name.strip() for name in run(["git", "branch", "-r", "--merged", main], cwd).stdout.splitlines()
              if name.strip() not in ("origin/main",) and "->" not in name]
    return local, remote


def merged_branches_note(cwd):
    local, remote = merged_branches(cwd)
    notes = []
    if local:
        notes.append("Влитые локальные ветки — удали `git branch -d`: " + ", ".join(local) + ".")
    if remote:
        notes.append("Влитые ветки на сервере — удаление `git push origin --delete` только со словом владельца, спроси: "
                     + ", ".join(remote) + ".")
    return " ".join(notes)


def workflow_rules():
    workflow = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "skills", "unity-project-guidelines", "GIT_WORKFLOW.md")
    rules = []
    for line in open(workflow, encoding="utf-8"):
        if not rules and not line.startswith("- Описание PR по умолчанию"):
            continue
        if not line.strip():
            break
        rules.append(line.rstrip())
    return "\n".join(rules)


if len(sys.argv) > 2 and sys.argv[1] == "--checked":
    mark_checked(sys.argv[2:], os.getcwd())
    sys.exit(0)

data = json.load(sys.stdin)
tool_input = data.get("tool_input") or {}
command = tool_input.get("command") or tool_input.get("cmd") or ""
if isinstance(command, list):
    command = " ".join(command)

START = r"(?:^|[;&|(]\s*)"
is_push = re.search(START + r"git (?:-C \S+ )?push\b", command, re.M)
is_pr_create = re.search(START + r"gh pr create\b", command, re.M)
is_commit = re.search(START + r"git (?:-C \S+ )?commit\b(?!-)", command, re.M)
is_merge = re.search(START + r"gh pr merge\b", command, re.M)
is_sync = re.search(START + r"git (?:-C \S+ )?(?:fetch|pull)\b", command, re.M)
if is_merge and not is_push and not is_pr_create and not is_commit:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
        "PR влит на сервере. До ответа владельцу синхронизируй (GIT_WORKFLOW.md, «После merge»): локальный main — "
        "`git fetch origin main:main` или `git checkout main && git pull --ff-only`; влитую task-ветку удали, проверив "
        "`git log origin/main..<ветка>`; в дочерние PR влей origin/main и запушь; для ветки checkout пересобери csproj."}},
        ensure_ascii=False))
    sys.exit(0)
if is_sync and not is_push and not is_pr_create and not is_commit:
    sync_target = re.search(START + r"(?:cd|git -C) (\S+)", command, re.M)
    sync_cwd = os.path.expanduser(sync_target.group(1).strip("'\"")) if sync_target else data.get("cwd") or os.getcwd()
    note = merged_branches_note(sync_cwd)
    if note:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}, ensure_ascii=False))
    sys.exit(0)
if not is_push and not is_pr_create and not is_commit:
    sys.exit(0)

target = re.search(START + r"(?:cd|git -C) (\S+)", command, re.M)
cwd = os.path.expanduser(target.group(1).strip("'\"")) if target else data.get("cwd") or os.getcwd()

if is_commit and not is_push and not is_pr_create:
    names = new_type_names(cwd)
    components = new_components(cwd)
    messages = []
    if names:
        messages.append(
            "Новые имена в коммите: " + ", ".join(names) + ". Для каждого до push назови, откуда слово: так понятие уже "
            "называется в модуле или соседних системах, в методах его MonoBehaviour, или так его видит игрок; имя ключа из конфига ГД не источник "
            "(CODE_STYLE.md, «Перед тем как назвать…»). Слово, придуманное с нуля, — переименовать, как Retiring → "
            "Leaving и Reserve → Next в обновлении доски.")
    if components:
        messages.append(
            "Новые компоненты: " + ", ".join(components) + ". Для каждого до push назови существующий факт, который уже говорит то же самое: "
            "компонент, группу, состояние действия. Нашёлся — новый компонент не нужен: CoinsMoved дублировал Animating с FromSlotId/ToSlotId "
            "у принятого хода. Не нашёлся — назови владельца, кто ставит, кто читает и когда он умирает (OPTIONAL_ENTITAS.md, «Новые компоненты»).")
    if messages:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": " ".join(messages)}}, ensure_ascii=False))
    sys.exit(0)

try:
    stale = stale_pull_requests(cwd)
except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
    stale = None

if stale is None:
    context = ("Не удалось спросить GitHub об открытых PR. Сверь описание каждой PR, в которую сейчас ушли коммиты, "
               "с итоговым diff от base до head и перепиши его, если расходится.")
elif stale:
    blocks = []
    for pr, since, fresh in stale:
        commits = "\n".join(f"    {oid[:9]} {headline}" for oid, headline in reversed(fresh))
        body = "\n".join("    > " + line for line in (pr["body"] or "(пусто)").splitlines())
        blocks.append(f"- PR #{pr['number']} «{pr['title']}», описание от {since[:16].replace('T', ' ')} UTC. "
                      f"Коммиты после него:\n{commits}\n  Текущее описание:\n{body}")
    numbers = " ".join(str(pr["number"]) for pr, _, _ in stale)
    context = ("Описание PR старше коммитов. Сверь каждую PR ниже с итоговым diff от base до head: расходится — перепиши "
               "описание целиком, совпадает — отметь сверку из папки репозитория: "
               f"`{os.path.realpath(__file__)} --checked {numbers}` (только сверенные номера). "
               "Пока не сделано, напоминание повторяется после каждого push. В ответе владельцу по каждой PR одна строка: "
               "«Описание #N: сверено, без изменений» или «Описание #N: обновлено — что поменялось».\n\n"
               + "\n".join(blocks))
elif is_pr_create:
    context = "Проверь описание новой PR по правилам ниже."
else:
    sys.exit(0)

print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                         "additionalContext": context + "\n\nПравила GIT_WORKFLOW.md:\n" + workflow_rules()}},
                 ensure_ascii=False))
