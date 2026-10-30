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
        "source deploy/deploy.sh",
        ". deploy/deploy.sh",
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
        "deploy/install.sh 203.0.113.10",
        "./deploy/install.sh 203.0.113.10",
        "/opt/ai-trainer/deploy/install.sh 203.0.113.10",
        "bash deploy/install.sh 203.0.113.10",
        "sh -e deploy/install.sh 203.0.113.10",
        "sudo deploy/install.sh 203.0.113.10",
        "source deploy/install.sh 203.0.113.10",
        ". ./deploy/install.sh 203.0.113.10",
        "source /opt/ai-trainer/deploy/install.sh 203.0.113.10",
        "cd deploy && ./install.sh 203.0.113.10",
        "cd deploy && install.sh 203.0.113.10",
        "cd ./deploy && bash install.sh 203.0.113.10",
        "cd /opt/ai-trainer/deploy && ./install.sh 203.0.113.10",
        "pushd deploy && ./install.sh 203.0.113.10",
        "cd deploy && source ./install.sh 203.0.113.10",
    ],
)
def test_running_the_host_install_denied(command: str) -> None:
    """install.sh copies files onto the production host; agents never reach that host."""
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "cat deploy/deploy.sh",
        "bash -n deploy/deploy.sh",
        "bash -n deploy/install.sh",
        "shellcheck deploy/deploy.sh",
        "shellcheck deploy/install.sh",
        "/tmp/install.sh",
        "source /tmp/install.sh",
        "./scripts/install.sh",
        "cd other && ./install.sh",
        "cd deploy && cd .. && ./install.sh",
        "cd deploy && bash -n ./install.sh",
        "cd deploy && /tmp/install.sh",
        "uv run pytest tests/ops/test_deploy_script.py",
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


# Only a human merges into main (ADR-0001 invariant 9, #350).


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git push origin HEAD:main",
        "git -C . push origin main",
        "git push origin +main",
        "git push origin infra/1-x:main",
        "git push origin HEAD:refs/heads/main",
        "git push origin :main",
        "git push -d origin main",
        "git push --all",
        "git push --branches origin",
        "git push -u origin feature main",
    ],
)
def test_push_to_main_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "git push --force",
        "git push -f origin infra/1-x",
        "git push -uf origin infra/1-x",
        "git push --force-with-lease",
        "git push --force-with-lease=infra/1-x origin infra/1-x",
        "git push --force-if-includes --force-with-lease origin infra/1-x",
        "git push --forc origin infra/1-x",
        "git push origin +infra/1-x",
    ],
)
def test_force_push_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin infra/1-x",
        "git push origin infra/1-x:infra/1-x",
        "git push origin main:infra/1-x",
        "git push -o ci.skip origin infra/1-x",
        "git push origin maintenance/1-x",
        "git push origin docs/main-page",
    ],
)
def test_push_ticket_branch_allowed(command: str) -> None:
    assert not _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 1",
        "gh pr merge 1 --squash --auto",
        "gh -R stanislawK/ai-trainer pr merge 1",
        "gh api -X PUT repos/stanislawK/ai-trainer/pulls/1/merge",
        "gh api graphql -f query='mutation { mergePullRequest(input: {}) { clientMutationId } }'",
        "gh api graphql -f query='mutation { enablePullRequestAutoMerge(input: {}) { x } }'",
        "curl -X PUT https://api.github.com/repos/o/r/pulls/1/merge",
    ],
)
def test_gh_pr_merge_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 1",
        "gh pr create --base main --title t --body-file b.md",
        "gh api repos/o/r/pulls/1/merge",
    ],
)
def test_pr_reads_allowed(command: str) -> None:
    """A GET on the merge endpoint only asks whether the PR was merged."""
    assert not _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo ok && git push origin main",
        "git status; git push --force",
        "bash -c 'git push origin HEAD:main'",
        "sh -c 'gh pr merge 1'",
        "echo $(gh pr merge 1)",
        "git checkout main && git commit -m x",
        "git switch main && git merge infra/1-x",
        "git checkout -B main && git commit -m x",
    ],
)
def test_main_guard_chained_or_nested_denied(command: str) -> None:
    assert _denied(command)


def test_switching_away_from_main_before_committing_allowed() -> None:
    assert not _denied("git switch -c infra/1-x && git commit -m x")


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git push --force",
        "gh pr merge 1",
        "git checkout main && git commit -m x",
    ],
)
def test_main_denials_cite_adr_0001_invariant_9(command: str) -> None:
    reason = guard.denial_reason(command)
    assert reason is not None
    assert "ADR-0001 invariant 9" in reason


