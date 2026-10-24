"""The PreToolUse hook that keeps agents from releasing or reaching production (ADR-0023)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "guard_release.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard_release", HOOK)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load()


def _denied(command: str) -> bool:
    return guard.denial_reason(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git tag v0.1.0",
        "git tag -a v0.1.0 -m release",
        "git tag -d v0.1.0",
        "git -C /tmp/repo tag v0.1.0",
        "git --no-pager tag",
    ],
)
def test_git_tag_denied(command: str) -> None:
    assert _denied(command)


def test_git_tag_reason_names_adr_0023() -> None:
    reason = guard.denial_reason("git tag v0.1.0")
    assert reason is not None
    assert "ADR-0023" in reason


@pytest.mark.parametrize(
    "command",
    [
        "git push origin v0.1.0",
        "git push --tags",
        "git push origin --tags",
        "git push --follow-tags",
        "git push --mirror",
        "git push origin refs/tags/v0.1.0",
        "git push origin :v0.1.0",
        "git push origin HEAD:refs/tags/v0.1.0",
        "git push -f origin v1.2.3",
    ],
)
def test_push_tag_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "git push origin infra/214-stop-agents-tagging-deploying",
        "git push -u origin infra/214-slug",
        "git push",
        "git push origin HEAD",
        "git push origin viewer-fix",
        "git push origin feature/v2-redesign",
    ],
)
def test_push_branch_allowed(command: str) -> None:
    assert not _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/stanislawK/ai-trainer/deployments",
        "gh api repos/stanislawK/ai-trainer/rulesets",
        "gh api repos/stanislawK/ai-trainer/rulesets/123 -X PUT",
        "gh api repos/stanislawK/ai-trainer/environments/production",
        "gh api repos/stanislawK/ai-trainer/actions/runs/1/pending_deployments -f state=approved",
        "gh api -X POST repos/stanislawK/ai-trainer/deployments/9/statuses",
        "gh api /repos/stanislawK/ai-trainer/branches/main/protection",
    ],
)
def test_gh_api_guarded_paths_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "gh issue list",
        "gh issue view 214 --json state",
        "gh api repos/stanislawK/ai-trainer/issues",
        "gh pr create --base main --title t --body-file b.md",
    ],
)
def test_gh_read_and_ticket_commands_allowed(command: str) -> None:
    assert not _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "gh release create v0.1.0",
        "gh release list",
        "gh workflow run release.yml",
        "gh workflow run release.yml --ref v0.1.0",
        "gh workflow run",
    ],
)
def test_release_commands_denied(command: str) -> None:
    assert _denied(command)


def test_other_workflows_may_be_viewed() -> None:
    assert not _denied("gh workflow list")
    assert not _denied("gh run list")


@pytest.mark.parametrize(
    "command",
    [
        "deploy/deploy.sh",
        "./deploy/deploy.sh",
        "/opt/ai-trainer/deploy/deploy.sh",
        "sudo -u deploy /opt/ai-trainer/deploy/deploy.sh",
        "bash deploy/deploy.sh",
        "sh -e deploy/deploy.sh",
        "CREDENTIALS_DIRECTORY=/tmp/c deploy/deploy.sh",
        "systemctl start ai-trainer-deploy.service",
        "sudo systemctl restart ai-trainer-deploy",
        "systemctl --user start ai-trainer-deploy.timer",
    ],
)
def test_running_the_deploy_poller_denied(command: str) -> None:
    """The poller deploys whatever the owner approved (#225); only its timer on the host runs it."""
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "cat deploy/deploy.sh",
        "bash -n deploy/deploy.sh",
        "shellcheck deploy/deploy.sh",
        "uv run pytest tests/unit/test_deploy_script.py",
        "systemctl status ai-trainer-deploy.timer",
        "sudo systemctl restart docker",
        "git add deploy/deploy.sh deploy/systemd/ai-trainer-deploy.service",
    ],
)
def test_reading_or_checking_the_deploy_poller_allowed(command: str) -> None:
    assert not _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "ssh deploy@host",
        "ssh -i key deploy@host 'ls'",
        "scp f host:",
        "scp -r ./deploy deploy@host:/srv",
        "sftp deploy@host",
        "rsync -e ssh -a ./ deploy@host:/srv",
        "/usr/bin/ssh deploy@host",
    ],
)
def test_ssh_scp_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize("operator", ["&&", "||", ";", "|", "&", "\n"])
def test_denied_command_chained_after_operator_is_denied(operator: str) -> None:
    assert _denied(f"echo ok {operator} git tag v0.1.0")
    assert _denied(f"git status {operator} ssh deploy@host")


