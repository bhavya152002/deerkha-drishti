#!/usr/bin/env bash
#
# install-server.sh -- build a fresh Ubuntu VPS into the Deerkha Drishti office
# server: Postgres, the app, systemd units, firewall, backups.
#
# Run ON the VPS as root, once:
#     sudo bash install-server.sh
#
# It is idempotent: safe to re-run after fixing something.
#
# It does NOT migrate data and does NOT start the app -- both need decisions
# only you can make (see the checklist it prints at the end).

set -euo pipefail

APP_USER=deerkha
APP_ROOT=/opt/deerkha-server
STATE=/var/lib/deerkha-server
CONF=/etc/deerkha-server
BACKUPS=/var/backups/deerkha
DB_NAME=deerkha
DB_USER=deerkha
DB_EDGE_USER=deerkha_edge
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- timezone --
say "timezone"
# MUST happen before Postgres is installed. initdb bakes the system timezone
# into postgresql.conf, and the edge inserts NAIVE datetimes
# (edge/main.py: datetime.utcnow()) which Postgres then interprets in the
# session timezone. A cluster created under Asia/Kolkata puts every new
# detection 5h30m ahead of all migrated history -- interleaved by the
# dashboard's ORDER BY, and invisible for weeks.
timedatectl set-timezone UTC
timedatectl show -p Timezone --value

# -------------------------------------------------------------- base system --
say "base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    postgresql postgresql-contrib \
    python3-venv python3-dev build-essential \
    nginx certbot python3-certbot-nginx \
    git curl ufw unattended-upgrades zstd jq

say "swap (small box + Postgres; prevents the OOM killer picking a side)"
if ! swapon --show | grep -q .; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap -q /swapfile
    swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

say "journald size cap"
# Unbounded journals are the classic way a small VPS fills its disk months
# later. The app logs to journald, so cap it here rather than discovering it.
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/size.conf <<'EOF'
[Journal]
SystemMaxUse=500M
SystemMaxFileSize=50M
MaxRetentionSec=1month
EOF
systemctl restart systemd-journald

say "unattended security upgrades"
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

# ------------------------------------------------------------------ firewall --
say "firewall"
# NOTE: ufw does NOT filter tailnet traffic. tailscaled installs a ts-input
# iptables chain, jumped from INPUT, that ACCEPTs -s 100.64.0.0/10 -i tailscale0
# BEFORE ufw's rules are consulted. So `ufw deny 5432` will not stop a tailnet
# peer from reaching Postgres. Segmenting inside the tailnet is a Tailscale ACL
# job, not a ufw job. ufw here is purely about the PUBLIC interface.
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
# nginx terminates TLS for the dashboard on 80/443 and proxies to 127.0.0.1:8000.
ufw allow 'Nginx Full'
# Deliberately NOT opened: 8000 (the app itself) and 5432 (Postgres). The app
# binds 0.0.0.0 so Jetsons can reach it over Tailscale, and ufw is the only
# thing keeping that off the public internet -- see the check at the end.
# Postgres reaches the tailnet the same way. Devices arrive via Tailscale;
# browsers arrive via nginx. Nothing else gets in.
ufw --force enable
ufw status verbose

say "ssh hardening"
# The public IP means SSH is the one service the internet can actually see.
# Key-only auth is the single highest-value hardening step; do not skip it
# without first confirming your key works, or you will lock yourself out.
if grep -q '^\s*PasswordAuthentication\s\+yes' /etc/ssh/sshd_config 2>/dev/null \
   || ! grep -q '^\s*PasswordAuthentication' /etc/ssh/sshd_config 2>/dev/null; then
    # Check EVERY home directory, not just root and the service account. The
    # admin user you actually log in as (deerkha-admin in the guide) is neither
    # of those, so a narrower check finds no key, skips the hardening, and
    # leaves password auth on while reporting nothing useful.
    if [ -s /root/.ssh/authorized_keys ] || \
       compgen -G "/home/*/.ssh/authorized_keys" >/dev/null 2>&1; then
        sed -i 's/^\s*#\?\s*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
        systemctl reload ssh 2>/dev/null || systemctl reload sshd
        echo "    password auth disabled (an authorized_keys file was found)"
    else
        echo "    !! NO authorized_keys found -- leaving password auth ENABLED."
        echo "       Add your key, then: sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config"
    fi
fi

# ------------------------------------------------------------------ postgres --
say "postgres tuning (2 vCPU / 8 GB / NVMe)"
PGVER="$(psql --version | grep -oE '[0-9]+' | head -1)"
PGCONF="/etc/postgresql/${PGVER}/main"
echo "    detected PostgreSQL ${PGVER} at ${PGCONF}"

