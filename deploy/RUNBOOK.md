# Production host runbook (owner only)

Source: ADR-0023 (*Host*, *Ingress and TLS*, ⚠ *Verify at provisioning*), ADR-0024 invariant 5, PRD 0005 G12. Ticket #222.

Agents never reach the host: the `guard_release.py` hook denies `ssh` and `scp`, and ADR-0024 invariant 5 forbids it. The owner runs every step here from their laptop. The only file the server needs from this repo up front is [`cloud-init.yaml`](cloud-init.yaml), which `make cloud-init-check` validates.

Placeholders used below: `<domain>` is the host of `PUBLIC_BASE_URL` (e.g. `trainer.example.com`), `<ip>` the server's public IPv4, `<my-ip>` your own public address (`curl -4 https://ifconfig.me`).

Tools on the laptop: `hcloud` ([Hetzner CLI](https://github.com/hetznercloud/cli)) with a project context (`hcloud context create ai-trainer`), `curl`, `jq`, `openssl`, `nc`.

## 1. Check stock and price

1. In the Cloud Console → *Add server* → an EU location (Falkenstein, Nuremberg or Helsinki) → *Cost-optimized* → **CX23** (x86, 2 vCPU, 4 GB). A greyed-out type is out of stock there; try another EU location before falling back.
2. `hcloud server-type describe cx23` shows the price per location. ADR-0023 expects €5.49/month before VAT and IPv4.
3. **Fallback: CAX11** (Arm, €5.99). It needs a `linux/arm64` image, which `release.yml` doesn't build yet (#219 builds amd64 only). Stop and raise a ticket for a multi-arch build before ordering it.
4. Record the type, location and price now, privately (see [Provisioning record](#provisioning-record)). The price is the one shown at order time, not this file's expectation.

## 2. SSH key

Upload your public key once: `hcloud ssh-key create --name owner --public-key-from-file ~/.ssh/id_ed25519.pub`. Hetzner hands it to cloud-init as instance metadata, and `cloud-init.yaml` installs it for `deploy` (the default user there). Root gets no usable key.

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

- `--enable-backup` turns Hetzner's own backups on (+20% of the server price). They are the second layer; the offsite age-encrypted dump is [section 8](#8-backups-to-r2) (ADR-0023 *Backups*).
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
   - Keep `aop-ca-key.pem` offline, like the age key. Only `aop-ca.pem` goes to the server.
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

Starting the stack and the first deploy wait for the deploy-poller ticket and for M1a and M1 (ADR-0023).

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

| Prefix | Written by | Locked and kept for |
|---|---|---|
| `daily/` | the nightly timer, Monday to Saturday (UTC) | 7 days |
| `weekly/` | the nightly timer on Sunday (UTC) | 28 days |
| `predeploy/` | `deploy.sh`, before `migrate` | 7 days |

Sunday's backup goes to `weekly/` only, so `daily/` holds at most six objects: with Sunday's, that's seven nightly restore points in any week.

A bucket lock rule overrides a lifecycle rule ([R2 docs](https://developers.cloudflare.com/r2/buckets/bucket-locks/)), so each prefix gets a lock and an expiry of the same age. R2 has no write-only token: the server's token can delete objects, and only the lock stops it inside the window. Changing a lock rule needs an admin token, which never goes to the server.

### 8.1 Your age key (laptop)

```sh
brew install age
age-keygen -o ai-trainer-backup.key      # prints "Public key: age1…"
```

- Keep `ai-trainer-backup.key` offline, like `aop-ca-key.pem`: a password manager entry and one offline copy. Without it no backup can be restored. The restore drill (#224) is the only thing that reads it, on your laptop.
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

From your laptop, in this repo:

```sh
scp deploy/backup.sh deploy/systemd/ai-trainer-backup.service deploy/systemd/ai-trainer-backup.timer deploy@<ip>:/tmp/
```

Then `ssh deploy@<ip>` and on the server:

```sh
sudo apt-get install -y age rclone       # only on a host provisioned before #223; cloud-init installs them now
install -d -m 0750 /opt/ai-trainer/deploy
install -m 0755 /tmp/backup.sh /opt/ai-trainer/deploy/backup.sh
sudo install -o root -g root -m 0644 /tmp/ai-trainer-backup.service /tmp/ai-trainer-backup.timer /etc/systemd/system/
rm /tmp/backup.sh /tmp/ai-trainer-backup.*
umask 077
$EDITOR /opt/ai-trainer/backup.env       # from deploy/backup.env.example: ACCOUNT_ID in the endpoint, the token, your age1… key
chmod 0600 /opt/ai-trainer/backup.env
sudo systemctl daemon-reload
sudo systemctl enable --now ai-trainer-backup.timer
```

- Write `backup.env` values unquoted, one `KEY=value` per line: systemd's `EnvironmentFile=` and the shell checks below both read it.
- `backup.sh` dumps from the `ai-trainer-prod-db-1` container. Until the first deploy starts the stack, start Postgres alone for the checks below. Compose reads `/opt/ai-trainer/.env` and `db.env` even then, so [section 6](#6-server-files) comes first. Copy `compose.prod.yaml` to `/opt/ai-trainer/`, then `cd /opt/ai-trainer && APP_IMAGE=unused SITE_HOST=unused docker compose -f compose.prod.yaml up -d db`. That database is empty, and an empty dump is still a valid backup.

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
