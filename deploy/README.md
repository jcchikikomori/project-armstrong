# MediKeep deployment — Caddy + DuckDNS on a DigitalOcean droplet

Runbook for [MediKeep](https://github.com/afairgiant/MediKeep) behind Caddy with automatic
Let's Encrypt certificates, a DuckDNS updater, host-mapped data, and a nightly JSON export.

Claude generated these files.

## What gets deployed

Four containers on one bridge network. Only Caddy publishes ports.

| Service | Image | Exposed |
| --- | --- | --- |
| `medikeep-caddy` | `caddy:2-alpine` | `80/tcp`, `443/tcp`, `443/udp` |
| `medikeep-app` | `ghcr.io/afairgiant/medikeep:latest` | internal `8000` only |
| `medikeep-db` | `postgres:15.8-alpine` | internal `5432` only |
| `medikeep-duckdns` | `lscr.io/linuxserver/duckdns:latest` | nothing |

Upstream's compose publishes `5432:5432` to the host. That is removed here — on a public
droplet it would put the database on the internet.

## Prerequisites

1. A DigitalOcean droplet running Ubuntu 22.04 or 24.04, with a public IPv4 address.
2. A DuckDNS subdomain and its token, from <https://www.duckdns.org>.
3. Ports 80 and 443 reachable from the internet. Let's Encrypt validates over HTTP-01 on port 80.

## Deploy

```bash
git clone <this-repo> /root/project-armstrong
cd /root/project-armstrong
sudo ./deploy/deploy.sh \
  --domain nan0machin3s.duckdns.org \
  --duckdns-token <your-duckdns-token> \
  --email you@example.com
```

Options: `--tz` (default `Asia/Singapore`; `Australia/Perth` is the same UTC+8 with no DST),
`--stack-dir` (default `/opt/medikeep`), `--force` to regenerate `.env`.

The script prints the generated `admin` password once at the end. Change it on first login.

### What the script does, in order

1. Preflight — root, Ubuntu, required arguments, refuses to clobber an existing `.env`.
2. Installs `ca-certificates curl gnupg jq ufw cron dnsutils`.
3. Sets the host timezone, so cron and the containers agree.
4. Installs Docker CE + the compose plugin from Docker's own apt repo.
5. Configures `ufw`: SSH, 80/tcp, 443/tcp, 443/udp; everything else denied.
6. Creates `/opt/medikeep/data/*`.
7. Generates `.env` with random `DB_PASSWORD`, `SECRET_KEY`, `ADMIN_DEFAULT_PASSWORD` (chmod 600).
8. Installs the compose file, Caddyfile, export script, and the export cron.
9. Starts DuckDNS **alone**, then waits for the domain to resolve to this droplet.
10. Starts the rest and waits for `https://<domain>/health`.

Step 9 exists because Caddy's HTTP-01 challenge fails closed and Let's Encrypt allows only
5 failed validations per hostname per hour. Starting Caddy against DNS that has not propagated
burns that budget for an hour.

## Layout on the droplet

```
/opt/medikeep/
  docker-compose.yml
  Caddyfile
  .env                       chmod 600 — DB password, JWT key, DuckDNS token
  scripts/export-json.sh
  data/
    postgres/                -> /var/lib/postgresql/data
    uploads/                 -> /app/uploads
    logs/                    -> /app/logs
    backups/                 -> /app/backups
    exports/<timestamp>/     JSON dataset, plus a `latest` symlink
    caddy/data/              ACME account + issued certs — do not delete
    caddy/config/
    duckdns/
```

## Getting the records out as JSON

Bind-mounting `/var/lib/postgresql/data` gives you binary Postgres heap files, not records.
Readable output has to come through `psql`, which is what `export-json.sh` does.

```bash
sudo /opt/medikeep/scripts/export-json.sh
```

Runs nightly at 02:30 local time via `/etc/cron.d/medikeep-export`. Each run writes:

```
data/exports/2026-09-21T18-30-00Z/
  tables/<table>.json    one JSON array per table
  dataset.json           {"exported_at": ..., "database": ..., "tables": {...}}
  manifest.json          per-table row counts
data/exports/latest      symlink to the newest run
```

`dataset.json` is the single file to hand to an LLM pipeline. The table list comes from
`information_schema` rather than being hardcoded, so the export keeps working across MediKeep's
alembic migrations.

Runs older than `EXPORT_RETENTION_DAYS` (default 14) are pruned. Output is `chmod 600`.

Pull a dataset down to your machine:

```bash
scp root@<droplet-ip>:/opt/medikeep/data/exports/latest/dataset.json .
```

## Day-to-day

```bash
cd /opt/medikeep

docker compose ps                          # status
docker compose logs -f medikeep-app        # app logs
docker compose logs caddy | grep -i certificate
docker compose exec postgres psql -U medapp -d medical_records

docker compose pull && docker compose up -d   # update (export JSON first)
docker compose down                           # stop
```

### Backup

The bind mounts mean a backup is a file copy. Stop the stack first so Postgres is consistent:

```bash
cd /opt/medikeep
docker compose down
tar czf /root/medikeep-$(date +%F).tar.gz -C /opt/medikeep data .env docker-compose.yml Caddyfile
docker compose up -d
```

For a hot backup without downtime, use `pg_dump` instead:

```bash
docker compose exec -T postgres pg_dump -U medapp -Fc medical_records > /root/medikeep-$(date +%F).dump
```

### Restore

Restore into an empty `data/postgres`, or Postgres' initdb refuses to run.

Note on ownership: the Postgres container chowns `data/postgres` to its own `postgres` user
(uid 999). Anything touching that directory from the host — including `rm -rf` — needs root.
That is expected, not a misconfiguration.

## Risks worth knowing about

1. **Medical records on an internet-facing host.** HTTPS protects transit only. The droplet
   disk, and every `exports/*.json`, are plaintext. Droplet backups and disk encryption are
   your call, not something this script decides for you.
2. **`ADMIN_DEFAULT_PASSWORD`.** Upstream defaults it to `admin123`. The script generates a
   random one instead, but it only applies to the *initial* admin seed — change it after
   first login and the `.env` value stops mattering.
3. **The DuckDNS token is a DNS takeover credential.** Anyone holding it can repoint the
   subdomain and obtain a valid certificate for it. It lives in `.env`, chmod 600, never committed.
4. **Let's Encrypt rate limits** — 5 failed validations per hostname per hour. The DNS gate in
   step 9 exists to protect that budget.
5. **`latest` tag moves.** `ghcr.io/afairgiant/medikeep:latest` is not pinned. Updates are
   manual, and worth running an export before.

## Verification checklist

```bash
cd /opt/medikeep
docker compose ps                                  # 4 services Up, db + app (healthy)
curl -sI https://nan0machin3s.duckdns.org/health   # 200
curl -sI http://nan0machin3s.duckdns.org           # 308 -> https
docker compose logs caddy | grep -i "certificate obtained"
docker compose logs duckdns | tail                 # OK
ss -ltnp | grep -E '5432|8005'                     # expect NO match
./scripts/export-json.sh
jq '.tables | keys' data/exports/latest/dataset.json
```
