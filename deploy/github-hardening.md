# GitHub repository hardening (owner checklist)

Source: ADR-0023 (*Who can release*, *GitHub account and repository hardening*), PRD 0005 G12.
Agents cannot apply any of this: the `guard_release.py` hook denies the rulesets API (#214). The owner does it by hand in **Settings** of `stanislawK/ai-trainer`, then ticks the boxes in a comment on #215.

The files are committed: `.github/dependabot.yml`, `.github/CODEOWNERS`, and `evals.yml` (inputs go through `env:`). `tests/unit/test_repo_hardening.py` guards them.

## 1. Tag ruleset: only admins touch `v*`

Settings → Rules → Rulesets → New ruleset → **New tag ruleset**.

- [ ] Name `release-tags`, enforcement **Active**.
- [ ] Target tags matching `v*`.
- [ ] Bypass list: **Repository admin** role only.
- [ ] Rules ticked: **Restrict creations**, **Restrict updates**, **Restrict deletions**.

## 2. `main` ruleset

Settings → Rules → Rulesets → edit the ruleset that targets `main`.

- [ ] Enforcement **Active**, target `main` (default branch).
- [ ] **Require status checks to pass** → add `ci` (the job id in `ci.yml`). Leave "Require branches to be up to date" on.
- [ ] **Require a pull request before merging**. No required approvals, so the owner can merge their own PR once `ci` passes.
- [ ] **Block force pushes** and **Restrict deletions**.
- [ ] Bypass list: **Repository admin**, with the mode set to **For pull requests only** (not "Always"). This stops anyone pushing to `main` directly. ADR-0023 requires it.
- Known limit: a bypass actor can use "Merge without waiting for requirements" on a PR, which skips the `ci` check for the owner. The owner must not use that option; the required check only binds non-admins. Without the bypass the owner could still merge their own PR once `ci` passes (no required approvals), so the bypass is a deliberate escape hatch, not what makes self-merge work.
- Only `ci` is required. `e2e` (which `needs: ci`) is not, on purpose: it needs the full compose stack. Add it here if it should block merges.

## 3. Dependabot

Settings → Advanced Security (Code security).

- [ ] **Dependabot alerts**: enabled.
- [ ] **Dependabot security updates**: enabled.
- [ ] After merge, Insights → Dependency graph → Dependabot lists `uv`, `docker` and `github-actions`.

## 4. Actions

Settings → Actions → General.

- [ ] **Require actions to be pinned to a full-length commit SHA**: on (every action in the workflows already is).
- [ ] Workflow permissions: **Read repository contents** by default.

Settings → Environments.

- [ ] The `evals` environment exists, with `OPENROUTER_API_KEY`, `EVAL_JUDGE_MODEL`, `ROUTER_MODEL`, `CHITCHAT_MODEL`, `EXTRACT_MODEL` and `EXTRACT_PROVIDER` as its secrets.
- [ ] A `production` environment exists (#220), before the first `v*` tag. Settings → Environments → New environment → `production`:
  - [ ] **Required reviewers**: on, with the owner as the only reviewer.
  - [ ] **Prevent self-review**: off. The owner pushes the tag and is the only reviewer, so with it on nobody could approve.
  - [ ] **Deployment branches and tags** → **Selected branches and tags** → add a **tag** rule `v*`, and no branch rule.
  - [ ] No secrets or variables. The `release` job needs none.

After the first `release.yml` run (#219), on the profile's **Packages** tab → `ai-trainer` → Package settings.

- [ ] Danger Zone → **Change visibility** → **Public**, then type the package name to confirm. GHCR creates a new package as private, and the server pulls without a registry credential (ADR-0023). This cannot be undone: a public package never goes private again.
- [ ] Logged out (`docker logout ghcr.io`), `docker pull --platform linux/amd64 ghcr.io/stanislawk/ai-trainer@sha256:<digest>` succeeds. The image is amd64 only, so on an Apple silicon Mac the pull needs `--platform`.

## 5. Owner account

- [ ] 2FA with a passkey or security key on GitHub.
- [ ] Reviewed: personal access tokens, SSH keys, GPG keys, authorized OAuth apps, installed GitHub Apps, active sessions. Removed what is unused.
- [ ] The `gh` token agents use is the narrowest one that works (a fine-grained token on this repo only; no `administration`, `deployments` or `workflow` scope).
- [ ] Hetzner, Cloudflare and Google Cloud accounts have 2FA.

## 6. Verify

- [ ] From a scratch commit: `git push origin HEAD:main` is **rejected** (protected branch / required pull request).
- [ ] A PR whose `ci` fails shows the merge button blocked ("Required statuses must pass"). The owner, as a bypass actor, will see an extra "Merge without waiting for requirements" option; do not use it.
- [ ] The same PR with `ci` green: the owner merges their own PR. Record which setting allowed it: "Require a pull request" with zero required approvals, so no second reviewer is needed. The bypass is only the escape hatch.
- [ ] `git tag v0.0.0-test && git push origin v0.0.0-test` from a non-admin account is rejected; delete any test tag afterwards as an admin.
