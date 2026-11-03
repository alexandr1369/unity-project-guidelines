#!/usr/bin/env python3
"""Регрессии командных хуков: только временные Git-репозитории и поддельная сборка."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parent
FILE = "Assets/Code/Tests/EditMode/VictoryDialogue.cs"
VALID = "public class VictoryDialogue : DialogueBase\n{\n    protected override void Initialize() { }\n}\n"
INVALID = VALID.replace("protected override void Initialize()", "private void OnEnable()")


class HookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hook-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo with spaces"
        self.other = self.root / "other"
        for repo in (self.repo, self.other):
            repo.mkdir()
            self.git("init", "-q", repo=repo)
            file = repo / FILE
            file.parent.mkdir(parents=True)
            file.write_text(VALID)
            self.git("add", "--", FILE, repo=repo)
            # Фикстура не зависит от пользовательских Git-хуков и подписи коммитов.
            self.git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                     "-c", "user.name=Hook test", "-c", "user.email=hook@example.invalid",
                     "commit", "-qm", "Добавил фикстуру", repo=repo)

    def git(self, *args, repo=None):
        return subprocess.run(["git", *args], cwd=repo or self.repo, check=True,
                              capture_output=True, text=True, timeout=10).stdout

    def stage(self, source):
        (self.repo / FILE).write_text(source)
        self.git("add", "--", FILE)

    def hook(self, command, *, script="commit-guard.py", key="cmd", cwd=None, workdir=None, env=None, prompt=None, tool_response=None):
        tool_input = {key: command}
        if workdir is not None:
            tool_input["workdir"] = str(workdir)
        event = {"cwd": str(cwd or self.repo), "tool_name": "Bash", "tool_input": tool_input}
        if prompt is not None:
            event["prompt"] = prompt
        if tool_response is not None:
            event["tool_response"] = tool_response
        runner = "bash" if script.endswith(".sh") else sys.executable
        result = subprocess.run([runner, str(SCRIPTS / script)], input=json.dumps(event),
                                cwd=self.root, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def denied(self, output, reason):
        decision = json.loads(output)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn(reason, decision["permissionDecisionReason"])

    def commit_allowed(self, output):
        context = json.loads(output)["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", context)
        self.assertIn("Самопроверка перед отчётом", context["additionalContext"])

    def test_existing_code_check_regressions(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / "code-checks.py"), "--self-test"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("dialogue-lifecycle self-test ok", result.stdout)
        self.assertIn("duplicate-save-stage self-test ok", result.stdout)

    def test_unsafe_git_commands_are_denied(self):
        commands = ["git add -A", "git add --all --dry-run", 'git commit -am "Исправил окно"',
                    'git commit --all -m "Исправил окно"',
                    'git add -- file.cs && git commit -m "Исправил окно"',
                    'git add -- file.cs\ngit commit -m "Исправил окно"',
                    'git commit -m "Закоммитил исправление"']
        for command in commands:
            with self.subTest(command=command):
                self.denied(self.hook(command), "правил")

    def test_review_feedback_injects_rule_update_instructions(self):
        prompts = ["Два момента оставил в 171 проверь в мр", "Опять ошибка, порядок методов нарушен",
                   "Дополни правила по замечаниям, сам себя обучай", "Left two comments on PR 171"]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                output = json.loads(self.hook("", script="prompt-reminders.py", prompt=prompt))["hookSpecificOutput"]
                self.assertEqual(output["hookEventName"], "UserPromptSubmit")
                self.assertIn("## Выводы из замечаний владельца", output["additionalContext"])
                self.assertIn("без отдельной просьбы", output["additionalContext"])
                self.assertIn("Проверка нового правила", output["additionalContext"])

    def test_unrelated_prompt_does_not_inject_rule_update_instructions(self):
        self.assertEqual(self.hook("", script="prompt-reminders.py", prompt="Покажи текущий баланс гемов"), "")

    def test_successful_commit_without_new_names_injects_self_check(self):
        self.stage(VALID.replace("Initialize() { }", "Initialize() { base.Initialize(); }"))
        self.git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "-c", "user.name=Hook test", "-c", "user.email=hook@example.invalid",
                 "commit", "-qm", "Уточнил фикстуру")
        responses = [{"exit_code": 0, "output": "commit created"},
                     json.dumps({"exit_code": 0, "output": "commit created"}),
                     "Wall time: 0.1 seconds\nProcess exited with code 0\nFinal output:\ncommit created",
                     {"stdout": "commit created", "stderr": "", "interrupted": False, "isImage": False}]
        for response in responses:
            with self.subTest(response=response):
                output = json.loads(self.hook('git commit -m "Исправил окно"', script="pr-description-reminder.py",
                                             cwd=self.other, workdir=self.repo, tool_response=response))["hookSpecificOutput"]
                self.assertEqual(output["hookEventName"], "PostToolUse")
                self.assertIn("Самопроверка перед отчётом", output["additionalContext"])
                self.assertIn("Выводы из замечаний владельца", output["additionalContext"])
                self.assertNotIn("Новые имена", output["additionalContext"])

    def test_failed_commit_result_does_not_claim_success(self):
        responses = [{"exit_code": 1, "output": "nothing to commit"},
                     "Process exited with code 1\nFinal output:\nnothing to commit",
                     {"stdout": "", "stderr": "interrupted", "interrupted": True, "isImage": False}]
        for response in responses:
            with self.subTest(response=response):
                self.assertEqual(self.hook('git commit -m "Исправил окно"', script="pr-description-reminder.py",
                                           tool_response=response), "")

    def test_unknown_commit_result_requests_confirmation_and_self_check(self):
        for response in (None, {}, {"exit_code": None, "session_id": 1}, {"unknown_wrapper": "result"}):
            with self.subTest(response=response):
                output = json.loads(self.hook('git commit -m "Исправил окно"', script="pr-description-reminder.py",
                                             tool_response=response))["hookSpecificOutput"]["additionalContext"]
                self.assertIn("После команды коммита:", output)
                self.assertIn("дождись завершения", output)
                self.assertIn("Самопроверка перед отчётом", output)
                self.assertNotIn("успешного коммита", output)
                self.assertNotIn("Новые имена", output)
                self.assertNotIn("Новые компоненты", output)

    def test_commit_self_check_keeps_existing_name_and_component_reminders(self):
        file = self.repo / "Assets/Code/FlightComponent.cs"
        file.write_text("public class FlightComponent : IComponent { }\n")
        self.git("add", "--", str(file))
        self.git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "-c", "user.name=Hook test", "-c", "user.email=hook@example.invalid",
                 "commit", "-qm", "Добавил фикстуру компонента")
        command = f'git -C {shlex.quote(str(self.repo))} commit -m "Исправил окно"'
        output = json.loads(self.hook(command, script="pr-description-reminder.py", cwd=self.other,
                                     tool_response={"exit_code": 0}))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Самопроверка перед отчётом", output)
        self.assertIn("Новые имена в коммите: FlightComponent", output)
        self.assertIn("Новые компоненты: FlightComponent", output)

    def test_unrelated_post_tool_command_does_not_inject_commit_self_check(self):
        self.assertEqual(self.hook("git status --short", script="pr-description-reminder.py",
                                  tool_response={"exit_code": 0}), "")

    def test_failed_publish_or_merge_does_not_claim_success(self):
        for command in ("git push origin main", "gh pr create", "gh pr merge 171"):
            with self.subTest(command=command):
                self.assertEqual(self.hook(command, script="pr-description-reminder.py",
                                           tool_response={"exit_code": 1}), "")

    def test_commit_and_push_require_separate_calls(self):
        for prefix in ("git ", f"git -C {shlex.quote(str(self.repo))} "):
            with self.subTest(prefix=prefix):
                self.denied(self.hook(prefix + 'commit -m "Исправил окно" && git push origin main'), "самопроверку")
        self.commit_allowed(self.hook('git commit -m "Уточнил текст git push"'))

    def test_read_commands_and_explicit_staging_are_allowed(self):
        for command in ["git status --short", "git add -- file.cs", 'printf "git add -A"']:
            with self.subTest(command=command):
                self.assertEqual(self.hook(command), "")

    def test_unsafe_git_commands_with_quoted_directory_are_denied(self):
        prefix = f"git -C {shlex.quote(str(self.repo))} "
        commands = [("add --all", "индексирует"),
                    ('commit -am "Исправил окно"', "правки владельца"),
                    ('commit -m "Закоммитил исправление"', "git-действия")]
        for command, reason in commands:
            with self.subTest(command=command):
                self.denied(self.hook(prefix + command, cwd=self.other), reason)

    def test_clean_lifecycle_is_allowed_in_both_payload_formats(self):
        self.stage(VALID.replace("Initialize", "CleanUp"))
        for key in ("cmd", "command"):
            with self.subTest(key=key):
                self.commit_allowed(self.hook('git commit -m "Исправил окно"', key=key))

    def test_own_method_before_base_lifecycle_is_denied_until_reordered(self):
        public_method = "    public void DisplayCompletedLevel() { }\n"
        self.stage(VALID.replace("    protected override", public_method + "    protected override"))
        self.denied(self.hook('git commit -m "Исправил окно"'), "блок lifecycle")
        self.stage(VALID.replace("\n}\n", "\n" + public_method + "}\n"))
        self.commit_allowed(self.hook('git commit -m "Исправил окно"'))

    def test_staged_violation_is_denied_in_both_payload_formats(self):
        self.stage(INVALID)
        for key in ("cmd", "command"):
            for prefix in ("", "pwd\n"):
                with self.subTest(key=key, prefix=prefix):
                    self.denied(self.hook(prefix + 'git commit -m "Исправил окно"', key=key), "OnEnable")

    def test_workdir_selects_the_actual_repository(self):
        self.stage(INVALID)
        self.denied(self.hook('git commit -m "Исправил окно"', cwd=self.other, workdir=self.repo), "OnEnable")
        self.commit_allowed(self.hook('git commit -m "Исправил окно"', cwd=self.repo, workdir=self.other))

    def test_quoted_git_directory_and_cd_select_the_actual_repository(self):
        self.stage(INVALID)
        for prefix in (f"git -C {shlex.quote(str(self.repo))} ", f"cd {shlex.quote(str(self.repo))} && git "):
            with self.subTest(prefix=prefix):
                self.denied(self.hook(prefix + 'commit -m "Исправил окно"', cwd=self.other), "OnEnable")

    def test_variable_with_quoted_directory_selects_the_actual_repository(self):
        self.stage(INVALID)
        assignment = f"TASK_REPO={shlex.quote(str(self.repo))}\n"
        for prefix in ('git -C "$TASK_REPO" ', 'cd "${TASK_REPO}" && git '):
            with self.subTest(prefix=prefix):
                command = assignment + prefix + 'commit -m "Исправил окно"'
                self.denied(self.hook(command, cwd=self.other), "OnEnable")

    def test_check_reads_index_not_fixed_working_copy(self):
        self.stage(INVALID)
        (self.repo / FILE).write_text(VALID)
        self.denied(self.hook('git commit -m "Исправил окно"'), "OnEnable")
        self.stage(VALID)
        self.commit_allowed(self.hook('git commit -m "Исправил окно"'))

    def test_non_lifecycle_style_violation_is_also_denied(self):
        self.stage(VALID.replace("    protected override void Initialize() { }",
                                "    private int Count()\n    {\n        int count = 1;\n\n        return count;\n    }"))
        self.denied(self.hook('git commit -m "Исправил окно"'), "return")

    def build_environment(self, body, project=True):
        if project:
            file = self.repo / "src/game/Code.csproj"
            file.parent.mkdir(parents=True)
            file.touch()
        binaries = self.root / "bin"
        binaries.mkdir()
        dotnet = binaries / "dotnet"
        dotnet.write_text("#!/bin/sh\n" + body + "\n")
        dotnet.chmod(0o755)
        return os.environ | {"PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                             "CLAUDE_PROJECT_DIR": str(self.repo)}

    def test_successful_build_returns_empty_output(self):
        env = self.build_environment("exit 0")
        self.assertEqual(self.hook("git push origin main", script="verify-builds.sh", env=env), "")

    def test_build_error_returns_valid_deny_json(self):
        env = self.build_environment("printf '%s\\n' 'error CS0001: fixture failure'\nexit 1")
        self.denied(self.hook("git push origin main", script="verify-builds.sh", env=env), "CS0001")

    def test_build_uses_codex_directory_without_claude_environment(self):
        env = self.build_environment("printf '%s\\n' 'error CS0001: fixture failure'\nexit 1")
        env.pop("CLAUDE_PROJECT_DIR", None)
        for cwd, workdir in ((self.other, self.repo), (self.repo, None)):
            with self.subTest(cwd=cwd, workdir=workdir):
                output = self.hook("git push origin main", script="verify-builds.sh", env=env,
                                   cwd=cwd, workdir=workdir)
                self.denied(output, "CS0001")

    def test_build_directory_flags_and_cd_select_the_actual_repository(self):
        env = self.build_environment("printf '%s\\n' 'error CS0001: fixture failure'\nexit 1")
        env.pop("CLAUDE_PROJECT_DIR", None)
        for prefix in (f"git -C {shlex.quote(str(self.repo))} ", f"cd {shlex.quote(str(self.repo))} && git "):
            with self.subTest(prefix=prefix):
                output = self.hook(prefix + "push origin main", script="verify-builds.sh", env=env, cwd=self.other)
                self.denied(output, "CS0001")

    def test_git_directory_status_does_not_build(self):
        marker = self.root / "dotnet-called"
        env = self.build_environment(f"touch {shlex.quote(str(marker))}\nexit 1")
        command = f"git -C {shlex.quote(str(self.repo))} status --short"
        self.assertEqual(self.hook(command, script="verify-builds.sh", env=env, cwd=self.other), "")
        self.assertFalse(marker.exists())

    def test_build_diagnostic_with_backslashes_and_quotes_is_valid_json(self):
        env = self.build_environment("printf '%s\\n' 'error CS0001: C:\\src\\file.cs: unexpected \"token\"'\nexit 1")
        self.denied(self.hook("git push origin main", script="verify-builds.sh", env=env), "CS0001")

    def test_no_projects_and_unrelated_commands_do_not_build(self):
        marker = self.root / "dotnet-called"
        env = self.build_environment(f"touch {shlex.quote(str(marker))}\nexit 1", project=False)
        for command in ("git push origin main", "git status --short"):
            with self.subTest(command=command):
                self.assertEqual(self.hook(command, script="verify-builds.sh", env=env), "")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
