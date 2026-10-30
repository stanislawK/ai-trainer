"""PreToolUse hook: agents never release, reach production or write main (ADR-0023, ADR-0024,
ADR-0001 invariant 9).

Reads the hook JSON on stdin. For a Bash call whose command would tag, push a tag, create a
release, run the release workflow, touch GitHub deployments, environments or rulesets, run the
deploy poller, run deploy/install.sh, or open an ssh/scp connection, it prints a `deny`
decision and exits 0. It does the same for a push whose refspec targets main, a force push, a
pull request merge, and a commit, merge, cherry-pick, revert or am while the branch checked out
where the command runs is main: the hook input's `cwd`, moved by any `cd`, `git -C`,
`--git-dir` or `GIT_DIR=` in the command. Everything else passes silently.

Standard library only, and kept compatible with the system `python3`, so the hook runs without the
project environment. The command is split on shell operators, `$( )`, backticks and `bash -c`, so
a denied command chained after `&&`, `;` or `|` is still found. This is a guard against honest
mistakes by an agent holding the owner's credentials, not a sandbox: obfuscation such as
`eval "$(echo … | base64 -d)"`, a git alias, a refspec fed through xargs or set with `git config`,
or a graphql query read from a file can get past it. A heredoc body is read as commands, so a
heredoc that only writes text naming a denied command is denied too; write such files with the
editor tools instead.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from functools import lru_cache
from typing import NamedTuple

ADR_0023 = "ADR-0023 (agents never release)"
ADR_0024 = "ADR-0024 invariant 5 (no agent reaches the production host)"
ADR_0001 = "ADR-0001 invariant 9 (only a human merges into main)"
_MAIN = "main"
_DEPLOY_DENIED = (
    "Running the deploy poller is blocked: only its timer on the host deploys a release the "
    f"owner approved, {ADR_0023}."
)
_HOST_INSTALL = "install.sh"
_INSTALL_DENIED = (
    f"Running deploy/install.sh is blocked: it copies files onto the production host, {ADR_0024}."
)


def _is_host_install(path: str) -> bool:
    """True for deploy/install.sh, not for any other file named install.sh."""
    parent, name = os.path.split(path.replace("\\", "/").rstrip("/"))
    return name == _HOST_INSTALL and os.path.basename(parent) == "deploy"


def _deny_invoked_script(path: str) -> str | None:
    if os.path.basename(path) == _DEPLOY_POLLER:
        return _DEPLOY_DENIED
    if _is_host_install(path):
        return _INSTALL_DENIED
    return None


# Characters that end a command or start a nested one. Newline is an operator, not whitespace.
_OPERATORS = ";&|()`\n"
_RESERVED = frozenset(
    {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "{", "}"}
)
_WRAPPERS = frozenset(
    {
        "env",
        "sudo",
        "doas",
        "command",
        "xargs",
        "nohup",
        "exec",
        "time",
        "nice",
        "timeout",
        "stdbuf",
        "builtin",
        "setsid",
        "watch",
        "flock",
        "ionice",
        "caffeinate",
        "sshpass",
        "chroot",
        "busybox",
        "screen",
        "parallel",
        "coproc",
        "uv",
    }
)
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_REMOTE_SHELL_TOOLS = frozenset({"ssh", "scp", "sftp"})
_FETCHERS = frozenset({"curl", "wget", "http", "https"})
_DEPLOY_POLLER = "deploy.sh"
_DEPLOY_UNIT = "ai-trainer-deploy"
_SYSTEMCTL_STARTS = frozenset({"start", "restart", "try-restart", "reload-or-restart"})

_GIT_OPTIONS_WITH_VALUE = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--config-env",
        "--attr-source",
    }
)
_PUSH_ALL_TAGS = ("--tags", "--follow-tags", "--mirror")
_GH_OPTIONS_WITH_VALUE = frozenset({"-R", "--repo", "--hostname"})
_PUSH_OPTIONS_WITH_VALUE = frozenset({"-o", "--push-option", "--receive-pack", "--exec", "--repo"})
_PUSH_FORCE = ("--force", "--force-with-lease", "--force-if-includes")
_PUSH_ALL_BRANCHES = frozenset({"--all", "--branches"})
# Each writes a commit to the checked-out branch; --abort and --quit only leave the operation.
_COMMITS_TO_BRANCH = frozenset({"commit", "merge", "cherry-pick", "revert", "am"})
_LEAVE_OPERATION = frozenset({"--abort", "--quit"})
_HEAD = frozenset({"HEAD", "@"})
# A new branch name follows these flags of `git switch` and `git checkout`.
_NEW_BRANCH_FLAGS = frozenset({"-b", "-B", "-c", "-C", "--create", "--force-create", "--orphan"})
# A release tag is `v*` (ADR-0023); `v<digit>.<digit>` keeps branches like `viewer-fix` and
# `v2-redesign` pushable.
_TAG_NAME = re.compile(r"^((refs/)?tags/.*|v\d+\.\d.*)$", re.IGNORECASE)
_GUARDED_API = re.compile(
    r"deployments?|environments?|rulesets?|branch(es)?/[^/\s]+/protection"
    r"|/releases|git/(refs|tags)|/dispatches|/approve",
    re.IGNORECASE,
)
_MERGE_API = re.compile(
    r"pulls/[^/\s]+/merge|/merges\b|mergePullRequest|enablePullRequestAutoMerge", re.IGNORECASE
)
_PUSH_CONFIG = re.compile(r"^remote\..+\.push$", re.IGNORECASE)
_GLUED_REDIRECT = re.compile(r"\d*[<>]")
# Variables that move git off the repository of the directory it runs in.
_GIT_LOCATION_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
_REDIRECT_FD = re.compile(r"(?<!\S)(\d*[<>]&\d*-?|&>>?)(?!\S)")
_REDIRECT_TOKEN = re.compile(r"^\d*[<>]")
_NESTED = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REMOTE_SPEC = re.compile(r"^[^/\s-][^/\s]*:")


def _tokenize(command: str) -> list[str]:
    # `$'…'` is ANSI-C quoting; shlex would keep the `$` on the first word.
    command = _REDIRECT_FD.sub(" ", command.replace("\\\n", " ").replace("$'", "'"))
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=_OPERATORS)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        # Unbalanced quote: still look at what the shell might run.
        tokens: list[str] = []
        for part in re.split(r"[;&|()`\n]+", command):
            tokens.extend(word.strip("'\"") for word in part.split())
            tokens.append(";")
        return tokens


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and all(char in _OPERATORS for char in token):
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _command_tokens(tokens: list[str]) -> list[str]:
    start = 0
    while start < len(tokens) and (
        tokens[start] in _RESERVED
        or _ASSIGNMENT.match(tokens[start])
        or tokens[start] in {"!", "function"}
    ):
        start += 2 if tokens[start] == "function" else 1  # `function f { …; }`: skip the name
    tokens = tokens[start:]
    while tokens and _REDIRECT_TOKEN.match(tokens[0]):
        bare = not tokens[0].lstrip("0123456789").strip("<>")
        tokens = tokens[2:] if bare else tokens[1:]
    return tokens


def _is_bare_install_path(path: str) -> bool:
    """install.sh or ./install.sh, the forms a prior cd deploy would make runnable."""
    return path.replace("\\", "/").rstrip("/") in {"install.sh", "./install.sh"}


def _invokes_bare_install(tokens: list[str]) -> bool:
    tokens = _command_tokens(tokens)
    if not tokens:
        return False
    name = os.path.basename(tokens[0])
    rest = tokens[1:]
    if name in _SHELLS:
        # bash -n only parses. A plain -n flag contains the letter n.
        if any(
            arg == "-n" or (arg.startswith("-") and not arg.startswith("--") and "n" in arg[1:])
            for arg in rest
        ):
            return False
        scripts = [arg for arg in rest if not arg.startswith(("-", "<"))]
        return bool(scripts) and _is_bare_install_path(scripts[0])
    if name in {".", "source"}:
        operands = [arg for arg in rest if not arg.startswith("-")]
        return bool(operands) and _is_bare_install_path(operands[0])
    return _is_bare_install_path(tokens[0])


def _directory_change(tokens: list[str]) -> str | None:
    """The path a cd or pushd names, '' when it leaves for $HOME, None when it is not one."""
    tokens = _command_tokens(tokens)
    if not tokens or os.path.basename(tokens[0]) not in {"cd", "pushd"}:
        return None
    operands = [arg for arg in tokens[1:] if not arg.startswith("-")]
    if not operands:
        return ""
    return operands[0]


def _changed_directory(cwd: str | None, change: str) -> str | None:
    """Where a cd from `cwd` lands, None when it can't be told."""
    target = os.path.expanduser(change or "~")
    if os.path.isabs(target):
        return target
    return os.path.join(cwd, target) if cwd is not None else None


