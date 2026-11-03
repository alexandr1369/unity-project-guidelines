"""Рабочая папка прямых Git-команд в хуках коммита и сборки."""
import json
import os
import re
import shlex
import sys


START = r"(?:^|[;&|(]\s*)"
ARGUMENT = r'''(?:"[^"]*"|'[^']*'|\S+)'''
GIT = r"git(?:\s+-[cC]\s+" + ARGUMENT + r")*"


def command_directory(command, cwd):
    # shortcut: прямые cd/git -C без выполнения shell; для новых форм команд сначала добавить регрессию.
    variables = {"HOME": os.path.expanduser("~")}
    directory = cwd
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    tokens = list(lexer)
    expand = lambda text: os.path.expanduser(re.sub(r"\$\{?(\w+)\}?", lambda m: variables.get(m.group(1), m.group(0)), text))
    for index, token in enumerate(tokens):
        assignment = re.fullmatch(r"(\w+)=(.*)", token)
        if assignment:
            variables[assignment.group(1)] = expand(assignment.group(2))
        elif token == "cd" and index + 1 < len(tokens):
            path = expand(tokens[index + 1])
            directory = path if os.path.isabs(path) else os.path.join(directory, path)
        elif token == "-C" and index > 0 and tokens[index - 1] == "git" and index + 1 < len(tokens):
            path = expand(tokens[index + 1])
            directory = path if os.path.isabs(path) else os.path.join(directory, path)
    return directory


if __name__ == "__main__":
    data = json.load(sys.stdin)
    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command") or tool_input.get("cmd") or ""
    if isinstance(command, list):
        command = " ".join(command)
    push = re.search(START + GIT + r"\s+push(?![-\w])", command, re.M)
    if push:
        cwd = tool_input.get("workdir") or data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or "."
        print(command_directory(command[:push.end()], cwd))