cat > "${PGCONF}/conf.d/10-deerkha.conf" <<'EOF'
# Deerkha Drishti tuning. 2 vCPU / 8 GB RAM / NVMe.
# The whole working set fits in RAM many times over, so this is mostly about
# telling the planner the truth about the storage.

listen_addresses = 'localhost'      # plus the tailnet address, added by install-server.sh
max_connections = 100               # local connections are cheap; no hosted ceiling any more

shared_buffers = 2GB
effective_cache_size = 6GB
maintenance_work_mem = 512MB
work_mem = 16MB

# NVMe: random reads cost about the same as sequential, and it can service
# many concurrent requests. Leaving these at spinning-disk defaults makes the
# planner avoid index scans it should be choosing.
random_page_cost = 1.1
effective_io_concurrency = 200

wal_compression = on
checkpoint_completion_target = 0.9

# Pinned, belt-and-braces alongside the system timezone above. The edge sends
# naive UTC datetimes; if this is ever anything but UTC, detection history
# silently shifts.
timezone = 'UTC'
log_timezone = 'UTC'

log_min_duration_statement = 1000   # anything over 1s is worth seeing
EOF

install -d "${PGCONF}/conf.d"
grep -q "conf.d" "${PGCONF}/postgresql.conf" || \
    echo "include_dir = 'conf.d'" >> "${PGCONF}/postgresql.conf"

# Bind to the tailnet address too, so field devices can insert detections.
TSIP="$(tailscale ip -4 2>/dev/null || true)"
if [ -n "$TSIP" ]; then
    sed -i "s|^listen_addresses.*|listen_addresses = 'localhost,${TSIP}'|" \
        "${PGCONF}/conf.d/10-deerkha.conf"
    # scram-sha-256 over the tailnet, which is already WireGuard-encrypted.
    grep -q "deerkha tailnet" "${PGCONF}/pg_hba.conf" || cat >> "${PGCONF}/pg_hba.conf" <<EOF

# deerkha tailnet -- field devices inserting detections. Tailscale ACLs are
# what actually restrict WHICH peers can reach this port; see §1.4 of the plan.
host    ${DB_NAME}    ${DB_EDGE_USER}    100.64.0.0/10    scram-sha-256
EOF
else
    echo "    !! Tailscale not up yet -- Postgres bound to localhost only."
    echo "       Run 'tailscale up' then re-run this script to add the tailnet bind."
fi

systemctl restart postgresql
systemctl enable postgresql

say "database and roles"
# Regenerating these on a re-run would silently break a working server.env, so
# only mint passwords the first time.
if [ -f "$CONF/server.env" ] && grep -q '^DATABASE_URL=postgresql' "$CONF/server.env"; then
    echo "    server.env already has a DATABASE_URL -- keeping existing passwords"
    DB_PW=""
    EDGE_PW=""
else
    DB_PW="$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)"
    EDGE_PW="$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)"
fi

sudo -u postgres psql -v ON_ERROR_STOP=1 <<EOF
SELECT 'CREATE DATABASE ${DB_NAME}'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${DB_NAME}')\gexec

DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PW}';
  ${DB_PW:+ELSE
    ALTER ROLE ${DB_USER} PASSWORD '${DB_PW}';}
  END IF;
  -- Least-privilege role for the FIELD DEVICES. Without it every Jetson would
  -- carry a credential with full access to the whole database, so a stolen box
  -- would be a total compromise. This role can do exactly one thing: append
  -- detections. The GRANT happens below, after the schema exists.
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_EDGE_USER}') THEN
    CREATE ROLE ${DB_EDGE_USER} LOGIN PASSWORD '${EDGE_PW}';
  ${EDGE_PW:+ELSE
    ALTER ROLE ${DB_EDGE_USER} PASSWORD '${EDGE_PW}';}
  END IF;
END \$\$;

ALTER DATABASE ${DB_NAME} OWNER TO ${DB_USER};
EOF

