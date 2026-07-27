#!/usr/bin/env bash
#
# backup.sh -- nightly Postgres dump, retention, and a real restore test.
#
#   ./backup.sh                  # dump + prune old dumps (what the timer runs)
#   ./backup.sh --test-restore   # restore the newest dump into a scratch DB and
#                                # verify it, then drop it
#   ./backup.sh --list
#
# Moving off a hosted database means nobody else is taking backups any more.
# That is the real cost of self-hosting, and it is paid here.
#
# OFFSITE: the office PC is already on the tailnet and already owned, so it is
# a free second location. Pull from there rather than pushing from here -- a
# compromised or wiped VPS then cannot destroy its own backups:
#
#   scp deerkha@deerkha-vps:/var/backups/deerkha/latest.dump D:\deerkha-backups\
#
# Scale note: the database is well under a gigabyte and compresses hard, so
# keeping many dumps is cheap. Revisit if detections ever grows past a few GB.

set -euo pipefail

DB_NAME="${DB_NAME:-deerkha}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/deerkha}"
KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"

STAMP="$(date -u +%Y%m%d-%H%M%S)"
DOW="$(date -u +%u)"     # 7 = Sunday
OUT="${BACKUP_DIR}/deerkha-${STAMP}.dump"

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }

mkdir -p "$BACKUP_DIR"

case "${1:-}" in
  --list)
    ls -lh "$BACKUP_DIR"/*.dump 2>/dev/null || echo "no dumps yet"
    exit 0
    ;;

  --test-restore)
    # An untested backup is not a backup. This restores the newest dump into a
    # throwaway database and checks the tables are actually populated -- which
    # catches the failure that matters: a dump that runs nightly, exits 0, and
    # contains nothing useful.
    latest="$(ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null | head -1)"
    [ -n "$latest" ] || { echo "no dump to test"; exit 1; }
    scratch="deerkha_restoretest_$$"
    log "restoring $(basename "$latest") into $scratch"
    trap 'sudo -u postgres dropdb --if-exists "$scratch" 2>/dev/null || true' EXIT

    sudo -u postgres createdb "$scratch"
    sudo -u postgres pg_restore --no-owner --no-privileges -d "$scratch" "$latest" \
        || log "pg_restore reported warnings (often benign for ownership)"

    fail=0
    for t in cfg_devices cfg_cameras cfg_global_settings; do
        n="$(sudo -u postgres psql -tAc "SELECT count(*) FROM $t" "$scratch" 2>/dev/null || echo ERR)"
        printf '  %-24s %s\n' "$t" "$n"
        [ "$n" = "ERR" ] && { echo "    ^ MISSING"; fail=1; }
    done
    n="$(sudo -u postgres psql -tAc "SELECT count(*) FROM detections" "$scratch" 2>/dev/null || echo ERR)"
    printf '  %-24s %s\n' detections "$n"
    [ "$n" = "ERR" ] && { echo "    ^ MISSING -- detections did not survive the dump"; fail=1; }

    devs="$(sudo -u postgres psql -tAc "SELECT count(*) FROM cfg_devices" "$scratch" 2>/dev/null || echo 0)"
    if [ "${devs:-0}" -lt 1 ]; then
        echo "  !! cfg_devices is EMPTY -- this backup would not restore your fleet"
        fail=1
    fi

    if [ $fail -eq 0 ]; then
        log "RESTORE TEST PASSED"
    else
        log "RESTORE TEST FAILED -- fix this before you need it"
    fi
    exit $fail
    ;;
esac

# ------------------------------------------------------------------- dump --
log "dumping ${DB_NAME}"
# -Fc: compressed custom format, restorable selectively with pg_restore.
sudo -u postgres pg_dump -Fc --no-owner --no-privileges "$DB_NAME" -f "$OUT"
chmod 640 "$OUT"
ln -sfn "$OUT" "${BACKUP_DIR}/latest.dump"
log "wrote $(du -h "$OUT" | cut -f1) -> $(basename "$OUT")"

# A zero-byte or absurdly small dump means something went wrong in a way
# pg_dump's exit code did not report.
size=$(stat -c%s "$OUT")
if [ "$size" -lt 10240 ]; then
    log "WARNING: dump is only ${size} bytes -- that is almost certainly broken"
    exit 1
fi

# -------------------------------------------------------------- retention --
# Keep the last N daily, plus Sunday dumps for N weeks.
log "pruning"
mapfile -t dailies < <(ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null | grep -v latest || true)
kept=0
for f in "${dailies[@]}"; do
    kept=$((kept + 1))
    [ $kept -le "$KEEP_DAILY" ] && continue
    # Keep Sundays up to KEEP_WEEKLY weeks back.
    fdate="$(basename "$f" | sed -E 's/deerkha-([0-9]{8})-.*/\1/')"
    if [ "$(date -u -d "$fdate" +%u 2>/dev/null || echo 0)" = "7" ] \
       && [ "$(( ( $(date -u +%s) - $(date -u -d "$fdate" +%s) ) / 604800 ))" -lt "$KEEP_WEEKLY" ]; then
        continue
    fi
    rm -f "$f"
    log "  removed $(basename "$f")"
done

log "done; $(ls -1 "$BACKUP_DIR"/*.dump 2>/dev/null | grep -vc latest || echo 0) dump(s) retained"
