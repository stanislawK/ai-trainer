"""PreToolUse hook: agents never release or reach production (ADR-0023, ADR-0024).

Reads the hook JSON on stdin. For a Bash call whose command would tag, push a tag, create a
release, run the release workflow, touch GitHub deployments, environments or rulesets, run the
deploy poller, run deploy/install.sh, or open an ssh/scp connection, it prints a `deny`
decision and exits 0. Everything
else passes silently.

Standard library only, and kept compatible with the system `python3`, so the hook runs without the
project environment. The command is split on shell operators, `$( )`, backticks and `bash -c`, so
a denied command chained after `&&`, `;` or `|` is still found. This is a guard against honest
mistakes by an agent holding the owner's credentials, not a sandbox: obfuscation such as
`eval "$(echo … | base64 -d)"` or a git alias can get past it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

ADR_0023 = "ADR-0023 (agents never release)"
ADR_0024 = "ADR-0024 invariant 5 (no agent reaches the production host)"
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
# A release tag is `v*` (ADR-0023); `v<digit>.<digit>` keeps branches like `viewer-fix` and
# `v2-redesign` pushable.
_TAG_NAME = re.compile(r"^((refs/)?tags/.*|v\d+\.\d.*)$", re.IGNORECASE)
_GUARDED_API = re.compile(
    r"deployments?|environments?|rulesets?|branch(es)?/[^/\s]+/protection"
    r"|/releases|git/(refs|tags)|/dispatches|/approve",
    re.IGNORECASE,
)
_REDIRECT_FD = re.compile(r"(?<!\S)(\d*[<>]&\d*-?|&>>?)(?!\S)")
_REDIRECT_TOKEN = re.compile(r"^\d*[<>]")
_NESTED = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REMOTE_SPEC = re.compile(r"^[^/\s-][^/\s]*:")


def _tokenize(command: str) -> list[str]:
    command = _REDIRECT_FD.sub(" ", command.replace("\\\n", " "))
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
        tokens[start] in _RESERVED or _ASSIGNMENT.match(tokens[start]) or tokens[start] == "!"
    ):
        start += 1
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


def denial_reason(command: str) -> str | None:
    """Why `command` must not run, or None when it may."""
    for match in _NESTED.finditer(command):  # `$( )` and backticks inside double quotes
        reason = denial_reason(match.group(1) or match.group(2) or "")
        if reason:
            return reason
    in_deploy = False
    for segment in _segments(_tokenize(command)):
        reason = _check_segment(segment)
        if reason:
            return reason
        # `cd deploy && ./install.sh` is two segments. The path check never sees `deploy/`.
        if in_deploy and _invokes_bare_install(segment):
            return _INSTALL_DENIED
        change = _directory_change(segment)
        if change is not None:
            in_deploy = os.path.basename(change.rstrip("/")) == "deploy"
    return None


def _check_segment(tokens: list[str]) -> str | None:
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
            reason = _check_segment(tokens[index:])
            if reason:
                return reason
        return None
    if name in _SHELLS:
        return _check_shell(rest)
    if name == "eval":
        return denial_reason(" ".join(rest))
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
                reason = _check_segment(rest[index + 1 :])
                if reason:
                    return reason
        return None
    if name == "git":
        return _check_git(rest)
    if name == "gh":
        return _check_gh(rest)
    if name in _FETCHERS and any(
        "api.github.com" in tok and _GUARDED_API.search(tok) for tok in rest
    ):
        return f"Calling GitHub deployment, environment or ruleset APIs is blocked: {ADR_0023}."
    return None


def _check_shell(args: list[str]) -> str | None:
    for index, arg in enumerate(args):
        if arg == "<<<":
            return denial_reason(args[index + 1]) if index + 1 < len(args) else None
        if arg.startswith("-c") and len(arg) > 2 and not arg.startswith("--"):
            return denial_reason(arg[2:])  # `bash -c"…"`: the script is glued to the flag
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            return denial_reason(args[index + 1]) if index + 1 < len(args) else None
    if any(arg in {"--version", "--help", "-h", "-v"} for arg in args):
        return None
    scripts = [arg for arg in args if not arg.startswith(("-", "<"))]
    # With -n the shell only parses the script.
    if scripts and "-n" not in args:
        return _deny_invoked_script(scripts[0])
    if all(arg.startswith(("-", "<")) for arg in args):
        return f"A shell reading commands from stdin is blocked: it can run anything, {ADR_0023}."
    return None


def _check_git(args: list[str]) -> str | None:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in _GIT_OPTIONS_WITH_VALUE else 1
    if index >= len(args):
        return None
    subcommand, rest = args[index], args[index + 1 :]
    if subcommand == "tag":
        return f"git tag is blocked: only the owner tags a release, {ADR_0023}."
    writes_tag_ref = "--stdin" in rest or any(arg.lower().startswith("refs/tags/") for arg in rest)
    if subcommand == "mktag" or (subcommand in {"update-ref", "symbolic-ref"} and writes_tag_ref):
        return f"Writing a tag ref is blocked: only the owner tags a release, {ADR_0023}."
    for index, arg in enumerate(rest):
        if arg.startswith("--exec=") or (arg in {"--exec", "-x"} and index + 1 < len(rest)):
            reason = denial_reason(arg.split("=", 1)[1] if "=" in arg else rest[index + 1])
            if reason:
                return reason
    if subcommand == "push":
        return _check_push(rest)
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


def _is_guarded_api_argument(arg: str) -> bool:
    """An endpoint path or a graphql query, not a field value such as `-f body=deployment`."""
    if arg.startswith("-"):
        return False
    if arg.startswith(("query=", "query:")):
        return bool(_GUARDED_API.search(arg))
    path = arg.split("?", 1)[0]
    return "/" in path and "=" not in path and bool(_GUARDED_API.search(path))


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
    if group == "api" and any(_is_guarded_api_argument(arg) for arg in rest):
        return (
            "gh api on deployments, environments, rulesets or pending deployments is blocked: "
            f"{ADR_0023}."
        )
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
    reason = denial_reason(command)
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