say "schema"
# Applying the schema here rather than leaving it as a manual step: it is
# idempotent, and a server whose database has no tables fails in confusing ways
# (healthz is green because Postgres answers, but every request 500s).
if [ -f "$REPO_DIR/server/schema.sql" ]; then
    # Feed the file on STDIN rather than with psql -f.
    #
    # -f makes the POSTGRES user open the path, and the repo is normally cloned
    # into a home directory -- /root/deerkha when you run this as root, which is
    # mode 0700. postgres cannot traverse it, and psql fails with a bare
    # "Permission denied" that reads like a database problem rather than a
    # filesystem one. With a redirect, THIS shell (already root) opens the file
    # and psql just reads stdin, so the repo's location and permissions stop
    # mattering.
    #
    # No `&& echo` here either: under `set -e` a failure in the non-final part
    # of an && list does NOT abort the script, so the original form swallowed
    # this error and carried on to GRANT against tables that did not exist.
    if ! sudo -u postgres psql -v ON_ERROR_STOP=1 -d "$DB_NAME" \
            < "$REPO_DIR/server/schema.sql" >/dev/null; then
        echo "    !! schema failed to apply -- stopping before anything depends on it."
        exit 1
    fi
    echo "    schema applied"
    # Ownership of anything schema.sql created, then the edge's single privilege.
    sudo -u postgres psql -v ON_ERROR_STOP=1 -d "$DB_NAME" <<EOF >/dev/null
DO \$\$ DECLARE r record; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO ${DB_USER}', r.tablename);
  END LOOP;
  FOR r IN SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema='public' LOOP
    EXECUTE format('ALTER SEQUENCE public.%I OWNER TO ${DB_USER}', r.sequence_name);
  END LOOP;
END \$\$;
GRANT CONNECT ON DATABASE ${DB_NAME} TO ${DB_EDGE_USER};
GRANT USAGE ON SCHEMA public TO ${DB_EDGE_USER};
GRANT INSERT ON detections TO ${DB_EDGE_USER};
GRANT USAGE, SELECT ON SEQUENCE detections_id_seq TO ${DB_EDGE_USER};
EOF
    echo "    ${DB_EDGE_USER} granted INSERT on detections and nothing else"
else
    echo "    !! $REPO_DIR/server/schema.sql not found -- apply it by hand:"
    echo "       sudo -u postgres psql -d ${DB_NAME} < server/schema.sql"
fi

# ------------------------------------------------------------------- app user --
say "app user and layout"
id -u "$APP_USER" >/dev/null 2>&1 || \
    useradd --system --create-home --home-dir "/home/$APP_USER" --shell /bin/bash "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_ROOT" "$APP_ROOT/releases" "$STATE" "$BACKUPS"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$CONF"

say "python venv"
if [ ! -x "$APP_ROOT/venv/bin/python" ]; then
    python3 -m venv "$APP_ROOT/venv"
fi
"$APP_ROOT/venv/bin/pip" install -q --upgrade pip
if [ -f "$REPO_DIR/server/requirements.txt" ]; then
    "$APP_ROOT/venv/bin/pip" install -q -r "$REPO_DIR/server/requirements.txt"
fi
chown -R "$APP_USER:$APP_USER" "$APP_ROOT/venv"

# ---------------------------------------------------------------- env file --
say "server.env"
if [ ! -f "$CONF/server.env" ]; then
    cat > "$CONF/server.env" <<EOF
# Deerkha Drishti office server. Created by install-server.sh.
# The app REFUSES TO START if any of the first four are missing.

DATABASE_URL=postgresql://${DB_USER}:${DB_PW}@127.0.0.1:5432/${DB_NAME}

# IMPORTANT: paste the EXISTING SESSION_SECRET from the old server.
# Generating a fresh one logs everyone out mid-cutover.
SESSION_SECRET=CHANGE_ME
ADMIN_USERNAME=CHANGE_ME
ADMIN_PASSWORD=CHANGE_ME

JETSON_STREAM_PORT=8090
DD_SERVER_DATA_DIR=${STATE}

LIVE_TILE_W=480
LIVE_TILE_Q=55
LIVE_SNAPSHOT_TTL=2.0
LIVE_MAX_STREAMS=4
EOF
    chmod 640 "$CONF/server.env"
    chown root:"$APP_USER" "$CONF/server.env"
    echo "    created $CONF/server.env -- FILL IN the CHANGE_ME values"
else
    echo "    $CONF/server.env exists, left untouched"
    echo "    (new DB password NOT written; update DATABASE_URL by hand if you rotated it)"
fi

# Stash the edge credential separately -- it goes into each Jetson's device.env.
cat > "$CONF/edge-db-credential.txt" <<EOF
# For each Jetson's /etc/deerkha/device.env -- replaces the old Supabase URL.
# Reaches Postgres over Tailscale. This role can ONLY insert into detections.
DATABASE_URL=postgresql://${DB_EDGE_USER}:${EDGE_PW}@${TSIP:-<VPS_TAILSCALE_IP>}:5432/${DB_NAME}
EOF
chmod 600 "$CONF/edge-db-credential.txt"

