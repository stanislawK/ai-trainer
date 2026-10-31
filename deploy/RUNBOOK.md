# Production host runbook (owner only)

Source: ADR-0023 (*Host*, *Ingress and TLS*, ⚠ *Verify at provisioning*), ADR-0024 invariant 5, PRD 0005 G12. Ticket #222.

Agents never reach the host: the `guard_release.py` hook denies `ssh`, `scp` and `deploy/install.sh`, and ADR-0024 invariant 5 forbids it. The owner runs every step here from their laptop. The only file the server needs from this repo up front is [`cloud-init.yaml`](cloud-init.yaml), which `make cloud-init-check` validates. Host files after that — `compose.prod.yaml`, the Caddyfile, `deploy.sh`, `backup.sh`, the [`compose.sh`](compose.sh) wrapper and the six systemd units — go over with [`install.sh`](install.sh).

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

### 4.1 Sections 1–4 in the Cloud Console instead (#227)

The production host was made this way. The wizard's defaults silently override what the CLI states explicitly, and cloud-init reads its settings only at first boot, so a wrong field means deleting the server and starting again. Three of the first four attempts failed that way: the Cloud config field left empty, the default Ubuntu image (whose missing `docker-compose` package fails the whole package step), and twice an empty `authorized_keys` for `deploy` with root's absent too, so Hetzner had passed no key at all.

1. **Price (§1):** *Add server* shows each type's price per location; close it without buying.
2. **Key (§2):** *Security → SSH keys → Add SSH key* with `~/.ssh/id_ed25519.pub`. Don't rely on ticking it in the wizard: write the key into the cloud config too, on the laptop. The copy is never committed, and a public key isn't secret:

   ```sh
   key=$(cat ~/.ssh/id_ed25519.pub)
   sed "/^  sudo: /a\\  ssh_authorized_keys: [\"$key\"]" deploy/cloud-init.yaml > /tmp/cloud-init-with-key.yaml
   ```

3. **Firewall (§3):** *Firewalls → Create Firewall*. Inbound exactly two rules: TCP 22 from `<my-ip>/32`, and TCP 443 from every line of `curl -s https://www.cloudflare.com/ips-v4; curl -s https://www.cloudflare.com/ips-v6`. Delete the pre-filled ICMP rule and any `0.0.0.0/0` or `::/0`. No outbound rules. Attach it to the server afterwards if the wizard didn't (*Resources → Apply to resources*); until then every port is open.
4. **Server (§4):** *Add server*. **Image: Debian 13, not the default Ubuntu.** CX23, IPv4 and IPv6, the SSH key ticked, the firewall ticked, Backups on, **Cloud config:** the whole of `/tmp/cloud-init-with-key.yaml`, **Name:** `ai-trainer`, not the generated one. Check the summary says Debian 13 and CX23 before *Create & Buy now*.
5. **When key login fails:** *Rescue → Reset root password*, then log in as root through the console (`>_`), and read `/home/deploy/.ssh/authorized_keys`, `/root/.ssh/` and `cloud-init status --long`. The console's paste drops Shift, so `^ | ' _ :` arrive wrong: paste commands without them, and screenshot the output. Lock root again with `passwd -l root`, or delete the server.
6. **IPv6 (§7, 4b and 5b):** the server's *Overview* shows its IPv6 address.

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

Leave every `OTEL_*` key, `APP_VERSION` and `DEPLOYMENT_ENVIRONMENT` out of the production `.env`. `compose.prod.yaml` sets what the app needs, and the rest keep their defaults: the app sends to Alloy, which holds the only Grafana Cloud credentials ([section 11](#11-telemetry-to-grafana-cloud), ADR-0030 invariant 1), and `deploy.sh` passes the release tag. The one exception is `OTEL_TRACES_SAMPLER_RATIO`, which you may set to fit the free tier (section 11.1).

On the production OpenRouter key, set a credit limit in the OpenRouter dashboard (ADR-0029). The app caps model calls per user and cannot see that limit. It is the backstop if the app's cap is wrong. The key's secret stays in `.env` only.

Log in with `ssh deploy@<ip>`, then on the server:

```sh
umask 077
openssl rand -hex 24                     # the database password; use it in both files
$EDITOR /opt/ai-trainer/.env             # from .env.example, production values
$EDITOR /opt/ai-trainer/db.env           # POSTGRES_PASSWORD=<that password>
chmod 0600 /opt/ai-trainer/.env /opt/ai-trainer/db.env
ls -l /opt/ai-trainer                    # -rw------- deploy deploy .env and db.env
```

The deploy poller ([section 10](#10-deploy-poller)) starts the stack with the first release, which waits for M1a, M1 and M1o (ADR-0023, PRD 0008).

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

The dashboard does the same without Wrangler (#227): *R2 → Create bucket* (location hint Western Europe, storage class Standard), then the bucket's *Settings → Bucket lock rules* and *Object lifecycle rules*, one rule per prefix as above. Keep Standard: R2's free tier doesn't cover Infrequent Access, which also bills each object for 30 days. An upload's `X-Amz-Expiration` header shows the expiry R2 applied; check it matches the rule.

The locks make R2 version every object, and R2 answers a `HEAD` with `?versionId=` with `501 Not Implemented`. rclone 1.60 sends one after each upload to check it, so every upload reported failure although it had landed. `RCLONE_CONFIG_R2_NO_HEAD=true` in `backup.env` skips that check (#227).

### 8.3 The server's token (dashboard)

*R2 → Manage R2 API tokens → Create API token*:

- Permission **Object Read & Write**.
- **Apply to specific buckets only:** `ai-trainer-backups`, nothing else.
- No expiry, or a long one with a calendar reminder.

Copy the **Access Key ID**, the **Secret Access Key** (shown once) and the S3 endpoint `https://<account-id>.r2.cloudflarestorage.com`.

