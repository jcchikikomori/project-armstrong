#!/usr/bin/env bash
#
# Bootstrap MediKeep on a fresh Ubuntu DigitalOcean droplet.
#
#   sudo ./deploy/deploy.sh \
#     --domain nan0machin3s.duckdns.org \
#     --duckdns-token <token> \
#     --email you@example.com
#
# Idempotent: re-running skips package installs that already succeeded and
# refuses to overwrite an existing .env unless --force is passed.

set -euo pipefail

STACK_DIR="${STACK_DIR:-/opt/medikeep}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TZ_NAME="Asia/Singapore"
FORCE=0
DOMAIN=""
DUCKDNS_TOKEN=""
ACME_EMAIL=""

log() { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die() {
	printf '\nerror: %s\n' "$*" >&2
	exit 1
}

# A fresh droplet runs unattended-upgrades on first boot, which holds the dpkg
# lock for a minute or two. Without this wait, apt-get fails with
# "Could not get lock /var/lib/dpkg/lock-frontend".
wait_for_apt() {
	local waited=0
	while pgrep -x unattended-upgr >/dev/null 2>&1 ||
		pgrep -x apt-get >/dev/null 2>&1 ||
		pgrep -x dpkg >/dev/null 2>&1; do
		[ "$waited" -eq 0 ] && info "waiting for another apt/dpkg process to finish"
		sleep 5
		waited=$((waited + 5))
		[ "$waited" -ge 600 ] && die "apt/dpkg still locked after 600s; check with: ps aux | grep -E 'apt|dpkg'"
	done
	[ "$waited" -gt 0 ] && info "apt lock released after ${waited}s"
	return 0
}

usage() {
	cat <<'USAGE'
Usage: deploy.sh --domain <sub>.duckdns.org --duckdns-token <token> --email <addr> [options]

Required:
  --domain <fqdn>          DuckDNS FQDN, e.g. nan0machin3s.duckdns.org
  --duckdns-token <token>  From https://www.duckdns.org (treat as a DNS takeover credential)
  --email <addr>           Contact address for the Let's Encrypt account

Options:
  --tz <zone>              Timezone, default Asia/Singapore (Australia/Perth is the same UTC+8)
  --stack-dir <path>       Install location, default /opt/medikeep
  --force                  Regenerate .env even if one already exists
  -h, --help               This message
USAGE
}

while [ $# -gt 0 ]; do
	case "$1" in
	--domain)
		DOMAIN="${2:-}"
		shift 2
		;;
	--duckdns-token)
		DUCKDNS_TOKEN="${2:-}"
		shift 2
		;;
	--email)
		ACME_EMAIL="${2:-}"
		shift 2
		;;
	--tz)
		TZ_NAME="${2:-}"
		shift 2
		;;
	--stack-dir)
		STACK_DIR="${2:-}"
		shift 2
		;;
	--force)
		FORCE=1
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	*) die "unknown argument: $1" ;;
	esac
done

# --- 1. Preflight ---------------------------------------------------------
log "1/10 Preflight"

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0 ...)"
grep -qi ubuntu /etc/os-release || die "this script targets Ubuntu; /etc/os-release says otherwise"
[ -n "$DOMAIN" ] || {
	usage
	die "--domain is required"
}
[ -n "$DUCKDNS_TOKEN" ] || {
	usage
	die "--duckdns-token is required"
}
[ -n "$ACME_EMAIL" ] || {
	usage
	die "--email is required"
}

case "$DOMAIN" in
*.duckdns.org) DUCKDNS_SUBDOMAIN="${DOMAIN%.duckdns.org}" ;;
*) die "--domain must end in .duckdns.org (got: $DOMAIN)" ;;
esac
[ -n "$DUCKDNS_SUBDOMAIN" ] || die "could not derive a subdomain label from $DOMAIN"

if [ -f "$STACK_DIR/.env" ] && [ "$FORCE" -ne 1 ]; then
	die "$STACK_DIR/.env already exists. Pass --force to regenerate secrets (this rotates the DB password and will break the existing database)."
fi

