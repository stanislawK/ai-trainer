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
  - Caddy enforces it with `client_auth { mode require_and_verify; trust_pool file <ca.pem> }` (amended 2026-10-24, see *Deploy audit follow-ups* in Consequences) ([Caddy tls directive](https://caddyserver.com/docs/caddyfile/directives/tls)). The mode is set in `deploy/Caddyfile`, so a later image cannot fall back to `request`. A request that doesn't come through Cloudflare fails the TLS handshake. The firewall allows every Cloudflare address, not this zone, and Caddy trusts the CA rather than one leaf: a leaked client leaf or CA key can be presented from another Cloudflare zone.
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

- A **systemd timer** runs `deploy/deploy.sh` every two minutes. It uses a **fine-grained token** scoped to this one repository, with Deployments read/write, Attestations read and Metadata read (amended 2026-10-24, #225). The token lives in a 0600 credentials file. The script:
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
- **Verified at the deploy-poller ticket (#225):** a fine-grained token's *Deployments* permission covers listing deployments (read) and creating deployment statuses (write), so no GitHub App is needed ([permissions for fine-grained tokens](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens), read 2026-10-24). `gh attestation verify` also reads `GET /repos/…/attestations/{digest}`, which needs *Attestations: read*. The owner approved adding it to the token on 2026-10-24, at #225's plan gate.
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
  - `backup.sh nightly` (the `ai-trainer-backup` timer, 02:30 UTC, `Persistent=true`) writes to `daily/`, or to `weekly/` on a Sunday (UTC). `backup.sh predeploy` writes to `predeploy/` and is what the deploy poller runs before `migrate`. Each prefix has an R2 lock and a lifecycle expiry of the same age: 7, 28 and 7 days. A lock overrides a lifecycle rule ([R2 bucket locks](https://developers.cloudflare.com/r2/buckets/bucket-locks/)), so equal ages never delete early. Sunday's copy goes to `weekly/` only, so "7 daily" is six `daily/` objects plus that one. The pre-deploy local copy is #225's (below). The backup unit sets `TimeoutStartSec=30min` and `KillSignal=SIGKILL`. A oneshot has no start timeout by default, and a hung `rclone` would stay `activating` and skip later nights. The signal is SIGKILL, not SIGTERM: SIGTERM can close `backup.sh`'s pipe while `rclone` is still running, and `rclone` would then store a truncated object that the bucket lock keeps. The deploy unit stays on SIGTERM, because `deploy.sh` traps it so a timed-out deploy still rolls the app back.
  - `age` (1.2.1) and `rclone` (1.60.1) come from Debian 13's archive through cloud-init, so unattended-upgrades patches them like Docker. rclone's `r2` remote is configured by `RCLONE_CONFIG_R2_*` in `/opt/ai-trainer/backup.env` alone, with `NO_CHECK_BUCKET=true` because the token can't create buckets. Also `NO_HEAD=true` (#227, found on the real bucket): the bucket locks make R2 version every object, rclone 1.60 checks each upload with `HEAD …?versionId=`, and R2 answers that with `501 Not Implemented`, so every upload reported failure although it had landed. `make backup-check` uploads to a local folder and can't catch it.
  - R2 has no write-only token. The server's bucket-scoped "Object Read & Write" token can delete, and only the lock refuses it. Changing a lock needs an admin token, which never goes to the server.
  - The dump never touches disk: `docker exec … pg_dump | age` writes to a FIFO that `rclone rcat` reads. A truncated object would sit under the lock for its whole window, so when the dump or age fails, rclone is killed while the FIFO is still open. It never sees EOF, never completes the PUT or the multipart upload, and the abandoned parts expire through `--abort-multipart-days 1`.
  - When the pipeline fails, a successful `kill` doesn't prove rclone was still running. bash may not have reaped an rclone that already failed and broke the pipe under age, and killing a zombie succeeds. The script sends `SIGKILL` and reads rclone's status: 137 means the dump failed, and anything else means the upload did. Testing liveness with `kill` misreported 28 of 30 mid-stream upload failures on Linux.
  - rclone's stderr is passed through a filter that replaces the value of any environment variable named like a key, secret, token or password with `***`. It works line by line: if rclone hex-dumps an error body it can't parse, an echoed key is split across lines and gets through. R2 doesn't echo credentials.
  - `make backup-check` runs the real round trip in a `debian:trixie` container with the host's tool versions and the Docker socket mounted, against the `make start` database: a throwaway key, a local directory as the bucket, then `restore-drill.sh` on that backup (#224).
- **The restore drill (#224):**
  - **The backup-time counts are a manifest written by `backup.sh`** (picked at #224's plan gate). It sits beside each dump as `<same name>.counts.age`: one `table count` line per `public` table, encrypted to the same recipient. A psql session opens `REPEATABLE READ READ ONLY`, runs `pg_export_snapshot()` and counts every table in one statement (a `query_to_xml` count per table). It keeps that transaction open while `pg_dump --snapshot=<id>` dumps the same snapshot ([pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html), [snapshot synchronization](https://www.postgresql.org/docs/18/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION)), so the purge or an athlete writing during the dump can't cause a false mismatch. If the count fails, nothing is uploaded. The manifest is uploaded after the dump, so it never stands for a missing dump. If its upload fails, the unit fails and the drill refuses that backup. The prefix locks and lifecycle rules cover it. It isn't plaintext in R2: table sizes say how many athletes and sessions there are.
  - The psql session runs over two FIFOs, not `coproc`. The scripts' tests run on macOS's bash 3.2, which has none, and the drill itself runs there on the owner's laptop. The four statements go out in one write from a subshell that ignores `SIGPIPE`, so a psql that already died (a stopped database) shows up as EOF on read. Without that, the write would kill the script silently.
  - **The drill runs on the owner's laptop**, the only place the private key exists. It uses a separate, bucket-scoped **Object Read only** R2 token (`deploy/restore.env.example`), never the server's read-write one. The latest backup is the newest stamp across `daily/`, `weekly/` and `predeploy/`; a path argument picks another one. The counts are fetched and decrypted first, so a backup without them fails before anything starts.
  - **The throwaway container** runs `compose.prod.yaml`'s `db` image by digest, and a test keeps the two the same. It has `--network none` and `--tmpfs /var/lib/postgresql`. The decrypted stream goes `rclone cat | age --decrypt | docker exec -i … pg_restore --single-transaction --no-owner`, so the plaintext reaches only the container's memory. `--single-transaction` implies `--exit-on-error` ([pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html)). The `EXIT` trap runs `docker rm -f -v` after a success, a failure or an INT/TERM. The restore runs in the background under `wait`, so a signal runs the trap at once instead of after the restore (bash defers traps until a foreground job ends). In a non-interactive shell, background jobs ignore SIGINT, so after Ctrl-C the local `rclone | age | docker exec` keeps running briefly. When `docker rm -f` removes its container, the `docker exec -i` client exits at once (checked with OrbStack on 2026-10-24). Then age dies of SIGPIPE on its next write, and rclone after it. The plaintext only ever passes through age's memory.
  - **Readiness is `pg_isready -h 127.0.0.1`.** On first start the image runs a temporary server for its init scripts that listens on the Unix socket only. A socket check can pass then and race the restart.
  - With the remote configured by `RCLONE_CONFIG_R2_*` alone, rclone prints `NOTICE: Config file … not found`. `--config ""` silences it, but it would also ignore an owner's own `rclone.conf`, so the drill leaves it.
  - Checked on 2026-10-24 with `make backup-check` against the dev database: 14 tables, every count matched, and no drill container was left behind.
- **The purge timer (#226):** `ai-trainer-purge.timer` fires at 02:00 UTC, with up to 15 minutes of random delay, so it always starts before the 02:30 backup and that night's dump already leaves out the expired conversations. The service runs `docker exec ai-trainer-prod-app-1 ai-trainer purge-conversations`, the app container's own `.env` and network included. `docker exec` exits non-zero when the container is stopped, missing, restarting or paused, which fails the unit. A oneshot has no start timeout by default, so `TimeoutStartSec=15min` makes a stuck run fail instead of staying `activating` and swallowing the next days' runs. A catch-up run at boot (`Persistent=true`) can fire before Docker has restarted the app container. That run fails even though nothing is wrong, and the next day's run clears it. The CLI prints only `purge: result=ok deleted=<count>`, the unit's `ExecStopPost=` prints `purge: result=failed` when the run fails, and `SyslogIdentifier=ai-trainer-purge` labels those lines in the journal (ADR-0030, #274).
- **The deploy poller (#225),** amended 2026-10-24 at the owner's direction (plan gate):
  - **gh comes from GitHub CLI's apt repository.** Debian 13 ships gh 2.46.0 ([packages.debian.org](https://packages.debian.org/trixie/gh)), with no backport, and `gh attestation` first shipped in 2.49.0 ([release notes](https://github.com/cli/cli/releases/tag/v2.49.0)). `deploy/cloud-init.yaml` adds `https://cli.github.com/packages` with GitHub CLI's current signing key committed inline. Its fingerprint, `7F38BBB59D064DBCB3D84D725612B36462313325`, matches cli/cli's `docs/install_linux.md`, and `tests/unit/test_cloud_init.py` checks it. The older published key, `2C61…6059`, expired on 2026-09-05 and is left out. unattended-upgrades follows only Debian's origins by default, so `52ai-trainer-unattended-upgrades` adds `origin=gh,label=gh` (the repository's Release fields). gh then upgrades like Docker, to each new release rather than to security fixes only. Checked in `debian:trixie` on 2026-10-24: gh 2.103.2 installed, with `--signer-workflow`, `--deny-self-hosted-runners` and `--bundle-from-oci`, and `unattended-upgrade --dry-run` listed the origin.
  - **The token is a systemd credential.** It's root-owned, mode 0600, at `/opt/ai-trainer/github-token`. `LoadCredential=` hands the service a private copy under `$CREDENTIALS_DIRECTORY`, and `deploy.sh` reads it into `GH_TOKEN` for each `gh` call only. It's never exported, so `docker`, `curl` and `backup.sh` never see it. `deploy.env` holds only `SITE_HOST`. The unit also reads `backup.env` for the pre-deploy backup.
  - **Host files are installed from the laptop.** `deploy/install.sh <ip>` copies `compose.prod.yaml`, the Caddyfile, `deploy.sh`, `backup.sh` and the six unit files, then reloads systemd. The poller changes only the app image. Those files are inside the public image and must not be applied from it. The hook denies agents running `install.sh`, the same way it denies `ssh` and `scp`. Two bugs showed only on the real host (#227): the remote staging directory used `mktemp -m`, which GNU mktemp lacks (`-d` already makes it 0700), and a comment with backticks inside the unquoted heredoc ran on the laptop as a command.
  - **Only the newest bot deployment counts.** The poller drops every deployment whose creator isn't `github-actions[bot]` (type `Bot`), takes the newest of the rest, and acts only when that one's latest status is `queued` and was posted by `github-actions[bot]` too. Anyone with Deployments write, the server's own token included, could otherwise re-queue a failed release. An older deployment still `queued` is never deployed, so an old approval can't take production back to an older release.
  - **The creator alone doesn't prove the owner approved.** Every workflow run's `GITHUB_TOKEN` creates deployments as `github-actions[bot]`. Environment protection applies to jobs that name the environment, not to a REST `POST /deployments`. So a branch push of an edited `release.yml` could build, attest and post a `production` deployment. `--signer-workflow` matches the workflow's path at any ref. Two checks close this:
    - the deployment's `ref` must be a release tag (`^v[0-9][0-9A-Za-z.+-]*$`);
    - verification adds `--source-ref refs/tags/<ref>`, so a branch run's attestation, which carries `refs/heads/…`, fails.

    Only admins can create a `v*` tag (the tag ruleset). A push of any branch runs the workflow files in that commit, and an explicit `permissions:` block can request `deployments: write` even when the repository default is read-only contents. `tests/unit/test_repo_hardening.py` fails CI unless the only such job is `release.yml`'s `release` job. That does not stop the workflow on the push that introduces it. The agent token has no `workflow` scope, so it cannot push that change (`deploy/github-hardening.md`). This came from #225's skeptic review.
  - **Verification is retried.** A `failure` status is final, so a GitHub or GHCR blip would end an approved release. The verify runs up to three times, 10 s apart. The status then carries gh's last error line, cut to the API's 140-character limit, so it says why.
  - **`/health` is checked through Cloudflare.** Caddy refuses any client without the AOP certificate, so the host can't reach it locally. `deploy.sh` polls `https://$SITE_HOST/health`, 30 times, 3 s apart. `/health` answers 200 even when Postgres is unreachable, with `{"database":"unavailable"}`, so the poller requires `"database":"ok"` in the body, not only a 2xx. A Cloudflare outage during a deploy causes a rollback, which is safe. The image's own `HEALTHCHECK` (`scripts/healthcheck.py`) still accepts any 2xx.
  - **Every `--wait` is bounded.** A container that crash-loops goes back to `starting` on each restart, so `up --wait` alone would wait until the unit's 30-minute timeout. Each `up` passes `--wait-timeout 120`, which Compose 2.26.1 on trixie supports (checked 2026-10-24).
  - **Rollback skips `migrate`.** The order is: `compose up -d --wait db`, `backup.sh predeploy`, `compose run --rm migrate`, then `compose up -d --wait --no-deps app caddy`. The rollback is `compose up -d --wait --no-deps app` with the digest from `/opt/ai-trainer/app-image`, the last one that passed. `--no-deps` matters: without it, `app`'s `depends_on` re-runs `migrate` with the previous image, whose alembic doesn't know the new head and fails. A first deploy has no previous digest, and its failure says so.
  - **The local pre-deploy copy** is the same age-encrypted stream, teed to `/opt/ai-trainer/backups/predeploy/` and renamed into place only once the upload has succeeded too. The newest three are kept. No plaintext dump touches the disk.
  - **One run at a time.** systemd never starts a oneshot that is still running, and `flock -n` on `/opt/ai-trainer/.deploy.lock` covers a run started by hand. When `TimeoutStartSec=30min` stops a run, systemd sends SIGTERM to the whole control group. The script's EXIT trap then reports `failure`, because only a `queued` deployment is ever picked up again, so one left `in_progress` would sit there unreported. If the new app had already been started, the trap first starts the previous digest again, within systemd's default 90 s stop timeout. The timer has `AccuracySec=10s`: systemd's default accuracy of one minute would stretch the two-minute interval.
  - **Nothing puts the token in the journal.** `gh` gets `GH_DEBUG` blanked on every call, so a debug setting left in an env file can't log its HTTP traffic.
  - **Cloudflare must let the health check through.** The poll comes from a Hetzner address with curl's user agent. Bot Fight Mode, or a WAF rule that challenges `/health`, would fail every deploy into a rollback. Leave both off for `/health`.
  - **Agents never run it.** `guard_release.py` denies running `deploy.sh` (directly, through a wrapper, or as a shell's script; `bash -n` only parses it) and `systemctl start|restart ai-trainer-deploy*`.
- **Deploy audit follow-ups,** amended 2026-10-24 at the owner's direction, after a read-only audit of `deploy/`:
  - The Decision's ingress line now sets `mode require_and_verify` explicitly instead of relying on Caddy's default for a configured trust pool. `tests/unit/test_compose_prod.py` requires that to be the Caddyfile's only `mode` line.
  - The firewall and AOP are independent only against scanners. Anyone holding the AOP client leaf (with its key) or the CA key can upload it to their own Cloudflare zone and proxy that zone at the origin, which passes both checks. `deploy/RUNBOOK.md` section 5 deletes the leaf key once Cloudflare has it and keeps the CA key wrapped offline.
  - The backup unit's timeout and kill signal, `deploy/install.sh`, and the `deployments: write` test are recorded under *`deploy/backup.sh` gotchas* and *The deploy poller* above.
- ~~**Observability:** an external uptime check and ADR-0018's OTLP export (Grafana Cloud) are recommended at go-live, but not required.~~ Superseded by [ADR-0030](0030-production-observability-on-grafana-cloud.md) (2026-10-25): telemetry, an external `/health` check and alerts are required for go-live.
- Amended 2026-10-27 at the project owner's direction (#227): the first deploy waits for M1o as well as M1a and M1. PRD 0008 adds AI onboarding (M1o) to the first release; the constraint in Context predates it and stays as recorded.
