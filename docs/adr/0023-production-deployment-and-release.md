# ADR 0023 — Production deployment and release

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-11 |
| Related | PRD-0004 (G1, G8, G12, G13, B26), ADR-0001, ADR-0002, ADR-0004, ADR-0005, ADR-0018, ADR-0021, ADR-0024 |

## Context

PRD 0004 brings the POC online: one production instance on the owner's Cloudflare-managed domain (G12), with data that survives every deploy (G13). Today:

- The stack runs only locally. `compose.yaml` publishes Postgres and the app on every interface, with hardcoded credentials.
- There is no TLS, no proxy-header trust and no image publishing. The OAuth redirect URI is built from `request.url_for`, which yields `http://` behind a TLS-terminating proxy.
- The GitHub repo is public. Its `main` ruleset has no required checks and an "always" admin bypass. There are no environments, and Dependabot is off.

The owner set four constraints on 2026-10-11:

- Caddy behind the Cloudflare proxy. No Traefik.
- A release needs a `v*` tag and the owner's approval, and the server pulls the image. GitHub holds no credential to the server.
- Security is critical: only the owner can release to production.
- The first deploy waits until M1a and M1 are merged.

## Decision

### Host

- **Hetzner Cloud CX23** (2 vCPU, 4 GB, x86) in an EU location, running Debian 13.
  - It costs €5.49 a month before VAT and IPv4 after the [15 June 2026 price adjustment](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/). That is enough for Postgres, the app and Caddy, since model calls run remotely.
  - Fallback: **CAX11** (Arm, €5.99), which needs a `linux/arm64` image as well.
  - Reviews report the cost-optimized plans as intermittently out of stock ([bitdoze](https://www.bitdoze.com/hetzner-cloud-cost-optimized-plans/)), so the provisioning ticket checks stock first.
- **Hetzner Cloud Firewall** (outside the VM) admits 443/tcp from [Cloudflare's IP ranges](https://www.cloudflare.com/ips/) only, and 22/tcp from the owner's address only. Docker-published ports bypass a host firewall such as ufw, so the cloud firewall is the control that counts.
- **Host hardening:**
  - SSH is key-only: no root login, no passwords.
  - `unattended-upgrades` is on.
  - A `deploy` user owns `/opt/ai-trainer`. Docker-group membership is root-equivalent, so that account is treated as root.
  - Provisioning is a committed cloud-init file plus a runbook in `deploy/`.

### Ingress and TLS

- **Cloudflare:**
  - DNS is proxied, and SSL/TLS mode is **Full (strict)**.
  - Caddy serves a **Cloudflare Origin CA** certificate with `tls cert.pem key.pem`.
  - **Zone-level Authenticated Origin Pulls** use a client certificate from a CA the owner controls. That is stricter than global AOP, whose certificate is shared across all Cloudflare accounts ([Cloudflare docs](https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/set-up/zone-level/)).
  - Caddy enforces it with `client_auth { trust_pool file <ca.pem> }`, which defaults to `require_and_verify` ([Caddy tls directive](https://caddyserver.com/docs/caddyfile/directives/tls)). A request that doesn't come through Cloudflare fails the TLS handshake.
- **Caddy** is the only container that publishes a port (443). It reverse-proxies to `app:8000` on an internal network and sets HSTS.
- **The app behind the proxy:**
  - Uvicorn runs with `--proxy-headers` and `--forwarded-allow-ips` set to the compose network's subnet, never `*` ([Uvicorn settings](https://github.com/kludex/uvicorn/blob/main/docs/settings.md)).
  - A new `public_base_url` setting builds the OAuth redirect URI. It is set to `http://localhost:8000` locally and to `https://<domain>` in production. A request's Host header never shapes a redirect.
  - `TrustedHostMiddleware` admits only the host of `public_base_url`.
  - `SESSION_COOKIE_SECURE` stays on (ADR-0005 invariant 5).
- **Google OAuth:** the production OAuth client is separate from the development one, with the production redirect URI only. The consent screen is published to production.

### `compose.prod.yaml`

- **Services:** `db`, `migrate` (one-shot `alembic upgrade head`), `app` and `caddy`. Images are referenced by digest, never by tag.
- **Exposure:** no `ports:` on `db` or `app`.
- **Runtime:**
  - `restart: unless-stopped`.
  - Memory limits.
  - Log rotation on the `json-file` driver.
  - `no-new-privileges`, with all capabilities dropped where the image allows it.
- **Secrets:**
  - They live in `/opt/ai-trainer/.env`, mode 0600, owned by `deploy`. They are never in the image, which `.dockerignore` already guarantees.
  - The database password is random and appears only in the server's env files.
  - The production compose file reads it for the `db` container from a separate env file, so the `.env.example`↔`Settings` parity test (`tests/unit/test_settings.py`) stays strict.
- **Workers:** one Uvicorn worker. The SSE streams and a 4 GB host don't call for more until there's real load.

### Release pipeline

- **`release.yml`** runs on push of a `v*` tag.
  - **`build` job:**
    - Builds the Dockerfile and pushes `ghcr.io/stanislawk/ai-trainer` by digest.
    - Attests provenance with `actions/attest-build-provenance` and `push-to-registry: true`.
    - Job permissions are `contents: read`, `packages: write`, `id-token: write` and `attestations: write` ([GitHub docs](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)).
    - The package is public. The repo is public, the image holds no secrets, and the server then needs no registry credential.
  - **`release` job:** runs in the **`production` environment**.
    - Its only required reviewer is the owner. Self-review is allowed, since the owner is the only reviewer.
    - Deployment tags are restricted to `v*`.
    - Required reviewers are available on public repos on the free plan ([GitHub community](https://github.com/orgs/community/discussions/170307)).
    - Once approved, it creates a GitHub **Deployment** for environment `production` whose payload is the image digest. It needs no secret.
- **Who can release:**
  - A **tag ruleset** lets only repository admins create, update or delete `v*` tags.
  - The **`main` ruleset**:
    - requires the CI checks;
    - changes the admin bypass from "always" to "for pull requests only", so nobody pushes to `main` directly;
    - blocks force-push and deletion, as today.
- **Agents never release.** A `PreToolUse` hook in `.claude/settings.json` denies:
  - `git tag`;
  - pushing tags;
  - `gh release`;
  - running the release workflow;
  - `gh api` calls on deployments, environments, rulesets or pending deployments.

  Agents act with the owner's GitHub credentials, so this hook is the hard stop (ADR-0001 Consequences).

### Pull-based deploy on the server

- A **systemd timer** runs `deploy/deploy.sh` every two minutes. It uses a **fine-grained token** scoped to this one repository, with Deployments read/write and Metadata read. The token lives in a 0600 credentials file. The script:
  1. Reads the newest `production` deployment in state `queued` that was created by `github-actions[bot]`.
  2. Verifies the image digest with `gh attestation verify oci://ghcr.io/stanislawk/ai-trainer@<digest> -R stanislawK/ai-trainer --signer-workflow stanislawK/ai-trainer/.github/workflows/release.yml --deny-self-hosted-runners` ([gh manual](https://cli.github.com/manual/gh_attestation_verify)). A failed check stops the run.
  3. Marks the deployment `in_progress`, then pulls the image by digest.
  4. Takes a **pre-migration backup** (`pg_dump -Fc`, kept locally and uploaded as in Backups).
  5. Runs `migrate`, then `up -d app`.
  6. Polls `/health` through Caddy.
  7. Reports `success`, or on failure rolls `app` back to the previous digest and reports `failure`. Migrations are expand-only (ADR-0024), so the previous code still runs on the new schema. The database itself is never rolled back automatically.
- **The server's scheduled jobs**, all as systemd timers:
  - the deploy poller;
  - the nightly backup;
  - the daily `ai-trainer purge-conversations` run (ADR-0021).

### Backups

- **Nightly:** `pg_dump -Fc`, encrypted with **age** to the owner's public key, uploaded to a **Cloudflare R2** bucket. The private key stays offline with the owner, so neither the server nor R2 can read a backup.
- **Retention:** R2 lifecycle rules keep 7 daily and 4 weekly copies. **Bucket locks** stop deletion inside that window, even with the server's R2 token ([R2 bucket locks](https://developers.cloudflare.com/r2/buckets/bucket-locks/)).
- **Restore drill:** `deploy/restore-drill.sh` restores the latest backup into a throwaway Postgres container and compares row counts per table. It runs at go-live, then monthly.
- **Second layer:** Hetzner's own server backups are turned on (+20% of the server price). They don't replace the offsite dump.

### GitHub account and repository hardening

- **Dependabot:** alerts and security updates on, plus `.github/dependabot.yml` for `uv`, `docker` and `github-actions`.
- **Actions:**
  - Turn on the setting that requires actions to be pinned to a full commit SHA. Every action is already pinned that way.
  - Every workflow keeps `permissions: contents: read` at the top, and widens it per job only.
  - `evals.yml` passes `inputs.*` through `env:`, never inline in `run:`.
- **Ownership:** `CODEOWNERS` assigns `*` to the owner. The `evals` environment exists explicitly, with its secrets.
- **Owner account checklist**, done by hand and recorded in the ticket:
  - 2FA with a passkey or security key.
  - Review PATs, SSH and GPG keys, authorized OAuth and GitHub Apps, and active sessions.
  - The `gh` token used by agents is the narrowest one that works.
  - Hetzner, Cloudflare and Google Cloud accounts have 2FA.

### Invariants

1. Only the project owner can cause a production deploy: an admin-only `v*` tag, then the owner's approval of the `production` environment. Agents never tag, approve or call deployment APIs.
2. The server runs only images whose provenance verifies against this repository's `release.yml`, pulled by digest.
3. GitHub holds no credential that can log in to or write to the server. The server holds only a single-repo deployments token.
4. Only Caddy's 443 is reachable, only from Cloudflare, and only with Cloudflare's AOP client certificate. Postgres and the app are never published.
5. Every deploy that runs `migrate` takes a backup first. A failed deploy rolls back the app, never the database.
6. No secret is in the image, the repo, a workflow log or a deployment payload.

## Alternatives considered

- Traefik — label-driven routing pays off with many services. For one app it adds moving parts. The owner chose Caddy.
- Cloudflare Tunnel — no inbound port at all, but the owner preferred Caddy behind the proxy. AOP plus the firewall allowlist closes the direct-to-origin path.
- Actions SSH-ing in — simpler, but it puts a server credential in GitHub (invariant 3).
- Watchtower-style auto-update on a tag — no approval step and no provenance check.

## Consequences

- **Supersedes in part:** ADR-0005's "the POC runs locally". Amend its Consequences on acceptance: production uses `public_base_url` and a separate OAuth client.
- **New paths:** `compose.prod.yaml`, `deploy/` (cloud-init, Caddyfile, scripts, systemd units, runbook) and `.github/workflows/release.yml`. `.claude/rules/operability.md` gains `deploy/**` and `compose.prod.yaml` in its scope.
- **CLAUDE.md:**
  - The commands section gains the release steps: tag, approve and watch.
  - The non-negotiables gain invariant 1.
  - Status records when the data gate goes live.
- **Hook:** the denying hook ships before `release.yml` exists (#214): `.claude/hooks/guard_release.py`, registered in `.claude/settings.json` and tested in `tests/unit/test_guard_release_hook.py`.
  - It also denies `ssh`, `scp`, `sftp`, remote `rsync`, a shell fed from stdin, and release-shaped `gh api` paths (`releases`, `git/refs`, `dispatches`).
  - It is a guard against an agent's honest mistakes, not a sandbox. Variable expansion, git aliases, scripts written then run, and other interpreters (`python -c`) get past it. A command that only names a guarded word as a wrapper argument (`sudo cat ssh`) is denied too. Branch protection and the `production` environment's required reviewer remain the hard stop.
  - A `v<digit>.<digit>…` or `tags/…` push argument counts as a tag; a branch named like that needs the owner.
- ⚠ **Verify at the deploy-poller ticket:**
  - That a fine-grained token's "Deployments: read and write" permission covers listing deployments and creating deployment statuses. The REST docs name only the classic `repo_deployment` scope ([GitHub REST](https://docs.github.com/en/rest/deployments/statuses)).
  - If it doesn't, use a GitHub App installation token scoped to the repo.
- ⚠ **Verify at the hardening ticket:**
  - The ruleset API's field for "bypass for pull requests only". Third-party docs suggest a `bypass_mode` value, which isn't confirmed.
  - That an "always" bypass isn't needed for a solo owner to merge their own PR with 1 required approval. If it is, drop the required approval count to 0, keep required checks, and keep bypass "for pull requests only".
- ⚠ **Verify at provisioning:** CX23 stock and the price at order time. The owner keeps that record privately, outside this public repo (`deploy/RUNBOOK.md`, #222).
- **`compose.prod.yaml` gotchas (#221):**
  - It sets its own Compose project name, `ai-trainer-prod`. `compose.yaml` is project `ai-trainer`, and two files in one directory with the same service names would otherwise replace each other's containers.
  - `no-new-privileges` blocks file capabilities. The Caddy image grants `cap_net_bind_service` to its binary that way, so with every capability dropped Caddy can't bind 443 unless `cap_add: NET_BIND_SERVICE` puts it in the root process's bounding set.
  - With every capability dropped, root in a container has no `DAC_OVERRIDE`. Caddy can't read a certificate or key owned by another user with mode 0600. On the server, `certs/` and its files are owned by root: the directory 0700, the keys 0600.
  - The Postgres entrypoint starts as root and switches to `postgres` with gosu, so `db` adds back `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID` and `SETUID`.
  - Postgres sits on an `internal: true` network with no route out. The app's proxy-header trust (`FORWARDED_ALLOW_IPS`) is the fixed `edge` subnet shared only with Caddy.
  - TLS 1.3 clients finish their side of the handshake before the server checks the client certificate. Without one, curl reports the server's `certificate required` alert on its first read (exit 56) rather than at connect (exit 35, as under TLS 1.2). Either way no request reaches the app.
- **`release.yml` gotchas (#219):**
  - `actions/attest-build-provenance` v4 creates an artifact-metadata storage record by default. That works only for organization-owned repositories and needs `artifact-metadata: write`. This repository is user-owned, so the step sets `create-storage-record: false` and the job keeps exactly the four permissions above.
  - GHCR creates a new package as private. The owner makes `ai-trainer` public once, after the first release (`deploy/github-hardening.md`).
  - `.dockerignore` excludes `.env`, `*.env`, `*.pem` and `*.key` at any depth: `make smoke-setup` writes `.smoke/.env`, `.smoke/db.env` and test private keys into the working tree, and a root-only `.env` pattern let them into a local build. Before attesting, the build job also fails if the pushed image holds an env file or a private key, so such an image is never attested.
- **`release` job gotchas (#220):**
  - A job that names an environment makes its own deployment by default, while it waits for review, and a rejection leaves it behind. The job sets `environment: {name: production, deployment: false}`, which keeps required reviewers and wait timers but creates no deployment object ([GitHub docs](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments)). The only `production` deployment is the one the approved job posts, so a rejection creates none. Custom deployment protection rules don't work with `deployment: false`.
  - A new deployment has no status. The job posts a `queued` status itself, because the poller looks for `queued`.
  - The deployments API refuses a deployment until every check on the commit has passed, and this run's own checks are still in progress. The job sends `required_contexts: []`; `main`'s ruleset already required `ci` before the tagged commit merged. `auto_merge: false` stops the API trying to merge `main` into a tag.
  - The payload is `{"digest": "sha256:…"}`, checked against `^sha256:[0-9a-f]{64}$` first. The job token has `deployments: write` only, and each `gh api` call keeps only the id, so the log shows no response body.
- **`deploy/cloud-init.yaml` gotchas (#222):**
  - The owner's SSH key is the one picked in Hetzner's console, not a key in the repo. Hetzner hands it over as instance metadata, and cloud-init installs metadata keys only for the default user, so the file's `user:` entry makes `deploy` that default user. A `deploy` listed only under `users:` would get no key.
  - `deploy` has no password at all, so sudo is `NOPASSWD`. That adds nothing beyond its Docker-group membership, which is already root-equivalent.
  - sshd keeps the first value it reads, and Debian includes `sshd_config.d/` at the top of `sshd_config`. The hardening drop-in is `10-ai-trainer.conf`, so it wins over cloud-init's own `50-cloud-init.conf`.
  - Docker comes from Debian's archive (`docker.io`, `docker-cli`, `docker-compose`: Docker 26.1 and compose 2.26 on trixie), so Debian-Security patches it and unattended-upgrades applies the patches. Docker's own apt repository would need its origin added to unattended-upgrades by hand. Compose 2.26 accepts `compose.prod.yaml`.
  - The `docker` group exists only once the packages are installed, after the users stage, so `deploy` joins it in `runcmd`. `20auto-upgrades` is a deferred `write_files` entry, written after the packages so their own setup can't replace it.
  - `make cloud-init-check` runs `cloud-init schema` in a `debian:trixie` container (cloud-init 25.1 there), and CI runs it too. The macOS laptop has no cloud-init.
  - The runbook keeps the server's addresses and the provisioning record (type, location, price) out of the repo, which is public. The owner keeps them privately.
- **`deploy/backup.sh` gotchas (#223):**
  - `backup.sh nightly` (the `ai-trainer-backup` timer, 02:30 UTC, `Persistent=true`) writes to `daily/`, or to `weekly/` on a Sunday (UTC). `backup.sh predeploy` writes to `predeploy/` and is what the deploy poller runs before `migrate`. Each prefix has an R2 lock and a lifecycle expiry of the same age: 7, 28 and 7 days. A lock overrides a lifecycle rule ([R2 bucket locks](https://developers.cloudflare.com/r2/buckets/bucket-locks/)), so equal ages never delete early. Sunday's copy goes to `weekly/` only, so "7 daily" is six `daily/` objects plus that one. The pre-deploy local copy is left to the poller ticket (#225).
  - `age` (1.2.1) and `rclone` (1.60.1) come from Debian 13's archive through cloud-init, so unattended-upgrades patches them like Docker. rclone's `r2` remote is configured by `RCLONE_CONFIG_R2_*` in `/opt/ai-trainer/backup.env` alone, with `NO_CHECK_BUCKET=true` because the token can't create buckets.
  - R2 has no write-only token. The server's bucket-scoped "Object Read & Write" token can delete, and only the lock refuses it. Changing a lock needs an admin token, which never goes to the server.
  - The dump never touches disk: `docker exec … pg_dump | age` writes to a FIFO that `rclone rcat` reads. A truncated object would sit under the lock for its whole window, so when the dump or age fails, rclone is killed while the FIFO is still open. It never sees EOF, never completes the PUT or the multipart upload, and the abandoned parts expire through `--abort-multipart-days 1`.
  - When the pipeline fails, a successful `kill` doesn't prove rclone was still running. bash may not have reaped an rclone that already failed and broke the pipe under age, and killing a zombie succeeds. The script sends `SIGKILL` and reads rclone's status: 137 means the dump failed, and anything else means the upload did. Testing liveness with `kill` misreported 28 of 30 mid-stream upload failures on Linux.
  - rclone's stderr is passed through a filter that replaces the value of any environment variable named like a key, secret, token or password with `***`. It works line by line: if rclone hex-dumps an error body it can't parse, an echoed key is split across lines and gets through. R2 doesn't echo credentials.
  - `make backup-check` runs the real round trip (throwaway key, local directory as the bucket, decrypt, `pg_restore --list`) in a `debian:trixie` container with the host's tool versions and the Docker socket mounted, against the `make start` database.
- **Observability:** an external uptime check and ADR-0018's OTLP export (Grafana Cloud) are recommended at go-live, but not required.