### 8.4 Install on the server

From your laptop, in this repo, copy the host files. This also installs the purge and deploy units; their timers are enabled in [section 9](#9-conversation-purge) and [section 10](#10-deploy-poller), after their own settings exist. Run it again whenever `compose.prod.yaml`, the Caddyfile, `deploy.sh`, `backup.sh`, `compose.sh` or a unit file changes, and do that before the release that needs the new files. The script waits for the deploy poller's lock, so it does not replace files during a deploy. If Caddy is already running, the script recreates it and passes `APP_IMAGE` from `/opt/ai-trainer/app-image`: Compose requires that variable for the whole file, not only for the app service. The Caddyfile is mounted as one file, so replacing it does not change what the running container has open. A failed run removes its staging directory under `/tmp`; run the script again to finish a copy that stopped halfway.

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
| 2 | A run by hand | `sudo systemctl start ai-trainer-backup.service; journalctl -u ai-trainer-backup.service -n 5 --no-pager` | `backup: uploaded r2:ai-trainer-backups/daily/ai-trainer-<UTC time>.dump.age` (`weekly/` on a Sunday) and, last, `backup: result=ok kind=nightly`, and `systemctl is-failed ai-trainer-backup.service` prints `inactive` |
| 3 | In the bucket | `r2env; rclone lsl "$BACKUP_DEST"` | the object from check 2: about 1 kB for the empty pre-go-live database, more once athletes use it |
| 4 | Next morning | `r2env; rclone lsl "$BACKUP_DEST"` | a second object, stamped between 02:30 and 02:45 UTC |
| 5 | Lock refuses delete | `r2env; rclone deletefile "$BACKUP_DEST/<path from check 3>"; echo $?` | an R2 error and a non-zero exit; `rclone lsl` still lists the object |
| 6 | A failure is visible | before go-live only: `docker stop ai-trainer-prod-db-1; sudo systemctl start ai-trainer-backup.service; systemctl --failed; docker start ai-trainer-prod-db-1` | `ai-trainer-backup.service` listed as failed; `journalctl -u ai-trainer-backup.service` says why, ends on `backup: result=failed kind=nightly reason=count exit=1`, and holds no token |

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

Every day at 02:00 UTC (up to 15 minutes later, always before the 02:30 backup), `ai-trainer-purge.timer` starts `ai-trainer-purge.service`. The service runs `ai-trainer purge-conversations` inside the app container, `ai-trainer-prod-app-1`. That deletes every conversation whose last message is older than `CONVERSATION_RETENTION_DAYS` (default 14), with its messages. The journal gets one line, `purge: result=ok deleted=<count>`, and never any content. A run that fails, including one with the app container stopped, ends on `purge: result=failed` instead (the unit's `ExecStopPost=`).

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
| 2 | A run by hand | `sudo systemctl start ai-trainer-purge.service; journalctl -u ai-trainer-purge.service -n 5 --no-pager` | a line `ai-trainer-purge[<pid>]: purge: result=ok deleted=<count>` (`deleted=0` while nothing has expired) and no other output; `systemctl is-failed ai-trainer-purge.service` prints `inactive` |
| 3 | A failure is visible | before go-live only: `docker stop ai-trainer-prod-app-1; sudo systemctl start ai-trainer-purge.service; systemctl --failed; docker start ai-trainer-prod-app-1` | `systemctl start` reports the job failed; `ai-trainer-purge.service` is listed as failed; `journalctl -u ai-trainer-purge.service` shows Docker's `is not running` error, then `purge: result=failed` as its last line |

Run `sudo systemctl reset-failed ai-trainer-purge.service` after check 3.

## 10. Deploy poller

Source: ADR-0023 *Pull-based deploy on the server*, invariants 2, 3 and 5; ADR-0024 invariant 4; PRD 0005 G12, G13. Ticket #225.

The server pulls approved releases, so GitHub holds no credential to it. Every two minutes `ai-trainer-deploy.timer` starts [`deploy.sh`](deploy.sh). It looks at the newest `production` deployment that `github-actions[bot]` created, which is the `release` job once you approve it (README "Releasing"). Only when that deployment's latest status is `queued` does it act:

1. It verifies the image's provenance against `release.yml` with `gh attestation verify`. On a failure it reports `failure` and pulls nothing.
2. It reports `in_progress` and pulls the image by digest.
3. It starts `db`, then runs `backup.sh predeploy`: uploaded to `predeploy/` in R2, and the same encrypted file kept in `/opt/ai-trainer/backups/predeploy/` (the newest three).
4. It runs `migrate`, then starts `app` and `caddy` on the new image. Right after the pull it starts `alloy` ([section 11](#11-telemetry-to-grafana-cloud)) without waiting for it; a failure there only prints a warning.
5. It polls the app's `/health` from inside the Caddy container, never through Cloudflare, for up to 90 seconds (ADR-0023, amended 2026-10-28).
6. It reports `success` and records the digest in `/opt/ai-trainer/app-image` and the release tag in `/opt/ai-trainer/app-version`. Compose passes that tag (`RELEASE_TAG`) to the app as `APP_VERSION`, its traces' `service.version` (ADR-0030). Otherwise it starts the previous digest from that file again, with the previous tag, and reports `failure` with the reason. The database is never rolled back: migrations are expand-only (ADR-0024 invariant 4).

What it ignores:

- Deployments anyone else created, including your own, and any deployment whose ref isn't a `v*` tag.
- A `queued` status anyone but the `release` job posted. To retry a failed release, tag a new one.
- An older deployment still `queued` once a newer release exists, so an old approval can't take production back.
- A deployment that is `success`, `failure` or `in_progress`.

The first release has no previous digest. If its health check fails, the new app keeps running, the status says there was nothing to roll back to, and you fix it forward.

The health check never leaves the host, so Bot Fight Mode stays on.

`/health` answers HTTP 200 when Postgres is down, with `"database":"unavailable"`. An external check, if you add one, has to require `"database":"ok"` in the body. A check that only looks at the status stays green while the database is down. Backup and purge failures stay in the journal; this check is the outage signal they don't send.

A failed release is fixed forward with a new tag. A broken database is restored from a backup; the restore drill ([section 8.6](#86-restore-drill-laptop)) proves the latest one restores. After an incident, pass that drill a path from before the incident rather than taking the newest stamp.

The poller changes only the app image. When `compose.prod.yaml`, `deploy/Caddyfile`, `deploy/alloy/config.alloy`, `deploy.sh`, `backup.sh`, `compose.sh` or a unit file changes, run [`install.sh`](install.sh) ([section 8.4](#84-install-on-the-server)) before the release that needs them. Approving first can start the new image against the old files. Do not copy those files out of the pulled image.

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

Run on the server (`ssh deploy@<ip>`) unless noted. Checks 3–5 make a real release, so they wait for go-live (M1a, M1 and M1o; ADR-0023, PRD 0008).

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

A failing attestation (AC 2) and a failing health check (AC 4) can't be staged safely on the real host: only `release.yml` creates the deployments the poller acts on, and it attests every image it pushes. `tests/ops/test_deploy_script.py` covers both, and a real one shows as a `failure` status whose description says why.

## 11. Telemetry to Grafana Cloud

Source: ADR-0030 *Pipeline*, *Secrets*, invariants 1, 2, 5 and 6; PRD 0008 G12. Tickets #272, #273, #277.

The `alloy` service relays these to one Grafana Cloud stack:

- **The app's traces**: OTLP over the internal `telemetry` network to `alloy:4318`.
- **This project's container logs**: read through the Docker socket and labelled by compose service (`app`, `caddy`, `db`, `alloy`), with email-shaped values masked.
- **The scheduled jobs' outcomes** (ticket #274): the host's journal, mounted read-only with `/etc/machine-id`, keeping only the `ai-trainer-backup`, `ai-trainer-purge` and `ai-trainer-deploy` syslog identifiers. Each line is labelled `job` (`backup`, `purge`, `deploy`). A run ends on `<job>: result=ok …` or `<job>: result=failed …`, with no secret and no row data; an idle deploy poll prints nothing. Every other unit's lines stay on the host.
- **Host, Caddy and Postgres metrics** (ticket #273), every 60 s, through one keep list. Series outside it are dropped on the host.
  - **Host:** CPU, load, memory, pressure and disk I/O, plus the root filesystem's size and free space. They come from the host's `/proc`, `/sys` and `/`, mounted read-only.
  - **Caddy:** requests, errors and upstream health, under `server="public"`. Caddy serves them only at `10.222.0.10:9180` on the internal `metrics` network, and only to Alloy's fixed address there, `10.222.0.20`. Anyone else gets 403, and nothing goes through 443.
  - **Postgres:** `pg_up`, connections per database, commits, rollbacks, deadlocks, locks and database size. They are read as `ai_trainer_monitor`, a login role with `pg_monitor` only, which can read no app table, over `backend`.

Alloy publishes no port and is never on `edge`. Only it reads the Grafana Cloud keys in `/opt/ai-trainer/alloy.env`, the one place a Grafana Cloud credential lives: never the app's `.env`, the repo, the image or CI. Without that file Alloy can't build its exporters and restarts in a loop, shipping nothing. Nothing depends on it, so no request, deploy, backup or purge fails over it.

`migrate` runs for a few seconds, usually too short for Alloy to tail. Its output is in the deploy unit's journal (`journalctl -u ai-trainer-deploy.service`), which Alloy ships under `job="deploy"`.

Agents never sign in to Grafana Cloud or call its API. Every step here is yours.

### 11.1 Stack, policy and token (laptop, Grafana Cloud portal)

1. Create the stack, or pick the existing one, in the region nearest the host (EU). The free plan allows one stack. Its settings are in the Cloud Portal (`https://grafana.com/profile/org`), not in the stack's own `https://<stack>.grafana.net`; the region is in the endpoint hostnames, e.g. `…-prod-eu-west-2…`.
2. On the stack's **OpenTelemetry** *Details* page, note the **OTLP endpoint** and the **instance ID**.
3. On the stack's **Loki** *Details* page, note the **URL**, ending in `/loki/api/v1/push`, and the **user**, a number.
4. On the stack's **Prometheus** *Details* page, note the **Remote Write Endpoint**, ending in `/api/prom/push`, and the **user**, a number.
5. **Access Policies** → *Create access policy*:
   - **Realm:** this stack.
   - **Scopes:** `traces:write`, `logs:write` and `metrics:write`, and nothing else. `metrics:write` is for the metrics tickets (#273, #275).
6. *Add token* on that policy and copy it once. Set its expiry, and a calendar reminder a week before. An expired token stops all shipping, which the "No telemetry in 15 min" alert (#277) reports.

### 11.2 Install on the server

[`install.sh`](install.sh) ([section 8.4](#84-install-on-the-server)) copies `compose.prod.yaml` and `deploy/alloy/config.alloy`. If Alloy is running, it recreates it so it reads the new config. On the server:

```sh
umask 077
cat > /opt/ai-trainer/alloy.env   # paste the eight lines below with your values, Enter, then Ctrl-D
ls -l /opt/ai-trainer/alloy.env   # -rw------- deploy deploy
/opt/ai-trainer/compose.sh config --quiet && echo ok   # must print ok; see below
/opt/ai-trainer/compose.sh up -d --no-deps --force-recreate alloy
```

```
GRAFANA_CLOUD_OTLP_URL=<OTLP endpoint>
GRAFANA_CLOUD_OTLP_USER=<instance ID>
GRAFANA_CLOUD_LOGS_URL=<Loki URL>/loki/api/v1/push
GRAFANA_CLOUD_LOGS_USER=<Loki user>
GRAFANA_CLOUD_METRICS_URL=<Remote Write Endpoint>
GRAFANA_CLOUD_METRICS_USER=<Prometheus user>
GRAFANA_CLOUD_TOKEN=<token>
POSTGRES_MONITOR_PASSWORD=<output of: openssl rand -hex 24>
```

[`alloy.env.example`](alloy.env.example) lists the same keys. Alloy reads the file only when its container is created, so recreate it after every change, as above. Before the first deploy there is no `app-image` yet, and `compose.sh` stops with `no app-image` ([section 12](#12-reading-logs-and-other-compose-commands)). Check with `APP_IMAGE=unused /opt/ai-trainer/compose.sh config --quiet && echo ok` instead, and skip the last command: the first deploy starts Alloy with the file in place.

`POSTGRES_MONITOR_PASSWORD` is the one key Alloy doesn't send anywhere: it is the password of the database role `ai_trainer_monitor`. A migration creates the role without a password, since no secret is in the repo. [`monitor-password.sh`](monitor-password.sh) sets the password from this file, reading only that line. It needs 24 or more letters and digits; anything else is refused and nothing is set.

Every deploy runs it after `migrate`. A failure there only warns (`the monitor password was not set; Postgres metrics are off until it is`) and the release still ships. After changing the password, set it yourself, then recreate Alloy as above:

```sh
/opt/ai-trainer/deploy/monitor-password.sh /opt/ai-trainer/alloy.env /opt/ai-trainer/compose.sh   # monitor-password: result=ok role=ai_trainer_monitor
```

A missing `alloy.env` is fine, but a broken one is not. A stray line (`key cannot contain a space`), a mode the `deploy` user can't read, or a directory at that path fails *every* compose command on this file. That would make the next deploy fail before `db` starts. `config --quiet` catches all three, so don't skip it after editing the file.

### 11.3 Is the journal persistent? (ticket #274)

Alloy reads `/var/log/journal`. If the host keeps its journal only in memory (`/run/log/journal`), the outcome lines are lost at every reboot, a 02:30 backup failure included. On the server:

```sh
ls -d /var/log/journal                       # must exist; "No such file or directory" means volatile
journalctl --disk-usage                      # "Archived and active journals take up …" on disk
journalctl --header | grep -m1 'File path'   # a path under /var/log/journal/<machine-id>/
```

If the directory is missing, make the journal persistent once: create `/var/log/journal` (`sudo mkdir -p /var/log/journal`), then `sudo systemd-tmpfiles --create --prefix /var/log/journal` and restart `systemd-journald`; or set `Storage=persistent` in a drop-in under `/etc/systemd/journald.conf.d/`. Write down which you did in the provisioning record. Then recreate Alloy (the last command in 11.2) so it sees the directory.

### 11.4 Checks (ticket #272 acceptance)

Run on the server (`ssh deploy@<ip>`) unless noted.

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Running, no port | `docker ps --filter name=ai-trainer-prod-alloy-1 --format '{{.Status}} {{.Ports}}'` | `Up …` and nothing after it |
| 2 | Shipping without errors | `docker logs --since 10m ai-trainer-prod-alloy-1 2>&1 \| grep -cE 'level=(warn\|error)'` | `0`; a `401` or `403` here means a wrong user, URL or token scope |
| 3 | Logs arrive | laptop, Grafana Cloud → Explore → Loki: `{service="app"}`, then `caddy` and `db` | recent lines for each; app lines are JSON with `request_id` |
| 4 | Traces arrive | laptop: `curl -sS https://<domain>/sign-in >/dev/null`, then Explore → Tempo: a `trace_id` from that request's `ai_trainer.access` line | the `GET /sign-in` trace |
| 5 | No email shipped | Explore → Loki: `{service=~".+"} \|~ "[\\pL\\pN._%+-]+(@\|%40)[\\pL\\pN.-]+\\.\\pL{2,}"` over the last day | no lines |
| 6 | An outage costs nothing | before go-live only: `docker stop ai-trainer-prod-alloy-1; curl -sS https://<domain>/health; docker start ai-trainer-prod-alloy-1` | `{"database":"ok"}` while Alloy is down |

### 11.5 Checks (ticket #274 acceptance)

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Journal is persistent | section 11.3 | the three commands answer as described |
| 2 | Outcome lines arrive | start `ai-trainer-backup.service` and `ai-trainer-purge.service` with `sudo systemctl start`, then laptop, Grafana Cloud → Explore → Loki: `{job=~"backup\|purge\|deploy"}` | `backup: result=ok kind=nightly` under `job="backup"` and `purge: result=ok deleted=<n>` under `job="purge"` within a minute |
| 3 | Nothing else is shipped | Explore → Loki: the label browser for `job` | only `backup`, `purge` and `deploy`; no `sshd`, `sudo` or other unit's lines |
| 4 | A failure arrives | before go-live only: `docker stop ai-trainer-prod-app-1`, `sudo systemctl start ai-trainer-purge.service`, `docker start ai-trainer-prod-app-1`, then Explore → Loki: `{job="purge"} \|= "result=failed"` | one `purge: result=failed` line; run `sudo systemctl reset-failed ai-trainer-purge.service` afterwards |

### 11.6 Checks (ticket #273 acceptance)

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Password set | `journalctl -u ai-trainer-deploy.service --no-pager \| grep -c 'monitor password was not set'` after the first deploy with this ticket | `0` |
| 2 | Metrics arrive | laptop, Grafana Cloud → Explore → Prometheus: `node_filesystem_avail_bytes{mountpoint="/"}`, `caddy_http_requests_total`, `pg_stat_database_numbackends{datname="ai_trainer"}` | one recent series each for the root filesystem and the database, all with `instance="ai-trainer-prod"`; Caddy's under `server="public"` |
| 3 | The VPS, not the container | Explore → Prometheus: `node_filesystem_size_bytes{mountpoint="/"}` and `node_memory_MemTotal_bytes` | about the CX23's 40 GB disk and 4 GB memory, not Alloy's 256 MiB limit |
| 4 | Nothing on 443 | laptop: `curl -s https://<domain>/metrics \| grep -c '^caddy_'` | `0` |
| 4a | Not from the app either | `docker exec ai-trainer-prod-app-1 python -c "import urllib.request as u; print(u.urlopen('http://10.222.0.10:9180/metrics', timeout=5).status)"` | an error: `HTTP Error 403`, or no route at all; never `200` |
| 5 | The role reads no app table | `docker exec ai-trainer-prod-db-1 psql -U ai_trainer -d ai_trainer -c "SET ROLE ai_trainer_monitor; SELECT 1 FROM users LIMIT 1"` | `ERROR:  permission denied for table users` |
| 6 | Only the kept series | Explore → Prometheus: `count by (__name__) ({instance="ai-trainer-prod", job=~"integrations/.+"})` | about thirty names, all `node_`, `caddy_` or `pg_` |

### 11.7 Alerts

Source: ADR-0030 *Dashboards and alerts are code*; PRD 0008 G12, G13, G14. Ticket #277.

[`grafana/alerts.json`](grafana/alerts.json) is one rule group, `ai-trainer`, in a folder of the same name. Every rule's description says where to look first.

| Rule | Fires when | Waits |
|---|---|---|
| Site down | deferred: pause it after import (step 4). Without the check it would fire on no data | 2 min |
| 5xx above 5% | more than 5% of the app's requests answer 5xx over 10 min | — |
| Log turn p95 above 10 s | send-to-draft p95 over 30 min is above 10 s (G14) | — |
| Model-call errors above 20% | more than 20% of model calls end in `error` or `timeout` over 15 min; `cancelled` is left out | — |
| No backup in 26 h | no `backup: result=ok` line in 26 h (G13) | — |
| Scheduled job failed | a `backup`, `purge` or `deploy` `result=failed` line in the last hour, one alert per job; it clears an hour after the last one | — |
| Disk above 80% | the root filesystem is more than 80% full | 10 min |
| No telemetry in 15 min | no host metric from the server in 15 min. Alloy writes one every 60 s whatever the traffic, so Alloy is down, its token expired (11.1 step 6), or the host is. A quiet night sends no log lines, so logs can't serve as the heartbeat; a break that stops only logs shows up as "No backup in 26 h" | — |

With no traffic, the three ratio rules have no data and stay quiet. "No backup in 26 h" fires 26 hours after go-live if the first backup never ran.

1. **Contact point.** Grafana Cloud → *Alerting → Contact points → + Add contact point*: name `owner-email`, integration **Email**, your address. **Test** it, and check the email arrives. Then go to *Alerting → Notification policies*, open **Default policy** → *Edit*, and set its contact point to `owner-email`. Every rule here goes through the default policy.
2. **The health check: deferred** (ADR-0030, amended 2026-10-28). Synthetic Monitoring's probes reach `/health` through Cloudflare, Bot Fight Mode challenges them, and it stays on. Don't create the check. A follow-up ticket replaces this step.
3. **Cloudflare: nothing to change.** Bot Fight Mode stays **on**. A WAF custom rule can't skip it, so a `/health` skip rule does nothing for it. You can delete one if you made it.
4. **Import the rules** from your laptop. At go-live, import them right after the first deploy succeeds, not before the tag: until the site and Alloy run, "No telemetry in 15 min" and "No backup in 26 h" fire and email you. Do it before inviting anyone (#227).
   - Grafana Cloud → *Administration → Users and access → Service accounts → Add service account*: name `alerts-import`, role **Editor**.
   - *Add service account token*, with a short expiry.
   - Note the UIDs of the stack's Prometheus and Loki datasources (*Connections → Data sources*; the UID is the last part of each one's URL, usually `grafanacloud-prom` and `grafanacloud-logs`).
   - Then run:

   ```sh
   GRAFANA_TOKEN=<token> deploy/grafana/import-alerts.sh https://<stack>.grafana.net <prometheus uid> <loki uid>
   #   import-alerts: result=ok imported 8 rules
   ```

   - Each run replaces the whole group, so re-run it after any change to `alerts.json`.
   - Rules imported this way are marked *Provisioned* and are read-only in the UI. Change them in the file, not in Grafana.
   - Delete the token when you're done.
   - After the import, *Alerting → Alert rules* shows the eight rules under `ai-trainer`, each **Normal** or **Pending**, never **Error**. "No backup in 26 h" is **Firing** until the first nightly backup has run.
   - Open **Site down** → *More → Pause evaluation*. With no health check it has no data, and it would fire. A re-import may resume it, so pause it again after each one.

**Test-fire.** At go-live, prove that an alert actually reaches you:

1. On the server: `systemd-cat -t ai-trainer-backup echo "backup: result=failed kind=test reason=test exit=1"`. Within about 2 minutes you get *The backup job failed*. It resolves an hour later. The line travels the same path as a real failure: the journal, Alloy's `job="backup"` label, then Loki.
2. ~~Before go-live only: `docker stop ai-trainer-prod-db-1`; *The site is down* within about 4 minutes.~~ Deferred with the health check (step 2).
3. Before go-live only: `docker stop ai-trainer-prod-alloy-1`. In 16 to 17 minutes you get *No telemetry from the server in 15 minutes*. Then run `docker start ai-trainer-prod-alloy-1`.

### 11.8 Checks (ticket #277 acceptance)

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Rules imported | Grafana Cloud → *Alerting → Alert rules* → folder `ai-trainer` | eight rules, none in **Error**, **Site down** paused |
| 2 | ~~Health check green~~ | deferred (11.7 step 2) | — |
| 3 | ~~`/health` unchallenged~~ | deferred (11.7 step 2) | — |
| 4 | A job failure emails you | test-fire step 1 | the email within about 2 minutes |
| 5 | An outage emails you | test-fire step 3, before go-live | the email |

### 11.9 Dashboard

Source: ADR-0030 *Dashboards and alerts are code*; PRD 0008 G12, G13, G14. Ticket #276.

[`grafana/dashboards/overview.json`](grafana/dashboards/overview.json) is the one page to open after a release, `ai-trainer overview`. It has four rows:

| Row | Panels |
|---|---|
| Traffic | 5xx rate; request p50/p95; log turn p50/p95, with G14's 5 s and 10 s marked |
| Model calls | calls, errors (`error` or `timeout`) and cost by template, and rate-limit refusals by limit kind, each over the 24 h up to the end of the range |
| Host and database | CPU, memory and root disk use; Postgres connections against `max_connections` |
| Logs | the backup, purge and deploy outcome lines of the last 7 days, newest first, so the top `backup:` line is the last backup's result; the app's ERROR and CRITICAL lines and its 5xx access lines, with a link on each `trace_id` |

- It finds the stack's own Prometheus, Loki and Tempo by their default names, `grafanacloud-<stack>-prom`, `-logs` and `-traces`, so the import asks for nothing. If you renamed one, pick it in the dashboard's settings under *Variables*.
- An unhandled exception's own line has no `trace_id`, because Uvicorn writes it after the request's span has closed. The request's access line, with the same `request_id` and status `500`, sits next to it and carries the link.
- The per-day panels count the calls and refusals of the first minute after a deploy too. The rate panels (5xx, durations) and the alert ratios start about a minute after each deploy, since the app's series are new at every start.

**Import** from your laptop, the first time and after every change to the file:

1. Grafana Cloud → *Dashboards → New → Import*, then *Upload dashboard JSON file* and pick `deploy/grafana/dashboards/overview.json`.
2. **Folder:** `ai-trainer`, the one the alert import created (11.7 step 4).
3. The first time, *Import*. After that, Grafana says a dashboard with the same uid (`ai-trainer-overview`) exists: choose *Import (Overwrite)*.
4. Change the dashboard in the file, not in Grafana. An edit saved in the UI is lost at the next import.

### 11.10 Checks (ticket #276 acceptance)

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Imported | open *Dashboards → ai-trainer → ai-trainer overview* | every panel without a red error corner; *Settings → Variables* shows `prometheus`, `loki` and `tempo` set to the stack's `grafanacloud-…` datasources |
| 2 | Data after a release | after the first deploy, a backup and a few log turns, last 24 h | a value or line in every panel; `backup: result=ok kind=nightly` at the top of *Scheduled job outcomes* |
| 3 | Trace link | *App errors*: click a `trace_id`, then *Open trace* | Explore → Tempo opens that request's trace |

## 12. Reading logs and other compose commands

Compose interpolates the whole of `compose.prod.yaml` before any command, so a bare `docker compose -f compose.prod.yaml logs caddy` fails with `required variable APP_IMAGE is missing a value` (#303). Run Compose through [`compose.sh`](compose.sh) instead, which [`install.sh`](install.sh) puts at `/opt/ai-trainer/compose.sh`. It reads `SITE_HOST` from `deploy.env`, `APP_IMAGE` from `app-image` and `RELEASE_TAG` from `app-version`, then runs `docker compose` on the production file with the arguments you give it. A value already set in the shell wins, so `APP_IMAGE=<digest> compose.sh …` still picks an image by hand. A host that has never been deployed has no `app-image`, and the wrapper stops instead of guessing. On the server:

```sh
/opt/ai-trainer/compose.sh ps                                  # every service, its image and health
/opt/ai-trainer/compose.sh logs app --since 1h | tail -50      # app: one JSON line per request and log record
/opt/ai-trainer/compose.sh logs app --since 1h | grep -E '/auth/|sign-in failed'
/opt/ai-trainer/compose.sh logs -f caddy                       # follow; Ctrl-C stops
/opt/ai-trainer/compose.sh logs db alloy --since 30m
journalctl -u ai-trainer-deploy.service --since today          # the poller; also ai-trainer-backup and ai-trainer-purge
```

- Compose writes its own errors to stderr, so a `| grep` after a failed command prints nothing, as if the logs were empty. When a filtered command prints nothing, run it again without the filter, or put `2>&1` before the pipe.
- The app's access line has the route template (`/auth/callback`), the method, the status and the duration. It never has the query, the client's address or a user (ADR-0030 invariant 3). Use a line's `request_id` to find that request's other lines.
- `docker logs ai-trainer-prod-<service>-1` reads the same logs and needs no variables, if the wrapper isn't installed yet.
- The same logs reach Grafana Cloud through Alloy ([section 11](#11-telemetry-to-grafana-cloud)). Use them there for anything older than the containers, since a recreated container starts with an empty `docker logs`.

## 13. Exercise catalogue

Source: ADR-0031. Ticket #375.

The app's migrations create the catalogue's tables empty. Run the import once, by hand, after the first deploy that carries them, and again after a release that bumps `EXERCISE_DATASET_COMMIT` or changes `data/catalogue/`:

```sh
docker exec ai-trainer-prod-app-1 ai-trainer import-exercises
```

It ends on one line. `import-exercises: result=ok exercises=1317 inserted=1317 updated=0 unchanged=0 texts_written=2633 skipped=8` is the first import of the commit pinned at #375. A later run of the same release reports `inserted=0 updated=0 texts_written=0`. `import-exercises: result=failed reason=<download|catalogue> detail=<why>` with exit code 1 changes no row, so fix the cause and run it again. It needs outbound HTTPS to `raw.githubusercontent.com`.

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