# ------------------------------------------------------------------- systemd --
say "nginx"
UNIT_SRC="$(dirname "${BASH_SOURCE[0]}")"
if [ -f "$UNIT_SRC/nginx-deerkha-proxy.conf" ]; then
    install -m 644 "$UNIT_SRC/nginx-deerkha-proxy.conf" /etc/nginx/deerkha-proxy.conf
fi
if [ -f "$UNIT_SRC/nginx-deerkha.conf" ] && [ ! -f /etc/nginx/sites-available/deerkha ]; then
    install -m 644 "$UNIT_SRC/nginx-deerkha.conf" /etc/nginx/sites-available/deerkha
    echo "    installed /etc/nginx/sites-available/deerkha"
    echo "    NOT enabled yet -- it still says example.com. Edit the two server_name"
    echo "    lines, then:  ln -s /etc/nginx/sites-available/deerkha /etc/nginx/sites-enabled/"
else
    echo "    /etc/nginx/sites-available/deerkha already exists, left untouched"
fi
# Ubuntu's default vhost answers on port 80 for any unmatched Host header and
# would otherwise serve the nginx welcome page from this box's public IP.
rm -f /etc/nginx/sites-enabled/default
nginx -t 2>&1 | sed 's/^/    /' || true

say "systemd units"
for u in deerkha-server.service deerkha-retention.service deerkha-retention.timer deerkha-backup.service deerkha-backup.timer; do
    [ -f "$UNIT_SRC/$u" ] && install -m 644 "$UNIT_SRC/$u" "/etc/systemd/system/$u"
done
[ -f "$UNIT_SRC/backup.sh" ] && install -m 755 -o "$APP_USER" -g "$APP_USER" "$UNIT_SRC/backup.sh" "$APP_ROOT/backup.sh"
systemctl daemon-reload
systemctl enable deerkha-retention.timer deerkha-backup.timer
echo "    units installed; app NOT started yet (no code + no data yet)"

cat <<EOF

────────────────────────────────────────────────────────────────────────
Base build complete. Postgres ${PGVER} is running and tuned.

Credentials written:
  ${CONF}/server.env                 (app; fill in the CHANGE_ME values)
  ${CONF}/edge-db-credential.txt     (for each Jetson's device.env)

Postgres, the schema, nginx and the systemd units are all in place.
The app is NOT started -- it has no code and no admin password yet.

NEXT, in this order:

 1. Set the admin credentials. install-server.sh wrote CHANGE_ME, and the app
    will happily boot with the literal password "CHANGE_ME":
        python3 -c "import secrets; print(secrets.token_urlsafe(48))"
        sudo nano ${CONF}/server.env      # SESSION_SECRET, ADMIN_USERNAME, ADMIN_PASSWORD
    These four values are REQUIRED -- the app refuses to start without them.

 2. Deploy the code to ${APP_ROOT}/releases/<tag>, point ${APP_ROOT}/current
    at it, then:
        systemctl enable --now deerkha-server
        curl -s localhost:8000/healthz     # expect {"ok":true,"db":"ok"}

 3. Point DNS at this box, edit the two server_name lines, enable the vhost,
    and get certificates:
        sudo nano /etc/nginx/sites-available/deerkha
        sudo ln -s /etc/nginx/sites-available/deerkha /etc/nginx/sites-enabled/
        sudo certbot --nginx -d admin.<domain> -d app.<domain>
        sudo nginx -t && sudo systemctl reload nginx

 4. Seed your first device and note the token it prints:
        cd ${APP_ROOT}/current && ../venv/bin/python seed.py \\
            --device-id jetson-forest-01 --name "Forest Site 01"

 5. Prove a restore works before calling this done:
        ${APP_ROOT}/backup.sh --test-restore

 6. Confirm the two front doors, and that nothing else is open. The app binds
    0.0.0.0 so Jetsons can reach it over Tailscale; ufw is the only thing
    keeping it off the public internet.

    From the VPS:
        ss -ltnp | grep -E ':(8000|5432)'      # both should be listening
    From a Jetson (must SUCCEED):
        curl -s http://${TSIP:-<vps-tailscale-ip>}:8000/healthz
    From anywhere else (must TIME OUT, not connect):
        curl --connect-timeout 5 http://<public-ip>:8000/healthz
        curl --connect-timeout 5 http://<public-ip>:5432

    If that third check connects, stop and fix ufw before repointing devices.
────────────────────────────────────────────────────────────────────────
EOF
