# project-armstrong

My self-hosted setup for [MediKeep](https://github.com/afairgiant/MediKeep), an open-source
personal medical records app, running on a single DigitalOcean droplet.

The goal is simple: keep my own health records on a box I control, reachable over HTTPS,
and get the data back out as plain JSON so I can feed it to an LLM pipeline later.

## What's in here

```
deploy/
  deploy.sh              one-shot bootstrap for a fresh Ubuntu droplet
  docker-compose.yml     Caddy + MediKeep + Postgres + DuckDNS
  Caddyfile              TLS termination and reverse proxy
  .env.example           reference for every variable the stack reads
  scripts/export-json.sh nightly Postgres -> JSON export
  README.md              the full runbook
```

## The stack at a glance

| Service | Role | Exposed |
| --- | --- | --- |
| **Caddy** | HTTPS with automatic Let's Encrypt certs | `80`, `443/tcp`, `443/udp` |
| **MediKeep** | the app itself | internal only |
| **Postgres 15** | records database | internal only |
| **DuckDNS** | keeps the free subdomain pointed at the droplet | nothing |

Only Caddy publishes ports. Upstream's compose file publishes Postgres on `5432`, which is
fine on a home LAN and a bad idea on a public droplet, so that mapping is gone here.

## Quick start

On a fresh Ubuntu 22.04 or 24.04 droplet, with a DuckDNS subdomain and token ready:

```bash
git clone <this-repo> /root/project-armstrong
cd /root/project-armstrong
sudo ./deploy/deploy.sh \
  --domain <sub>.duckdns.org \
  --duckdns-token <your-duckdns-token> \
  --email you@example.com
```

The script installs Docker, locks down `ufw`, generates random secrets into
`/opt/medikeep/.env`, waits for DNS to resolve, then brings everything up. It prints the
`admin` password once at the end, so copy it before the terminal scrolls away.

Everything else (layout on the droplet, backups, restore, updates, verification) lives in
**[deploy/README.md](deploy/README.md)**.

## Importing the Google Sheets history

My health tracking started as a Google Form feeding a spreadsheet, so the
history predates this droplet by two years. `import/` migrates it: seven CSV
exports in `datasets/` become 414 MediKeep records over the REST API.

MediKeep ships no generic CSV import -- the one importer it has is vitals-only
and hardcoded to Dexcom -- so this is a client, not a patch. It runs in Docker
and is safe to re-run; a SQLite ledger keyed on `<file>:<row_index>` means a
second pass creates nothing.

```bash
export MEDIKEEP_PASSWORD='...'
docker compose -f import/docker-compose.yml run --rm importer plan      # dry run
docker compose -f import/docker-compose.yml run --rm importer apply --yes
```

The interesting part was not the HTTP. The CSVs are event logs and MediKeep is
a record store, and 508 of the 688 tracker rows are medication _dose events_ --
one per swallow, against a table that holds one row per drug. Those, plus the
sleep, food, insights and persona rows, go to `datasets/derived/*.json` with
full fidelity instead of being forced into a schema that would misrepresent
them. Details and the full mapping are in **[import/README.md](import/README.md)**.

## Getting the data out

Bind-mounting the Postgres data directory gives you binary heap files, not records, which
took me a moment to accept. So `export-json.sh` goes through `psql` instead and writes one
JSON file per table plus a combined `dataset.json`, nightly at 02:30.

```bash
scp root@<droplet-ip>:/opt/medikeep/data/exports/latest/dataset.json .
```

## Things that bit during setup

A few lessons that shaped the scripts, recorded so I don't relearn them:

- **Fresh droplets hold the apt lock.** `unattended-upgrades` runs on first boot, so the
  first `apt-get` fails with `Could not get lock /var/lib/dpkg/lock-frontend`. The first fix
  polled for the process by name and hung forever, because `unattended-upgrade-shutdown`
  shares the same truncated name and never exits. The script now uses apt's own
  `DPkg::Lock::Timeout` instead.
- **Let's Encrypt only forgives 5 failures per hour.** Starting Caddy before DNS propagates
  burns that budget fast, so `deploy.sh` starts DuckDNS alone and waits for the domain to
  resolve before Caddy ever runs.

## Security notes

This is medical data on an internet-facing host, so a few things are worth being blunt about:

- HTTPS protects data in transit only. The droplet disk and every JSON export are plaintext.
- The DuckDNS token can repoint the subdomain and get a valid cert for it. Treat it like a password.
- `.env` holds all of the above and is `chmod 600` and gitignored. Never commit it.

The full risk list is in [deploy/README.md](deploy/README.md#risks-worth-knowing-about).

## Credits

- [MediKeep](https://github.com/afairgiant/MediKeep) by afairgiant does the actual work.
- Claude generated the deployment scripts and this README.

## License

[MIT](LICENSE)
