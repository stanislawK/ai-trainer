# Production host runbook (owner only)

Source: ADR-0023 (*Host*, *Ingress and TLS*, ⚠ *Verify at provisioning*), ADR-0024 invariant 5, PRD 0005 G12. Ticket #222.

Agents never reach the host: the `guard_release.py` hook denies `ssh`, `scp` and `deploy/install.sh`, and ADR-0024 invariant 5 forbids it. The owner runs every step here from their laptop. The only file the server needs from this repo up front is [`cloud-init.yaml`](cloud-init.yaml), which `make cloud-init-check` validates. Host files after that — `compose.prod.yaml`, the Caddyfile, `deploy.sh`, `backup.sh` and the six systemd units — go over with [`install.sh`](install.sh).

Placeholders used below: `<domain>` is the host of `PUBLIC_BASE_URL` (e.g. `trainer.example.com`), `<ip>` the server's public IPv4, `<my-ip>` your own public address (`curl -4 https://ifconfig.me`).

Tools on the laptop: `hcloud` ([Hetzner CLI](https://github.com/hetznercloud/cli)) with a project context (`hcloud context create ai-trainer`), `curl`, `jq`, `openssl`, `nc`.

## 1. Check stock and price

1. In the Cloud Console → *Add server* → an EU location (Falkenstein, Nuremberg or Helsinki) → *Cost-optimized* → **CX23** (x86, 2 vCPU, 4 GB). A greyed-out type is out of stock there; try another EU location before falling back.
2. `hcloud server-type describe cx23` shows the price per location. ADR-0023 expects €5.49/month before VAT and IPv4.
3. **Fallback: CAX11** (Arm, €5.99). It needs a `linux/arm64` image, which `release.yml` doesn't build yet (#219 builds amd64 only). Stop and raise a ticket for a multi-arch build before ordering it.
4. Record the type, location and price now, privately (see [Provisioning record](#provisioning-record)). The price is the one shown at order time, not this file's expectation.

## 2. SSH key

Upload your public key once: `hcloud ssh-key create --name owner --public-key-from-file ~/.ssh/id_ed25519.pub`. Hetzner hands it to cloud-init as instance metadata, and `cloud-init.yaml` installs it for `deploy` (the default user there). Root gets no usable key.

A hardware-backed key (`sk-ed25519@openssh.com`, or a Secure Enclave key) means a copied laptop disk is not a reusable login. Confirm the console accepts that public-key type when you add it. `deploy` is in the `docker` group, so this key is root on the host.

## 3. Cloud Firewall

The firewall sits outside the VM. Docker-published ports bypass a host firewall such as ufw, so this is the control that counts (ADR-0023).

```sh
cf=$( (curl -fsS https://www.cloudflare.com/ips-v4; echo; curl -fsS https://www.cloudflare.com/ips-v6) | grep . )
jq -n --arg me "<my-ip>/32" --arg cf "$cf" '[
  {direction: "in", protocol: "tcp", port: "443", source_ips: ($cf | split("\n")),
   description: "HTTPS from Cloudflare only"},
  {direction: "in", protocol: "tcp", port: "22", source_ips: [$me],
   description: "SSH from the owner only"}
]' > /tmp/ai-trainer-fw.json
hcloud firewall create --name ai-trainer --rules-file /tmp/ai-trainer-fw.json
```

No other inbound rule: ICMP, 80 and everything else are dropped. Outbound stays open (apt, GHCR, OpenRouter, R2).

When your address changes, or Cloudflare publishes new ranges ([cloudflare.com/ips](https://www.cloudflare.com/ips/)), rebuild the file the same way and run `hcloud firewall replace-rules ai-trainer --rules-file /tmp/ai-trainer-fw.json`.

## 4. Create the server

```sh
hcloud server create --name ai-trainer --type cx23 --image debian-13 --location <location> \
  --ssh-key owner --firewall ai-trainer --enable-backup \
  --user-data-from-file deploy/cloud-init.yaml
```

- `--enable-backup` turns Hetzner's own backups on (+20% of the server price). A restore is the whole disk: `.env`, `db.env`, `backup.env`, `github-token`, `origin-key.pem` and the database. It is not a second age backup. The age private key and `aop-ca-key.pem` are not on the server. The offsite age-encrypted dump is [section 8](#8-backups-to-r2) (ADR-0023 *Backups*). The Hetzner account has 2FA ([github-hardening.md](github-hardening.md)).
- cloud-init upgrades the packages, installs Docker and unattended-upgrades, then reboots once. Allow about five minutes.
- If `debian-13` isn't found, `hcloud image list --type system` names the current Debian 13 image.
- Keep the IPv4 out of this repo: it's public, and the origin address is better left unpublished even behind the firewall.

Then log in once to see that cloud-init finished cleanly:

```sh
ssh deploy@<ip> 'cloud-init status --long; docker compose version; ls -ld /opt/ai-trainer /opt/ai-trainer/certs'
```

If key login fails, nothing else gets in: `deploy` has no password and sshd admits no one else. Don't change the firewall or sshd. Delete and recreate the server (`hcloud server delete ai-trainer`, then this step again with `--ssh-key owner`), or inspect it from Hetzner's rescue system (`hcloud server enable-rescue --ssh-key owner ai-trainer`, then `hcloud server reboot ai-trainer`).

Expect `status: done`, `Docker Compose version 2.26.1-4` (or newer), `drwxr-x--- deploy deploy /opt/ai-trainer` and `drwx------ root root /opt/ai-trainer/certs`.

## 5. Cloudflare

All in the Cloudflare dashboard for the zone.

1. **DNS:** an `A` record for `<domain>` → `<ip>`, **Proxied** (orange cloud). No unproxied record may point at the server; it would leak the origin address.
2. **SSL/TLS → Overview:** encryption mode **Full (strict)**.
3. **Origin certificate:** *SSL/TLS → Origin Server → Create Certificate*, RSA, hostname `<domain>`, 15 years. Save the certificate as `origin.pem` and the key as `origin-key.pem` on your laptop. Cloudflare shows the key only once.
4. **Zone-level Authenticated Origin Pulls** with a CA you own ([Cloudflare docs](https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/set-up/zone-level/)). This is stricter than global AOP, whose certificate every Cloudflare account shares. Make the CA and a client leaf on your laptop:

   ```sh
   openssl req -x509 -newkey rsa:4096 -nodes -days 3650 -subj "/CN=ai-trainer AOP CA" \
     -keyout aop-ca-key.pem -out aop-ca.pem
   openssl req -newkey rsa:2048 -nodes -subj "/CN=ai-trainer AOP client" \
     -keyout aop-client-key.pem -out aop-client.csr
   openssl x509 -req -in aop-client.csr -CA aop-ca.pem -CAkey aop-ca-key.pem -CAcreateserial \
     -days 825 -extfile <(printf 'extendedKeyUsage=clientAuth\n') -out aop-client.pem
   ```

   - *SSL/TLS → Origin Server → Authenticated Origin Pulls → Zone-level → Upload certificate*: paste `aop-client.pem` and `aop-client-key.pem` (the leaf, not the CA; Cloudflare rejects a CA).
   - Switch **Zone-level** to **On**. Nothing serves on the origin yet; from its first start Caddy requires this client certificate (`deploy/Caddyfile`).
   - Delete `aop-client-key.pem`, `aop-client.pem` and `aop-client.csr` from the laptop once the upload has succeeded. Cloudflare keeps the leaf. A copy of that key, uploaded to any other Cloudflare zone and proxied at this origin, passes both checks: the firewall allows every Cloudflare address, and Caddy trusts the CA rather than this zone. The leaf stays valid for 825 days.
   - Keep `aop-ca-key.pem` offline, like the age key. Only `aop-ca.pem` goes to the server. The CA key can mint a new leaf, including after this one expires, and that leaf takes the same path. `openssl -nodes` writes the key in the clear; keep it in a password manager, or wrap the file with `age -p`, and unwrap it only to mint a leaf.
   - Renewing before the leaf expires: run the two `openssl` commands that make the leaf again (the CA files stay), upload the new pair, and delete the new leaf key. That does not revoke a leaf that has already been copied. Replacing `aop-ca.pem` on the server, and uploading a leaf from the new CA, does.
   - Optional: *Notifications* → "Zone-level Authenticated Origin Pulls Certificate Expiration Alert".

## 6. Server files

Copy the certificates and write the env files. Caddy runs with every capability dropped, so `certs/` and its files belong to root (ADR-0023, #221 gotchas).

```sh
scp origin.pem origin-key.pem aop-ca.pem deploy@<ip>:/tmp/
ssh deploy@<ip> 'sudo install -o root -g root -m 0600 /tmp/origin.pem /tmp/origin-key.pem /tmp/aop-ca.pem /opt/ai-trainer/certs/ && rm /tmp/origin*.pem /tmp/aop-ca.pem'
```

The app's `.env` comes from `.env.example`, filled with the production values: the production Google OAuth client, `PUBLIC_BASE_URL=https://<domain>`, and `DATABASE_URL` with a fresh random password. The database password goes to `db.env` as well, which `compose.prod.yaml` reads for the `db` container.

Log in with `ssh deploy@<ip>`, then on the server:

```sh
umask 077
openssl rand -hex 24                     # the database password; use it in both files
$EDITOR /opt/ai-trainer/.env             # from .env.example, production values
$EDITOR /opt/ai-trainer/db.env           # POSTGRES_PASSWORD=<that password>
chmod 0600 /opt/ai-trainer/.env /opt/ai-trainer/db.env
ls -l /opt/ai-trainer                    # -rw------- deploy deploy .env and db.env
```

The deploy poller ([section 10](#10-deploy-poller)) starts the stack with the first release, which waits for M1a and M1 (ADR-0023).

## 7. Checks (ticket #222 acceptance)

Run from your laptop unless noted.

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Schema | `make cloud-init-check` | `Valid schema deploy/cloud-init.yaml` |
| 2a | Key login works | `ssh deploy@<ip> true; echo $?` | `0` |
| 2b | Password refused | `ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive deploy@<ip>` | `Permission denied (publickey).` with no password prompt |
| 2c | Root refused | `ssh root@<ip>` | `Permission denied (publickey).` |
| 2d | Effective sshd config | `ssh deploy@<ip> 'sudo sshd -T' \| grep -Ei '^(permitrootlogin\|passwordauthentication\|kbdinteractiveauthentication\|authenticationmethods\|allowusers) '` | `permitrootlogin no`, `passwordauthentication no`, `kbdinteractiveauthentication no`, `authenticationmethods publickey`, `allowusers deploy` |
| 3 | unattended-upgrades | `ssh deploy@<ip> 'systemctl is-active unattended-upgrades apt-daily-upgrade.timer; apt-config dump APT::Periodic::Unattended-Upgrade'` | `active` twice (the service, and the timer that actually runs the upgrades) and `APT::Periodic::Unattended-Upgrade "1";` |
| 4 | 443 only through Cloudflare | `curl -sk -m 10 -o /dev/null https://<ip>; echo $?` | `28` (timeout: the firewall drops it, there's no TLS refusal) |
| 5 | 22 only from you | from another network (a phone hotspot, or any machine that isn't `<my-ip>`): `nc -vz -w 10 <ip> 22` | times out |
| 4b, 5b | Same over IPv6 | if your network has IPv6: checks 4 and 5 against `https://[<ipv6>]` and `nc -6 -vz -w 10 <ipv6> 22` (the server's address from `hcloud server ip -6 ai-trainer`) | both time out; 22 over IPv6 is closed even to you, since the rule admits only `<my-ip>/32` |
| 6 | Recorded | your private record | every field below filled in |

## 8. Backups to R2

Source: ADR-0023 *Backups*, PRD 0005 G13. Ticket #223.

Every night [`backup.sh`](backup.sh) streams `pg_dump -Fc` through age, encrypted to your public key, into a Cloudflare R2 bucket. The dump never touches the server's disk. The private key stays offline with you, so neither the server nor R2 can read a backup. The deploy poller runs `backup.sh predeploy` before every migration (ADR-0023 invariant 5).

Each backup is two objects: `ai-trainer-<UTC time>.dump.age` and, uploaded after it, `ai-trainer-<UTC time>.counts.age`. The second holds every table's row count, taken in the same database snapshot as the dump and encrypted the same way. The restore drill ([section 8.6](#86-restore-drill-laptop)) checks a restored dump against those counts. Each prefix's lock and lifecycle rule cover both objects.

| Prefix | Written by | Locked and kept for |
|---|---|---|
| `daily/` | the nightly timer, Monday to Saturday (UTC) | 7 days |
| `weekly/` | the nightly timer on Sunday (UTC) | 28 days |
| `predeploy/` | `deploy.sh`, before `migrate` | 7 days |

Sunday's backup goes to `weekly/` only, so `daily/` holds at most six backups: with Sunday's, that's seven nightly restore points in any week.

A bucket lock rule overrides a lifecycle rule ([R2 docs](https://developers.cloudflare.com/r2/buckets/bucket-locks/)), so each prefix gets a lock and an expiry of the same age. R2 has no write-only token: the server's token can delete objects, and only the lock stops it inside the window. Changing a lock rule needs an admin token, which never goes to the server.

### 8.1 Your age key (laptop)

```sh
brew install age
age-keygen -o ai-trainer-backup.key      # prints "Public key: age1…"
```

- `age-keygen` writes a plaintext file. Keep `ai-trainer-backup.key` in a password manager and one offline copy, and decrypt it only for the restore drill. `age -p` encrypts a payload to a passphrase; it does not lock this identity file. To put a passphrase on the file itself, wrap it (`age -p -o ai-trainer-backup.key.age ai-trainer-backup.key`, then delete the plaintext). `age-plugin-yubikey` is the hardware version of the same idea. Without the key, no backup can be restored. The drill ([section 8.6](#86-restore-drill-laptop)) is the only thing that reads it, on your laptop.
- The public key (`age1…`) goes to the server as `BACKUP_AGE_RECIPIENT`. It isn't secret.

### 8.2 Bucket, locks and lifecycle (laptop)

With [Wrangler](https://developers.cloudflare.com/r2/reference/wrangler-commands/) logged in to your account (`npx wrangler login`):

```sh
b=ai-trainer-backups
npx wrangler r2 bucket create $b --location weur
npx wrangler r2 bucket lock add $b daily daily/ --retention-days 7 -y
npx wrangler r2 bucket lock add $b predeploy predeploy/ --retention-days 7 -y
npx wrangler r2 bucket lock add $b weekly weekly/ --retention-days 28 -y
npx wrangler r2 bucket lifecycle add $b daily daily/ --expire-days 7 --abort-multipart-days 1 -y
npx wrangler r2 bucket lifecycle add $b predeploy predeploy/ --expire-days 7 --abort-multipart-days 1 -y
npx wrangler r2 bucket lifecycle add $b weekly weekly/ --expire-days 28 --abort-multipart-days 1 -y
npx wrangler r2 bucket lock list $b
npx wrangler r2 bucket lifecycle list $b
```

Expect the three lock rules and the three lifecycle rules listed with those ages. The bucket's default lifecycle rule (abort incomplete multipart uploads after 7 days) may be listed too; leave it.

### 8.3 The server's token (dashboard)

*R2 → Manage R2 API tokens → Create API token*:

- Permission **Object Read & Write**.
- **Apply to specific buckets only:** `ai-trainer-backups`, nothing else.
- No expiry, or a long one with a calendar reminder.

Copy the **Access Key ID**, the **Secret Access Key** (shown once) and the S3 endpoint `https://<account-id>.r2.cloudflarestorage.com`.

### 8.4 Install on the server

From your laptop, in this repo, copy the host files. This also installs the purge and deploy units; their timers are enabled in [section 9](#9-conversation-purge) and [section 10](#10-deploy-poller), after their own settings exist. Run it again whenever `compose.prod.yaml`, the Caddyfile, `deploy.sh`, `backup.sh` or a unit file changes, and do that before the release that needs the new files. The script waits for the deploy poller's lock, so it does not replace files during a deploy. If Caddy is already running, the script recreates it and passes `APP_IMAGE` from `/opt/ai-trainer/app-image`: Compose requires that variable for the whole file, not only for the app service. The Caddyfile is mounted as one file, so replacing it does not change what the running container has open. A failed run removes its staging directory under `/tmp`; run the script again to finish a copy that stopped halfway.

```sh
deploy/install.sh <ip>
```

Then `ssh deploy@<ip>` and on the server:

```sh
sudo apt-get install -y age rclone       # only on a host provisioned before #223; cloud-init installs them now
umask 077
$EDITOR /opt/ai-trainer/backup.env       # from deploy/backup.env.example: ACCOUNT_ID in the endpoint, the token, your age1… key
chmod 0600 /opt/ai-trainer/backup.env
sudo systemctl enable --now ai-trainer-backup.timer
```

- Write `backup.env` values unquoted, one `KEY=value` per line: systemd's `EnvironmentFile=` and the shell checks below both read it.
- `backup.sh` dumps from the `ai-trainer-prod-db-1` container. Until the first deploy starts the stack, start Postgres alone for the checks below. Compose reads `/opt/ai-trainer/.env` and `db.env` even then, so [section 6](#6-server-files) comes first. `install.sh` has already copied `compose.prod.yaml`. Then `cd /opt/ai-trainer && APP_IMAGE=unused SITE_HOST=unused docker compose -f compose.prod.yaml up -d db`. That database is empty, and an empty dump is still a valid backup.

### 8.5 Checks (ticket #223 acceptance)

Run on the server (`ssh deploy@<ip>`). `r2env` loads the token into the current shell only: `r2env() { set -a; . /opt/ai-trainer/backup.env; set +a; }`.

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Timer | `systemctl list-timers ai-trainer-backup.timer` | one row, `NEXT` between 02:30 and 02:45 UTC |
| 2 | A run by hand | `sudo systemctl start ai-trainer-backup.service; journalctl -u ai-trainer-backup.service -n 5 --no-pager` | `backup: uploaded r2:ai-trainer-backups/daily/ai-trainer-<UTC time>.dump.age` (`weekly/` on a Sunday), and `systemctl is-failed ai-trainer-backup.service` prints `inactive` |
| 3 | In the bucket | `r2env; rclone lsl "$BACKUP_DEST"` | the object from check 2: about 1 kB for the empty pre-go-live database, more once athletes use it |
| 4 | Next morning | `r2env; rclone lsl "$BACKUP_DEST"` | a second object, stamped between 02:30 and 02:45 UTC |
| 5 | Lock refuses delete | `r2env; rclone deletefile "$BACKUP_DEST/<path from check 3>"; echo $?` | an R2 error and a non-zero exit; `rclone lsl` still lists the object |
| 6 | A failure is visible | before go-live only: `docker stop ai-trainer-prod-db-1; sudo systemctl start ai-trainer-backup.service; systemctl --failed; docker start ai-trainer-prod-db-1` | `ai-trainer-backup.service` listed as failed; `journalctl -u ai-trainer-backup.service` says why, and holds no token |

A check-6 run leaves no object behind: `backup.sh` stops the upload before it completes whenever the dump or the encryption fails. Run `sudo systemctl reset-failed ai-trainer-backup.service` afterwards.

### 8.6 Restore drill (laptop)

Source: ADR-0023 *Backups*, PRD 0005 G13. Ticket #224.

A backup counts only once it has been restored. [`restore-drill.sh`](restore-drill.sh) fetches the latest backup, the newest stamp in `daily/`, `weekly/` or `predeploy/`, and decrypts it with your offline key on this laptop. It restores it into a throwaway Postgres container: the production image by digest, with no network and its data in memory. Then it compares every table's row count with the backup's `.counts.age`. It exits 0 only when every count matches, and it removes the container on every exit. The decrypted dump never touches the disk.

**When:** at go-live, the morning after the first nightly backup. Then on the first of each month (set a calendar reminder), and after any change to `backup.sh` or to the `db` image in `compose.prod.yaml`. In the same sitting: rebuild the Cloudflare firewall rules ([section 3](#3-cloud-firewall)), confirm the AOP leaf has not expired and renew it if it is close ([section 5](#5-cloudflare)), and re-run check 5 from [section 8.5](#85-checks-ticket-223-acceptance) so the bucket lock still refuses a delete.

**Once:**

1. `brew install age rclone`, with Docker (OrbStack or Docker Desktop) running. The first run pulls the Postgres image.
2. A read-only token. *R2 → Manage R2 API tokens → Create API token*: permission **Object Read only**, **Apply to specific buckets only:** `ai-trainer-backups`. Don't reuse the server's token: this laptop never needs to write or delete.
3. The settings file, from this repo:

   ```sh
   mkdir -p ~/.config/ai-trainer
   (umask 077; cp deploy/restore.env.example ~/.config/ai-trainer/restore.env)
   $EDITOR ~/.config/ai-trainer/restore.env   # ACCOUNT_ID in the endpoint, the read-only token, the path to ai-trainer-backup.key
   ```

**Each drill**, from this repo. The subshell keeps the token out of your shell afterwards:

```sh
(set -a; . ~/.config/ai-trainer/restore.env; set +a; deploy/restore-drill.sh); echo "exit $?"
```

| # | Check | Expected |
|---|---|---|
| 1 | The run above | `restore-drill: <prefix>/ai-trainer-<UTC time>.dump.age` (the newest backup), then one row per table (`backup`, `restored`, `ok`) and `restore-drill: every count matches (<n> tables)`; `exit 0` |
| 2 | Nothing left behind | `docker ps -a --filter name=ai-trainer-restore-drill` prints only its header line |
| 3 | A key that isn't yours fails | `age-keygen -o /tmp/other.key; (set -a; . ~/.config/ai-trainer/restore.env; set +a; RESTORE_AGE_IDENTITY=/tmp/other.key deploy/restore-drill.sh); echo "exit $?"; rm /tmp/other.key` prints `age: error: no identity matched any of the recipients`, `restore-drill: could not fetch or decrypt …` and `exit 1`, and starts no container |

- To drill a specific backup, pass its path, e.g. `deploy/restore-drill.sh weekly/ai-trainer-20261025T023000Z.dump.age`. `rclone lsf -R "$BACKUP_DEST"` lists them.
- rclone prints `NOTICE: Config file … not found - using defaults` because the remote comes from the environment. That's expected.
- **A failed drill** (`MISMATCH` rows, a failed restore or decrypt, exit 1): don't trust that backup. Drill the one before it by path, and open an issue with the output. The output shows table names and counts only, never data.
- After a host or token incident, restore a backup you name from before the incident. The drill's default is the newest stamp, and a future-dated object encrypted to your public key would win that race. The bucket lock is what keeps the older object.
- The local pre-deploy copies (`/opt/ai-trainer/backups/predeploy/`, newest three) are the same age ciphertext, with no `.counts.age` beside them. The private key is not on the host, so they are not a restore you can run there. They are for the case where the disk is up and R2 is down: `scp` the ciphertext to this laptop and decrypt it here. The drill uses the R2 object in `predeploy/`.

## 9. Conversation purge

Source: ADR-0021 *Retention*, ADR-0023 *The server's scheduled jobs*, PRD 0005 B26. Ticket #226.

Every day at 02:00 UTC (up to 15 minutes later, always before the 02:30 backup), `ai-trainer-purge.timer` starts `ai-trainer-purge.service`. The service runs `ai-trainer purge-conversations` inside the app container, `ai-trainer-prod-app-1`. That deletes every conversation whose last message is older than `CONVERSATION_RETENTION_DAYS` (default 14), with its messages. The journal gets one line, the number deleted, and never any content.

### 9.1 Install on the server

[`install.sh`](install.sh) in [section 8.4](#84-install-on-the-server) has already copied both unit files. On the server:

```sh
sudo systemctl enable --now ai-trainer-purge.timer
```

The timer can be enabled before the first deploy. Until the app container exists, each daily run fails and shows in `systemctl --failed`, which is the expected signal. Checks 2 and 3 need the stack running, so run them after the first deploy.

### 9.2 Checks (ticket #226 acceptance)

Run on the server (`ssh deploy@<ip>`).

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Timer | `systemctl list-timers ai-trainer-purge.timer` | one row, `NEXT` between 02:00 and 02:15 UTC |
| 2 | A run by hand | `sudo systemctl start ai-trainer-purge.service; journalctl -u ai-trainer-purge.service -n 5 --no-pager` | a line `ai-trainer-purge[<pid>]: <count>` (`0` while nothing has expired) and no other output; `systemctl is-failed ai-trainer-purge.service` prints `inactive` |
| 3 | A failure is visible | before go-live only: `docker stop ai-trainer-prod-app-1; sudo systemctl start ai-trainer-purge.service; systemctl --failed; docker start ai-trainer-prod-app-1` | `systemctl start` reports the job failed; `ai-trainer-purge.service` is listed as failed; `journalctl -u ai-trainer-purge.service` shows Docker's `is not running` error |

Run `sudo systemctl reset-failed ai-trainer-purge.service` after check 3.

## 10. Deploy poller

Source: ADR-0023 *Pull-based deploy on the server*, invariants 2, 3 and 5; ADR-0024 invariant 4; PRD 0005 G12, G13. Ticket #225.

The server pulls approved releases, so GitHub holds no credential to it. Every two minutes `ai-trainer-deploy.timer` starts [`deploy.sh`](deploy.sh). It looks at the newest `production` deployment that `github-actions[bot]` created, which is the `release` job once you approve it (README "Releasing"). Only when that deployment's latest status is `queued` does it act:

1. It verifies the image's provenance against `release.yml` with `gh attestation verify`. On a failure it reports `failure` and pulls nothing.
2. It reports `in_progress` and pulls the image by digest.
3. It starts `db`, then runs `backup.sh predeploy`: uploaded to `predeploy/` in R2, and the same encrypted file kept in `/opt/ai-trainer/backups/predeploy/` (the newest three).
4. It runs `migrate`, then starts `app` and `caddy` on the new image.
5. It polls `https://<domain>/health` through Cloudflare and Caddy for up to 90 seconds.
6. It reports `success` and records the digest in `/opt/ai-trainer/app-image`. Otherwise it starts the previous digest from that file again and reports `failure` with the reason. The database is never rolled back: migrations are expand-only (ADR-0024 invariant 4).

What it ignores:

- Deployments anyone else created, including your own, and any deployment whose ref isn't a `v*` tag.
- A `queued` status anyone but the `release` job posted. To retry a failed release, tag a new one.
- An older deployment still `queued` once a newer release exists, so an old approval can't take production back.
- A deployment that is `success`, `failure` or `in_progress`.

The first release has no previous digest. If its health check fails, the new app keeps running, the status says there was nothing to roll back to, and you fix it forward.

The health check comes from the server, through Cloudflare, with curl's user agent. Keep Bot Fight Mode and any WAF challenge off for `/health`, or every deploy rolls back.

`/health` answers HTTP 200 when Postgres is down, with `"database":"unavailable"`. An external check, if you add one, has to require `"database":"ok"` in the body. A check that only looks at the status stays green while the database is down. Backup and purge failures stay in the journal; this check is the outage signal they don't send.

A failed release is fixed forward with a new tag. A broken database is restored from a backup; the restore drill ([section 8.6](#86-restore-drill-laptop)) proves the latest one restores. After an incident, pass that drill a path from before the incident rather than taking the newest stamp.

The poller changes only the app image. When `compose.prod.yaml`, `deploy/Caddyfile`, `deploy.sh`, `backup.sh` or a unit file changes, run [`install.sh`](install.sh) ([section 8.4](#84-install-on-the-server)) before the release that needs them. Approving first can start the new image against the old files. Do not copy those files out of the pulled image.

### 10.1 The GitHub token (laptop)

*GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token*:

- **Resource owner:** `stanislawK`. **Repository access:** *Only select repositories* → `ai-trainer`.
- **Repository permissions:** *Deployments* **Read and write**, *Attestations* **Read**. *Metadata* **Read** is added by itself. Nothing else.
- **Expiration:** the longest offered, with a calendar reminder a week before. An expired token fails every run, which `systemctl --failed` shows.

The REST docs list exactly these permissions for listing deployments, posting their statuses and reading attestations ([GitHub docs](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens), read 2026-10-24). That settles ADR-0023's ⚠: no GitHub App is needed.

### 10.2 Install on the server

[`install.sh`](install.sh) in [section 8.4](#84-install-on-the-server) has already copied `deploy.sh`, `compose.prod.yaml`, the Caddyfile and the deploy units. On the server:

```sh
gh --version                             # 2.49 or later; cloud-init installs it from cli.github.com
umask 077
echo "SITE_HOST=<domain>" > /opt/ai-trainer/deploy.env
sudo sh -c 'umask 077; cat > /opt/ai-trainer/github-token'   # paste the token, Enter, then Ctrl-D
sudo ls -l /opt/ai-trainer/github-token  # -rw------- root root
sudo systemctl enable --now ai-trainer-deploy.timer
```

- On a host provisioned before #225, `gh` is Debian's 2.46, which has no `gh attestation`. Add GitHub CLI's repository first, exactly as `deploy/cloud-init.yaml` does: its `apt.sources.github-cli` source and key, and its `52ai-trainer-unattended-upgrades` file. Then `sudo apt-get update && sudo apt-get install -y gh`.
- The token file belongs to root. systemd reads it and hands the service a private copy (`LoadCredential=`), so it's never in an environment variable, and `docker` and `backup.sh` never see it.
- `deploy.sh` reads `/opt/ai-trainer/backup.env` for the pre-deploy backup, so [section 8](#8-backups-to-r2) comes first.

### 10.3 Checks (ticket #225 acceptance)

Run on the server (`ssh deploy@<ip>`) unless noted. Checks 3–5 make a real release, so they wait for go-live (M1a and M1, ADR-0023).

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Timer | `systemctl list-timers ai-trainer-deploy.timer` | one row, `NEXT` at most two minutes away |
| 2 | Idle run | `sudo systemctl start ai-trainer-deploy.service; journalctl -u ai-trainer-deploy.service -n 5 --no-pager; systemctl is-failed ai-trainer-deploy.service` | no output from the run while nothing is queued, then `inactive` |
| 3 | A release (AC 1) | laptop: tag and approve (README "Releasing"), then within five minutes `gh api repos/stanislawK/ai-trainer/deployments/<id>/statuses --jq '.[].state'` | `success`, `in_progress`, `queued`, newest first (the poller relies on that order); on the server `journalctl -u ai-trainer-deploy.service` shows `Verification succeeded!` and `deployment <id> succeeded`, and `cat /opt/ai-trainer/app-image` is that digest |
| 4 | Backup before migrate (AC 5) | `ls -l /opt/ai-trainer/backups/predeploy; set -a; . /opt/ai-trainer/backup.env; set +a; rclone lsl "$BACKUP_DEST/predeploy"` | a `.dump.age` file in both, stamped after the run's `is queued` line and before its `migrate` output |
| 5 | Serves | laptop: `curl -sS https://<domain>/health` | `{"database":"ok"}` |
| 6 | Others' deployments ignored (AC 3) | laptop: `id=$(gh api -X POST repos/stanislawK/ai-trainer/deployments -f ref=main -f environment=production -F auto_merge=false -F 'required_contexts[]' --jq .id); gh api -X POST repos/stanislawK/ai-trainer/deployments/$id/statuses -f state=queued`. Wait three minutes, then `gh api repos/stanislawK/ai-trainer/deployments/$id/statuses --jq '.[0].state'` | still `queued`; the journal shows nothing for it. Then clean up: `gh api -X POST …/deployments/$id/statuses -f state=inactive` and `gh api -X DELETE repos/stanislawK/ai-trainer/deployments/$id` |
| 7 | One run at a time (AC 6) | `flock /opt/ai-trainer/.deploy.lock sleep 60 & sleep 1; sudo systemctl start ai-trainer-deploy.service; journalctl -u ai-trainer-deploy.service -n 1 --no-pager` | `deploy: another deploy is running; nothing to do` |
| 8 | No token in the journal (AC 6) | `journalctl -u ai-trainer-deploy.service --no-pager \| grep -c github_pat_` | `0` |

A failing attestation (AC 2) and a failing health check (AC 4) can't be staged safely on the real host: only `release.yml` creates the deployments the poller acts on, and it attests every image it pushes. `tests/unit/test_deploy_script.py` covers both, and a real one shows as a `failure` status whose description says why.

## Provisioning record

Kept by the owner, privately, outside this repo: it's public, and these are the owner's account details (ADR-0023 ⚠ *Verify at provisioning*). A password manager entry next to the server's other details does. Never commit the values.

Record at order time:

- Ordered on
- Server type
- Location
- Price at order time (€/month, before VAT and IPv4)
- Hetzner backups (on; +20% of the server price)
- IPv4 and IPv6
- Firewall rules last rebuilt