[ -f "$SCRIPT_DIR/docker-compose.yml" ] || die "docker-compose.yml not found next to this script ($SCRIPT_DIR)"
[ -f "$SCRIPT_DIR/Caddyfile" ] || die "Caddyfile not found next to this script ($SCRIPT_DIR)"
[ -f "$SCRIPT_DIR/scripts/export-json.sh" ] || die "scripts/export-json.sh not found next to this script ($SCRIPT_DIR)"

info "domain:    $DOMAIN"
info "subdomain: $DUCKDNS_SUBDOMAIN"
info "timezone:  $TZ_NAME"
info "install:   $STACK_DIR"

# --- 2. Base packages -----------------------------------------------------
log "2/10 Base packages"
export DEBIAN_FRONTEND=noninteractive
wait_for_apt
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg jq ufw cron dnsutils >/dev/null
info "installed ca-certificates curl gnupg jq ufw cron dnsutils"

# --- 3. Timezone ----------------------------------------------------------
log "3/10 Timezone"
# The export cron fires on host time, so the host and the containers must agree.
timedatectl set-timezone "$TZ_NAME"
info "host clock now $(date '+%Y-%m-%d %H:%M:%S %Z')"

# --- 4. Docker ------------------------------------------------------------
log "4/10 Docker"
if docker compose version >/dev/null 2>&1; then
	info "docker compose already present: $(docker compose version --short)"
else
	install -m 0755 -d /etc/apt/keyrings
	curl -fsSL https://download.docker.com/linux/ubuntu/gpg |
		gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg
	chmod a+r /etc/apt/keyrings/docker.gpg

	arch="$(dpkg --print-architecture)"
	codename="$(. /etc/os-release && printf '%s' "$VERSION_CODENAME")"
	printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu %s stable\n' \
		"$arch" "$codename" >/etc/apt/sources.list.d/docker.list

	wait_for_apt
	apt-get update -qq
	apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
		docker-buildx-plugin docker-compose-plugin >/dev/null
	systemctl enable --now docker
	info "installed $(docker compose version --short)"
fi

# --- 5. Firewall ----------------------------------------------------------
log "5/10 Firewall"
# Allow SSH *before* enabling, or this session dies with the script half done.
ufw allow OpenSSH >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null
info "open: 22/tcp 80/tcp 443/tcp 443/udp; everything else denied"

# --- 6. Directories -------------------------------------------------------
log "6/10 Directories"
mkdir -p \
	"$STACK_DIR/scripts" \
	"$STACK_DIR/data/postgres" \
	"$STACK_DIR/data/uploads" \
	"$STACK_DIR/data/logs" \
	"$STACK_DIR/data/backups" \
	"$STACK_DIR/data/exports" \
	"$STACK_DIR/data/caddy/data" \
	"$STACK_DIR/data/caddy/config" \
	"$STACK_DIR/data/duckdns"
chmod 700 "$STACK_DIR/data"
info "tree created under $STACK_DIR/data"

# Postgres initdb refuses to run against a non-empty directory.
if [ -n "$(ls -A "$STACK_DIR/data/postgres" 2>/dev/null)" ]; then
	info "note: $STACK_DIR/data/postgres is not empty; the existing database is kept as-is"
fi

# --- 7. Secrets -----------------------------------------------------------
log "7/10 Secrets"
# Compose expands $ and treats an unquoted # as a comment, so strip both from
# generated passwords rather than relying on escaping rules.
rand_pw() { openssl rand -base64 "$1" | tr -d '$#=+/\n'; }

DB_PASSWORD="$(rand_pw 32)"
SECRET_KEY="$(openssl rand -hex 32)"
ADMIN_DEFAULT_PASSWORD="$(rand_pw 24)"

umask 077
cat >"$STACK_DIR/.env" <<EOF
# Generated by deploy.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Contains the database password, the JWT signing key, and a DuckDNS token that
# can repoint this subdomain. Never commit this file.

DOMAIN=$DOMAIN
ACME_EMAIL=$ACME_EMAIL

DUCKDNS_SUBDOMAIN=$DUCKDNS_SUBDOMAIN
DUCKDNS_TOKEN=$DUCKDNS_TOKEN