def test_chain_with_only_allowed_commands_is_allowed() -> None:
    assert not _denied("git status && git push origin infra/214-x | cat; gh issue list")


@pytest.mark.parametrize(
    "command",
    [
        "echo $(git tag v0.1.0)",
        "echo `git tag v0.1.0`",
        "bash -c 'git tag v0.1.0'",
        'sh -c "echo hi && ssh deploy@host"',
        "env GIT_AUTHOR_NAME=x git tag v0.1.0",
        "FOO=1 git tag v0.1.0",
        "sudo ssh deploy@host",
        "cd repo && (git tag v0.1.0)",
        "{ git tag v0.1.0; }",
        "if true; then git tag v0.1.0; fi",
        "xargs git tag",
        "command git tag v0.1.0",
    ],
)
def test_wrapped_commands_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "2>&1 git tag v1",
        ">/dev/null git tag v1",
        "2>/dev/null ssh h",
        "</dev/null ssh h",
        "> out git tag v1",
        "echo 'git tag v1' | bash",
        "echo ssh h | sh",
        "printf 'git tag v1' | sh -s",
        "bash <<< 'git tag v1'",
        "echo git tag v1 | xargs -I{} sh -c '{}'",
    ],
)
def test_redirections_and_piped_shells_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "setsid ssh h",
        "flock x ssh h",
        "sshpass -p x ssh h",
        "uv run ssh h",
        "find . -exec ssh h \\;",
        "git rebase --exec 'git tag v1' main",
        'bash -c"ssh h"',
        'echo "$(git tag v1)"',
        "git --attr-source HEAD tag v1",
        "echo refs/tags/v1 | git update-ref --stdin",
        "git mktag",
        "gh api -X POST repos/o/r/releases -f tag_name=v1",
        "gh api repos/o/r/git/refs -f ref=refs/tags/v1",
        "gh api repos/o/r/deployments?per_page=1",
        "gh workflow run -f x=1 release.yml",
        "gh workflow run --ref main release.yml",
        "git push origin tags/v1.0.0",
        "git push origin $TAG",
        'git push origin "v*"',
    ],
)
def test_other_ways_to_release_or_reach_the_host_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "rsync -av --exclude=ssh src/ dest/",
        "rsync -av sshfiles/ dest/",
        "git push origin v2-redesign",
        "git push origin viewer-fix",
        "gh api repos/o/r/issues -f body='fix deployment docs'",
        "gh api repos/o/r/pulls/3 --jq .environment",
        "gh workflow run evals.yml",
    ],
)
def test_lookalike_names_and_field_values_allowed(command: str) -> None:
    assert not _denied(command)


def test_shell_with_a_script_file_or_version_flag_allowed() -> None:
    assert not _denied("bash --version")
    assert not _denied("make start 2>&1 | tee log")
    assert not _denied("uv run pytest > out.txt 2>&1")


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'never run git tag or ssh here'",
        "echo 'ssh deploy@host'",
        "gh issue create --title 'ssh and git tag' --body-file b.md",
        "grep -rn 'gh release' docs",
        "git log --oneline",
        "",
    ],
)
def test_mentions_inside_quoted_arguments_allowed(command: str) -> None:
    assert not _denied(command)


def test_unbalanced_quote_still_denies_a_guarded_command() -> None:
    assert _denied("git tag 'v0.1.0")


def _run(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )


def test_hook_prints_a_deny_decision_for_bash() -> None:
    result = _run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git tag v0.0.0"}}))
    assert result.returncode == 0
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert "ADR-0023" in output["permissionDecisionReason"]


def test_hook_is_silent_for_an_allowed_command() -> None:
    result = _run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh issue list"}}))
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json",
        "[]",
        json.dumps({"tool_name": "Read", "tool_input": {"file_path": "git tag"}}),
        json.dumps({"tool_name": "Bash", "tool_input": {}}),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": 7}}),
        json.dumps({"tool_name": "Bash"}),
    ],
)
def test_hook_allows_input_that_is_not_a_bash_command(payload: str) -> None:
    result = _run(payload)
    assert result.returncode == 0
    assert result.stdout == ""


def test_settings_register_the_hook_on_every_bash_call() -> None:
    settings = json.loads((HOOK.parents[1] / "settings.json").read_text())
    entries = settings["hooks"]["PreToolUse"]
    bash = [e for e in entries if e["matcher"] == "Bash"]
    commands = [h["command"] for e in bash for h in e["hooks"]]
    assert any("guard_release.py" in c for c in commands)