def _git(*args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository on main with one commit."""
    path = tmp_path / "repo"
    _git("init", "-q", "-b", "main", str(path))
    _git("-C", str(path), "commit", "-q", "--allow-empty", "-m", "init")
    return path


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        "git commit --amend --no-edit",
        "git -C . commit -m x",
        "git merge infra/1-x",
        "git cherry-pick abc123",
        "git revert HEAD",
        "git am patch.mbox",
        "git push",
        "git push origin",
        "git push -u origin HEAD",
        "git push origin @",
    ],
)
def test_writing_main_denied_when_cwd_is_on_main(repo: Path, command: str) -> None:
    assert guard.denial_reason(command, cwd=str(repo)) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        "git push",
        "git push -u origin HEAD",
        "git merge main",
    ],
)
def test_writing_a_ticket_branch_allowed(repo: Path, command: str) -> None:
    _git("-C", str(repo), "switch", "-q", "-c", "infra/1-x")
    assert guard.denial_reason(command, cwd=str(repo)) is None


@pytest.mark.parametrize(
    "command",
    ["git pull", "git status", "git merge --abort", "git log --oneline", "git switch -c infra/1-x"],
)
def test_reading_or_updating_main_allowed(repo: Path, command: str) -> None:
    assert guard.denial_reason(command, cwd=str(repo)) is None


def test_branch_is_read_from_a_worktree_cwd(repo: Path, tmp_path: Path) -> None:
    """The main checkout is on a ticket branch; the worktree holds main."""
    _git("-C", str(repo), "switch", "-q", "-c", "infra/1-x")
    worktree = tmp_path / "wt"
    _git("-C", str(repo), "worktree", "add", "-q", str(worktree), "main")
    assert guard.denial_reason("git commit -m x", cwd=str(worktree)) is not None
    assert guard.denial_reason("git commit -m x", cwd=str(repo)) is None
    assert guard.denial_reason(f"git -C {worktree} commit -m x", cwd=str(repo)) is not None


def test_cwd_outside_a_repository_allows_commit(tmp_path: Path) -> None:
    assert guard.denial_reason("git commit -m x", cwd=str(tmp_path)) is None


def test_detached_head_allows_commit(repo: Path) -> None:
    _git("-C", str(repo), "switch", "-q", "--detach")
    assert guard.denial_reason("git commit -m x", cwd=str(repo)) is None


def test_hook_denies_commit_on_main_from_its_cwd(repo: Path) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(repo)}
    output = json.loads(_run(json.dumps(payload)).stdout)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "ADR-0001 invariant 9" in output["permissionDecisionReason"]


def test_hook_allows_commit_on_a_ticket_branch(repo: Path) -> None:
    _git("-C", str(repo), "switch", "-q", "-c", "infra/1-x")
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(repo)}
    assert _run(json.dumps(payload)).stdout == ""


def test_hook_ignores_a_cwd_that_is_not_a_string() -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": 7}
    assert _run(json.dumps(payload)).stdout == ""


def test_settings_deny_main_pushes_force_and_merge() -> None:
    """The deny rules stop the same calls before the hook runs (#350)."""
    deny = set(json.loads((HOOK.parents[1] / "settings.json").read_text())["permissions"]["deny"])
    assert {
        "Bash(git push main)",
        "Bash(git push * main)",
        "Bash(git push * main *)",
        "Bash(git push *:main)",
        "Bash(git push *:main *)",
        "Bash(git push *:refs/heads/main)",
        "Bash(git push -f*)",
        "Bash(git push * -f*)",
        "Bash(git push --force*)",
        "Bash(git push * --force*)",
        "Bash(git push * +*)",
        "Bash(gh pr merge *)",
    } <= deny


@pytest.mark.parametrize(
    "command",
    [
        "gh api -XPUT repos/o/r/pulls/1/merge",
        "gh api --method=PUT repos/o/r/pulls/1/merge",
        "gh api repos/o/r/pulls/1/merge -f merge_method=squash",
    ],
)
def test_gh_api_merge_written_any_way_denied(command: str) -> None:
    assert _denied(command)


def test_gh_api_merge_status_with_explicit_get_allowed() -> None:
    assert not _denied("gh api -X GET repos/o/r/pulls/1/merge")


@pytest.mark.parametrize(
    "command",
    [
        "git --version && git commit -m x",
        "git switch -c && git commit -m x",
        "git switch -q && git commit -m x",
        "git checkout main -- README.md && git commit -m x",
        "git switch --detach main && git commit -m x",
        "git checkout feature && git commit -m x",
    ],
)
def test_unclear_switches_leave_the_branch_unknown(command: str) -> None:
    """Without a cwd and no clear switch to main, the branch is unknown and commit passes."""
    assert not _denied(command)


@pytest.mark.parametrize(
    "error", [OSError("no git"), subprocess.TimeoutExpired(cmd="git", timeout=5)]
)
def test_branch_lookup_failure_allows_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(guard.subprocess, "run", fail)
    guard._checked_out_branch.cache_clear()
    assert guard.denial_reason("git commit -m x", cwd=str(tmp_path)) is None
    guard._checked_out_branch.cache_clear()


# Gaps the skeptics found at verify (#350).


@pytest.mark.parametrize(
    "command",
    [
        "gh pr -R stanislawK/ai-trainer merge 1",
        "gh pr --repo=stanislawK/ai-trainer merge 1",
        "gh api graphql --field=query='mutation { mergePullRequest(input: {}) { x } }'",
        "gh api graphql -fquery='mutation { mergePullRequest(input: {}) { x } }'",
        "gh api -X POST repos/o/r/merges -f base=main -f head=infra/1-x",
        'curl https://api.github.com/graphql -d \'{"query":"mutation{mergePullRequest}"}\'',
    ],
)
def test_other_ways_to_merge_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "bash -c -- 'git push origin main'",
        "true && sh -c -- 'gh pr merge 5'",
        "bash -c $'git push origin main'",
        "function f { git push origin main; }; f",
    ],
)
def test_other_shell_forms_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "git push --repo=origin main",
        "git push --repo origin HEAD:main",
        "git push origin HEAD:main>out.log",
        "git push origin main</dev/null",
        "git push origin HEAD:heads/main",
        "git push --exec /usr/bin/git-receive-pack origin main",
        "git send-pack --force origin infra/1-x",
        "git send-pack origin main",
        "git -c remote.origin.push=+HEAD:refs/heads/infra/1-x push origin",
        "git -c remote.origin.push=HEAD:main push origin",
    ],
)
def test_other_ways_to_push_main_or_force_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'deny `git push origin main` and `gh pr merge`'",
        "gh issue comment 1 --body 'never `git push --force`'",
        "git -c remote.origin.pushurl=x push origin infra/1-x",
    ],
)
def test_backticks_inside_single_quotes_allowed(command: str) -> None:
    """Single quotes keep backticks literal; the shell never runs them."""
    assert not _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "git push origin > log",
        "git push origin 2>/dev/null",
        "git --exec-path=/usr/lib/git push",
    ],
)
def test_push_of_main_with_redirects_denied(repo: Path, command: str) -> None:
    assert guard.denial_reason(command, cwd=str(repo)) is not None