DB_NAME=medical_records
DB_USER=medapp
DB_PASSWORD=$DB_PASSWORD

SECRET_KEY=$SECRET_KEY
ADMIN_DEFAULT_PASSWORD=$ADMIN_DEFAULT_PASSWORD
DEBUG=false
ENABLE_API_DOCS=false
LOG_LEVEL=INFO
TZ=$TZ_NAME
ALLOW_PRIVATE_INTEGRATION_URLS=false

PUID=1000
PGID=1000

EXPORT_RETENTION_DAYS=14
EOF
chmod 600 "$STACK_DIR/.env"
umask 022
info "wrote $STACK_DIR/.env (chmod 600)"

# --- 8. Config files ------------------------------------------------------
log "8/10 Config files"
install -m 0644 "$SCRIPT_DIR/docker-compose.yml" "$STACK_DIR/docker-compose.yml"
install -m 0644 "$SCRIPT_DIR/Caddyfile" "$STACK_DIR/Caddyfile"
install -m 0750 "$SCRIPT_DIR/scripts/export-json.sh" "$STACK_DIR/scripts/export-json.sh"

cat >/etc/cron.d/medikeep-export <<EOF
# Nightly JSON export of the medical records, for LLM processing.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
30 2 * * * root STACK_DIR=$STACK_DIR $STACK_DIR/scripts/export-json.sh >> $STACK_DIR/data/logs/export.log 2>&1
EOF
chmod 644 /etc/cron.d/medikeep-export
info "installed /etc/cron.d/medikeep-export (02:30 $TZ_NAME)"

# --- 9. DNS first, then the rest -----------------------------------------
log "9/10 DuckDNS, then DNS wait"
cd "$STACK_DIR"
docker compose up -d duckdns

PUBLIC_IP="$(curl -fsS --max-time 5 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address 2>/dev/null || true)"
[ -n "$PUBLIC_IP" ] || PUBLIC_IP="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
[ -n "$PUBLIC_IP" ] || die "could not determine this droplet's public IP"
info "droplet public IP: $PUBLIC_IP"

# Caddy's HTTP-01 challenge fails closed, and Let's Encrypt allows only 5 failed
# validations per hostname per hour. Confirm DNS before Caddy ever starts.
info "waiting for $DOMAIN to resolve to $PUBLIC_IP (up to 120s)"
resolved=""
for _ in $(seq 1 24); do
	resolved="$(dig +short "$DOMAIN" @1.1.1.1 | tail -n1)"
	[ "$resolved" = "$PUBLIC_IP" ] && break
	sleep 5
done
if [ "$resolved" != "$PUBLIC_IP" ]; then
	docker compose logs --tail 20 duckdns || true
	die "$DOMAIN resolves to '${resolved:-nothing}', not $PUBLIC_IP. Fix DuckDNS before continuing; starting Caddy now would burn Let's Encrypt rate limit attempts."
fi
info "DNS confirmed"

# --- 10. Start everything -------------------------------------------------
log "10/10 Starting the stack"
docker compose up -d

info "waiting for https://$DOMAIN/health (up to 180s; first run includes certificate issuance)"
healthy=0
for _ in $(seq 1 36); do
	if curl -fsS --max-time 5 "https://$DOMAIN/health" >/dev/null 2>&1; then
		healthy=1
		break
	fi
	sleep 5
done

docker compose ps

if [ "$healthy" -ne 1 ]; then
	echo
	echo "The stack is up but https://$DOMAIN/health did not answer in 180s."
	echo "Check:  cd $STACK_DIR && docker compose logs caddy medikeep-app --tail 50"
	exit 1
fi

cat <<EOF

================================================================
 MediKeep is live at https://$DOMAIN

   username: admin
   password: $ADMIN_DEFAULT_PASSWORD

 Change that password on first login. It is also stored in
 $STACK_DIR/.env and is printed here only once.

 JSON export (also runs nightly at 02:30 $TZ_NAME):
   $STACK_DIR/scripts/export-json.sh
   $STACK_DIR/data/exports/latest/dataset.json
================================================================
EOF
