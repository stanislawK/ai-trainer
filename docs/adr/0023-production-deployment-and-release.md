# ADR 0023 — Production deployment and release

| | |
|---|---|
| Status | Proposed |
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
- **Hook:** the denying hook ships with the GitHub-hardening ticket, before `release.yml` exists.
- ⚠ **Verify at the deploy-poller ticket:**
  - That a fine-grained token's "Deployments: read and write" permission covers listing deployments and creating deployment statuses. The REST docs name only the classic `repo_deployment` scope ([GitHub REST](https://docs.github.com/en/rest/deployments/statuses)).
  - If it doesn't, use a GitHub App installation token scoped to the repo.
- ⚠ **Verify at the hardening ticket:**
  - The ruleset API's field for "bypass for pull requests only". Third-party docs suggest a `bypass_mode` value, which isn't confirmed.
  - That an "always" bypass isn't needed for a solo owner to merge their own PR with 1 required approval. If it is, drop the required approval count to 0, keep required checks, and keep bypass "for pull requests only".
- ⚠ **Verify at provisioning:** CX23 stock and the price at order time.
- **Observability:** an external uptime check and ADR-0018's OTLP export (Grafana Cloud) are recommended at go-live, but not required.