def test_cd_into_a_main_checkout_before_committing_denied(repo: Path, tmp_path: Path) -> None:
    ticket = tmp_path / "ticket"
    _git("-C", str(repo), "worktree", "add", "-q", "-b", "infra/1-x", str(ticket))
    assert guard.denial_reason(f"cd {repo} && git commit -m x", cwd=str(ticket)) is not None
    assert guard.denial_reason(f"cd {ticket} && git commit -m x", cwd=str(repo)) is None
    assert guard.denial_reason("cd ../repo && git commit -m x", cwd=str(ticket)) is not None


def test_git_dir_pointing_at_main_denied(repo: Path, tmp_path: Path) -> None:
    ticket = tmp_path / "ticket"
    _git("-C", str(repo), "worktree", "add", "-q", "-b", "infra/1-x", str(ticket))
    git_dir = repo / ".git"
    for command in (
        f"git --git-dir={git_dir} --work-tree={repo} commit -m x",
        f"git --git-dir {git_dir} commit -m x",
        f"GIT_DIR={git_dir} git commit -m x",
        f"env GIT_DIR={git_dir} git commit -m x",
    ):
        assert guard.denial_reason(command, cwd=str(ticket)) is not None, command


def test_inherited_git_dir_is_ignored(
    monkeypatch: pytest.MonkeyPatch, repo: Path, tmp_path: Path
) -> None:
    """The hook reads the branch of `cwd`, not of a GIT_DIR Claude Code was started with."""
    ticket = tmp_path / "ticket"
    _git("-C", str(repo), "worktree", "add", "-q", "-b", "infra/1-x", str(ticket))
    monkeypatch.setenv("GIT_DIR", str(ticket / ".git"))
    guard._checked_out_branch.cache_clear()
    assert guard.denial_reason("git commit -m x", cwd=str(repo)) is not None
    guard._checked_out_branch.cache_clear()


def test_bare_assignments_allowed() -> None:
    assert not _denied("FOO=1 BAR=2; git status")