@lru_cache(maxsize=16)
def _checked_out_branch(directory: str, git_dir: str | None = None) -> str | None:
    """The branch checked out in `directory`, None when it is detached or not a repository."""
    location = ["--git-dir", git_dir] if git_dir else []
    env = {key: value for key, value in os.environ.items() if key not in _GIT_LOCATION_ENV}
    try:
        result = subprocess.run(
            ["git", "-C", directory, *location, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
    except OSError:
        return None
    except subprocess.SubprocessError:  # a hung git; a separate clause keeps Python 3.9 parsing it
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


class _Where(NamedTuple):
    """Where a command runs, as far as the command line tells."""

    cwd: str | None = None
    switched: str | None = None  # the branch an earlier `git switch` in the chain moved to
    git_dir: str | None = None  # a `GIT_DIR=` prefix


class _GitCall(NamedTuple):
    subcommand: str
    rest: list[str]
    directories: list[str]  # each `-C`
    git_dir: str | None
    configs: list[str]  # each `-c key=value`


_ANYWHERE = _Where()


def _git_subcommand(tokens: list[str]) -> _GitCall | None:
    """The global options, the subcommand and its arguments of a git call."""
    directories: list[str] = []
    configs: list[str] = []
    git_dir: str | None = None
    index = 0
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        value = tokens[index + 1] if index + 1 < len(tokens) else ""
        if option == "-C":
            directories.append(value)
        elif option == "-c":
            configs.append(value)
        elif option == "--git-dir":
            git_dir = value
        elif option.startswith("--git-dir="):
            git_dir = option.split("=", 1)[1]
        index += 2 if option in _GIT_OPTIONS_WITH_VALUE else 1
    if index >= len(tokens):
        return None
    return _GitCall(tokens[index], tokens[index + 1 :], directories, git_dir, configs)


def _branch_switch(tokens: list[str]) -> str | None:
    """The branch a `git switch` or `git checkout` moves to, None when it is not clear."""
    tokens = _command_tokens(tokens)
    if not tokens or os.path.basename(tokens[0]) != "git":
        return None
    call = _git_subcommand(tokens[1:])
    if call is None or call.subcommand not in {"switch", "checkout"}:
        return None
    subcommand, rest = call.subcommand, call.rest
    for index, arg in enumerate(rest):
        if arg in _NEW_BRANCH_FLAGS:
            return rest[index + 1] if index + 1 < len(rest) else None
    if "--detach" in rest or "--" in rest:
        return None
    names = [arg for arg in rest if not arg.startswith("-")]
    if not names:
        return None
    # A checkout operand may be a commit or a path; only main is sure to be a branch.
    return names[0] if subcommand == "switch" or names[0] == _MAIN else None


def denial_reason(command: str, cwd: str | None = None) -> str | None:
    """Why `command` must not run, or None when it may.

    `cwd` is where the command runs; without it no checked-out branch is read.
    """
    return _reason(command, _Where(cwd))


def _outside_single_quotes(command: str) -> str:
    """`command` without its single-quoted text, where backticks and `$( )` stay literal."""
    kept: list[str] = []
    quote = ""
    for char in command:
        if quote == "'":
            quote = "" if char == "'" else quote
            continue
        if char == "'" and not quote:
            quote = "'"
            continue
        if char == '"':
            quote = "" if quote else '"'
        kept.append(char)
    return "".join(kept)


def _reason(command: str, where: _Where) -> str | None:
    # `$( )` and backticks run inside double quotes too.
    for match in _NESTED.finditer(_outside_single_quotes(command)):
        reason = _reason(match.group(1) or match.group(2) or "", where)
        if reason:
            return reason
    in_deploy = False
    for segment in _segments(_tokenize(command)):
        reason = _check_segment(segment, where)
        if reason:
            return reason
        # `cd deploy && ./install.sh` is two segments. The path check never sees `deploy/`.
        if in_deploy and _invokes_bare_install(segment):
            return _INSTALL_DENIED
        change = _directory_change(segment)
        if change is not None:
            in_deploy = os.path.basename(change.rstrip("/")) == "deploy"
            where = where._replace(cwd=_changed_directory(where.cwd, change), switched=None)
        switched = _branch_switch(segment)
        if switched is not None:
            where = where._replace(switched=switched)
    return None


def _git_dir_prefix(tokens: list[str]) -> str | None:
    """The value of a `GIT_DIR=` assignment before the command."""
    for token in tokens:
        if not _ASSIGNMENT.match(token):
            return None
        if token.startswith("GIT_DIR="):
            return token.split("=", 1)[1]
    return None


def _check_segment(tokens: list[str], where: _Where = _ANYWHERE) -> str | None:
    git_dir = _git_dir_prefix(tokens)
    if git_dir is not None:
        where = where._replace(git_dir=git_dir)
    tokens = _command_tokens(tokens)
    if not tokens:
        return None
    name = os.path.basename(tokens[0])
    rest = tokens[1:]

    if name == "xargs" and any(os.path.basename(tok) in _SHELLS for tok in rest):
        return f"A shell fed by xargs is blocked: it can run anything, {ADR_0023}."
    if name in _WRAPPERS:
        # `sudo -u user ssh h`: any later word could be the real command, so try each.
        for index in range(1, len(tokens)):
            reason = _check_segment(tokens[index:], where)
            if reason:
                return reason
        return None
    if name in _SHELLS:
        return _check_shell(rest, where)
    if name == "eval":
        return _reason(" ".join(rest), where)
    if name == _DEPLOY_POLLER:
        return _DEPLOY_DENIED
    if _is_host_install(tokens[0]):
        return _INSTALL_DENIED
    if name in {".", "source"}:
        operands = [arg for arg in rest if not arg.startswith("-")]
        if operands:
            return _deny_invoked_script(operands[0])
        return None
    if name == "systemctl" and _SYSTEMCTL_STARTS & set(rest):
        if any(arg.startswith(_DEPLOY_UNIT) for arg in rest):
            return _DEPLOY_DENIED
        return None
    if name in _REMOTE_SHELL_TOOLS:
        return f"{name} is blocked: {ADR_0024}."
    if name == "rsync":
        if any(tok.startswith(("-e", "--rsh")) or _REMOTE_SPEC.match(tok) for tok in rest):
            return f"rsync to a remote host is blocked: {ADR_0024}."
        return None
    if name == "find":
        for index, tok in enumerate(rest):
            if tok in {"-exec", "-execdir", "-ok", "-okdir"}:
                reason = _check_segment(rest[index + 1 :], where)
                if reason:
                    return reason
        return None
    if name == "git":
        return _check_git(rest, where)
    if name == "gh":
        return _check_gh(rest)
    github_urls = [tok for tok in rest if "api.github.com" in tok]
    if name in _FETCHERS and any(_GUARDED_API.search(tok) for tok in github_urls):
        return f"Calling GitHub deployment, environment or ruleset APIs is blocked: {ADR_0023}."
    # The merge can sit in the URL or, for graphql, in the request body.
    if name in _FETCHERS and github_urls and any(_MERGE_API.search(tok) for tok in rest):
        return f"Merging a pull request through the API is blocked: {ADR_0001}."
    return None


def _check_shell(args: list[str], where: _Where = _ANYWHERE) -> str | None:
    for index, arg in enumerate(args):
        if arg == "<<<":
            return _reason(args[index + 1], where) if index + 1 < len(args) else None
        if arg.startswith("-c") and len(arg) > 2 and not arg.startswith("--"):
            return _reason(arg[2:], where)  # `bash -c"…"`: the script is glued to the flag
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            script = args[index + 1 :]
            if script[:1] == ["--"]:  # `bash -c -- '…'` still runs the script after `--`
                script = script[1:]
            return _reason(script[0], where) if script else None
    if any(arg in {"--version", "--help", "-h", "-v"} for arg in args):
        return None
    scripts = [arg for arg in args if not arg.startswith(("-", "<"))]
    # With -n the shell only parses the script.
    if scripts and "-n" not in args:
        return _deny_invoked_script(scripts[0])
    if all(arg.startswith(("-", "<")) for arg in args):
        return f"A shell reading commands from stdin is blocked: it can run anything, {ADR_0023}."
    return None


def _check_git(args: list[str], where: _Where = _ANYWHERE) -> str | None:
    call = _git_subcommand(args)
    if call is None:
        return None
    subcommand, rest = call.subcommand, call.rest
    git_dir = call.git_dir or where.git_dir

    def branch() -> str | None:
        if not call.directories and git_dir is None and where.switched is not None:
            return where.switched
        if where.cwd is None:
            return None
        return _checked_out_branch(os.path.join(where.cwd, *call.directories), git_dir)

    if subcommand == "tag":
        return f"git tag is blocked: only the owner tags a release, {ADR_0023}."
    writes_tag_ref = "--stdin" in rest or any(arg.lower().startswith("refs/tags/") for arg in rest)
    if subcommand == "mktag" or (subcommand in {"update-ref", "symbolic-ref"} and writes_tag_ref):
        return f"Writing a tag ref is blocked: only the owner tags a release, {ADR_0023}."
    for index, arg in enumerate(rest):
        if arg.startswith("--exec=") or (arg in {"--exec", "-x"} and index + 1 < len(rest)):
            reason = _reason(arg.split("=", 1)[1] if "=" in arg else rest[index + 1], where)
            if reason:
                return reason
    # `-c remote.origin.push=<refspec>` adds refspecs the command line doesn't show.
    pushed = [
        config.split("=", 1)[1]
        for config in call.configs
        if "=" in config and _PUSH_CONFIG.match(config.split("=", 1)[0])
    ]
    if subcommand == "push":
        return _check_push(rest) or _check_push_to_main(rest, branch, pushed)
    if subcommand == "send-pack":
        return _check_push_to_main(rest, branch, pushed)
    if subcommand in _COMMITS_TO_BRANCH and not _LEAVE_OPERATION & set(rest) and branch() == _MAIN:
        return f"git {subcommand} on main is blocked: work on a ticket branch, {ADR_0001}."
    return None


def _check_push(args: list[str]) -> str | None:
    blocked = f"Pushing a tag is blocked: only the owner tags a release, {ADR_0023}."
    for arg in args:
        if arg.startswith("--"):
            flag = arg.split("=", 1)[0]
            if len(flag) >= 4 and any(name.startswith(flag) for name in _PUSH_ALL_TAGS):
                return blocked
        elif not arg.startswith("-"):
            if "$" in arg or "*" in arg:
                return blocked
            for side in arg.lstrip("+").split(":"):
                if _TAG_NAME.match(side):
                    return blocked
    return None


def _is_force_flag(arg: str) -> bool:
    if arg.startswith("--"):
        flag = arg.split("=", 1)[0]
        # git accepts an unambiguous prefix; `--fo` could still be --follow-tags.
        return len(flag) >= 5 and any(name.startswith(flag) for name in _PUSH_FORCE)
    for char in arg[1:]:
        if char == "f":
            return True
        if char == "o":  # `-oci.skip`: the rest is the push option's value
            return False
    return False


def _check_push_to_main(
    args: list[str], branch: Callable[[], str | None], configured: list[str]
) -> str | None:
    positional: list[str] = []
    names_remote = False  # `--repo` names the remote, so every word is a refspec
    index = 0
    while index < len(args):
        arg = args[index]
        if _REDIRECT_TOKEN.match(arg):
            bare = not arg.lstrip("0123456789").strip("<>")
            index += 2 if bare else 1
            continue
        if arg.startswith("-") and arg != "-":
            if _is_force_flag(arg):
                return f"Force pushing is blocked: {ADR_0001}."
            if arg in _PUSH_ALL_BRANCHES:
                return f"Pushing every branch is blocked: it pushes main, {ADR_0001}."
            names_remote = names_remote or arg.startswith("--repo")
            index += 2 if arg in _PUSH_OPTIONS_WITH_VALUE else 1
            continue
        positional.append(_GLUED_REDIRECT.split(arg, 1)[0])  # `main>out.log`
        index += 1
    refspecs = positional if names_remote else positional[1:]  # else the first is the remote
    pushes_head = not refspecs
    for refspec in refspecs + configured:
        if refspec.startswith("+"):
            return f"Force pushing is blocked: {ADR_0001}."
        target = refspec.rsplit(":", 1)[-1] or refspec.split(":", 1)[0]
        target = target.removeprefix("refs/").removeprefix("heads/")
        if target == _MAIN:
            return f"Pushing to main is blocked: open a pull request instead, {ADR_0001}."
        pushes_head = pushes_head or target in _HEAD
    if pushes_head and branch() == _MAIN:
        return f"Pushing main is blocked: open a pull request from a ticket branch, {ADR_0001}."
    return None


def _is_guarded_api_argument(arg: str, pattern: re.Pattern[str] = _GUARDED_API) -> bool:
    """An endpoint path or a graphql query, not a field value such as `-f body=deployment`."""
    if arg.startswith("-"):
        return False
    if arg.startswith(("query=", "query:")):
        return bool(pattern.search(arg))
    path = arg.split("?", 1)[0]
    return "/" in path and "=" not in path and bool(pattern.search(path))


def _is_write_call(args: list[str]) -> bool:
    """gh api defaults to GET, and to POST once it carries a field."""
    for index, arg in enumerate(args):
        if arg in {"-X", "--method"} and index + 1 < len(args):
            return args[index + 1].upper() != "GET"
        if arg.startswith(("-X", "--method=")) and arg not in {"-X", "--method"}:
            return arg.split("=", 1)[-1].removeprefix("-X").upper() != "GET"
    return any(arg in {"-f", "-F", "--field", "--raw-field", "--input"} for arg in args)


def _gh_subcommand(args: list[str]) -> str | None:
    """The first word after the group's flags: `gh pr -R o/r merge 1` is a merge."""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in _GH_OPTIONS_WITH_VALUE else 1
    return args[index] if index < len(args) else None


def _split_glued_fields(args: list[str]) -> list[str]:
    """`-fquery=…` and `--field=query=…` as `-f query=…`, so the value is read like any field."""
    split: list[str] = []
    for arg in args:
        if arg[:2] in {"-f", "-F"} and len(arg) > 2:
            split.extend([arg[:2], arg[2:]])
        elif arg.startswith(("--field=", "--raw-field=")):
            split.extend(arg.split("=", 1))
        else:
            split.append(arg)
    return split


def _check_gh(args: list[str]) -> str | None:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in _GH_OPTIONS_WITH_VALUE else 1
    if index >= len(args):
        return None
    group, rest = args[index], args[index + 1 :]
    if group == "release":
        return f"gh release is blocked: only the owner releases, {ADR_0023}."
    if group == "workflow" and rest[:1] in (["run"], ["enable"], ["disable"]):
        names = [arg for arg in rest[1:] if not arg.startswith("-")]
        # A flag's value can precede the name (`-f x=1 release.yml`), so look at every word.
        if not names or any(name.isdigit() or "release" in name.lower() for name in names):
            return f"Running the release workflow is blocked: only the owner releases, {ADR_0023}."
        return None
    if group == "pr" and _gh_subcommand(rest) == "merge":
        return f"gh pr merge is blocked: only a human merges a pull request, {ADR_0001}."
    if group == "api":
        rest = _split_glued_fields(rest)
    if group == "api" and any(_is_guarded_api_argument(arg) for arg in rest):
        return (
            "gh api on deployments, environments, rulesets or pending deployments is blocked: "
            f"{ADR_0023}."
        )
    if (
        group == "api"
        and _is_write_call(rest)
        and any(_is_guarded_api_argument(arg, _MERGE_API) for arg in rest)
    ):
        return f"Merging a pull request through gh api is blocked: {ADR_0001}."
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 0
    cwd = payload.get("cwd")
    reason = denial_reason(command, cwd if isinstance(cwd, str) else None)
    if reason:
        decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
