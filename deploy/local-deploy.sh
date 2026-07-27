#!/usr/bin/env bash
#
# local-deploy.sh -- deploy a tagged release ON THIS JETSON.
#
#   sudo bash local-deploy.sh v1.0.9        # deploy, health-gate, auto-rollback
#   sudo bash local-deploy.sh --status      # what is this box running?
#   sudo bash local-deploy.sh --rollback    # back to the previous release
#
# For operators who work on the box itself -- remote desktop, console, or a
# keyboard in the field -- rather than pushing over ssh from a laptop.
#
# It gives the SAME guarantees as deploy/deploy.sh:
#   * the tree is staged first, so a failed copy never leaves a half-written
#     release directory that the service could start from;
#   * activation is an ATOMIC symlink swap, so the box is never running a
#     partially updated tree;
#   * the service must come back HEALTHY -- active, not restart-looping, and
#     logging a fresh heartbeat -- or it is rolled back automatically;
#   * it never touches /etc/deerkha/device.env, /var/lib/deerkha,
#     /opt/deerkha/models or the video storage root.
#
# deploy/deploy.sh is still the better tool once you have several boxes: it
# does one canary first, gates on health, and HALTS the rollout instead of
# breaking all seven forest sites at once. Use this for a single box you are
# sitting in front of; use that when the fleet grows.
#
# SOURCE OF THE CODE
#   A git clone on this box, default ~/deerkha-src. Override with DD_SRC.
#   Only files committed at the tag are deployed -- an edit you made in the
#   clone but did not commit is NOT picked up, deliberately: what runs on the
#   box must be a named, reproducible version.

set -uo pipefail

ROOT=/opt/deerkha
SVC=deerkha-drishti
SVC_USER=deerkha
# Default to the clone this script is IN -- it lives at <clone>/deploy/, so the
# parent of its own directory is the repository root. Deriving it from $HOME was
# wrong twice over: sudo resets HOME to /root, and the clone need not be in a
# home directory at all.
SRC="${DD_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
KEEP_RELEASES="${KEEP_RELEASES:-5}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"
LOG=/var/log/deerkha/deerkha.log

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
info() { printf '%s==>%s %s\n' "$BLD" "$RST" "$*"; }
ok()   { printf '  %sok%s   %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '  %swarn%s %s\n' "$YEL" "$RST" "$*"; }
err()  { printf '  %sFAIL%s %s\n' "$RED" "$RST" "$*" >&2; }

[ "$(id -u)" -eq 0 ] || { err "run with sudo"; exit 1; }

# ------------------------------------------------------------------ status --
do_status() {
  local cur rel
  cur="$(readlink -f "$ROOT/current" 2>/dev/null || echo '(none)')"
  rel="$(cat "$ROOT/current/RELEASE" 2>/dev/null || echo '(unknown)')"
  printf '  release   : %s\n' "$rel"
  printf '  path      : %s\n' "$cur"
  printf '  service   : %s\n' "$(systemctl is-active $SVC 2>/dev/null || echo dead)"
  printf '  restarts  : %s\n' "$(systemctl show -p NRestarts --value $SVC 2>/dev/null || echo '?')"
  printf '  available :\n'
  ls -1dt "$ROOT"/releases/*/ 2>/dev/null | sed 's|.*/releases/||; s|/$||' | sed 's/^/              /'
}

# ------------------------------------------------------------ health gate --
# Same three conditions deploy.sh uses. `systemctl is-active` alone is not
# enough: a crash-looping unit reports 'activating', or 'active' briefly between
# crashes, and would pass a naive check every time.
# Both failure paths must show WHY. A crash loop is the most likely failure on a
# first deploy and was previously the one that printed nothing at all.
dump_logs() {
  echo
  echo "  ---- journalctl -u $SVC (last 40) ----------------------------------"
  journalctl -u $SVC -n 40 --no-pager 2>/dev/null | sed 's/^/  | /'
  if [ -f "$LOG" ]; then
    echo "  ---- $LOG (last 40) ----"
    tail -40 "$LOG" 2>/dev/null | sed 's/^/  | /'
  else
    echo "  ---- $LOG does not exist: the process died before logging started ----"
  fi
  echo "  --------------------------------------------------------------------"
}

health_check() {
  local deadline=$(( SECONDS + HEALTH_TIMEOUT )) before
  before="$(systemctl show -p NRestarts --value $SVC 2>/dev/null || echo 0)"
  sleep 8
  while [ $SECONDS -lt $deadline ]; do
    local active restarts
    active="$(systemctl is-active $SVC 2>/dev/null || echo dead)"
    restarts="$(systemctl show -p NRestarts --value $SVC 2>/dev/null || echo 0)"
    if [ "$active" = "active" ]; then
      if [ "${restarts:-0}" -gt "${before:-0}" ]; then
        err "service is restart-looping (NRestarts ${before} -> ${restarts})"
        dump_logs
        return 1
      fi
      # A fresh heartbeat means the pipeline loaded its config, opened the
      # TensorRT engines and enumerated cameras -- i.e. the whole import chain
      # actually works, not just that systemd started a process.
      if [ -n "$(find "$LOG" -mmin -3 2>/dev/null | head -1)" ] \
         && tail -200 "$LOG" 2>/dev/null | grep -q 'HEARTBEAT'; then
        return 0
      fi
    fi
    sleep 6
  done
  err "did not become healthy within ${HEALTH_TIMEOUT}s"
  dump_logs
  return 1
}

# --------------------------------------------------------------- rollback --
rollback() {
  local prev
  # sed -n 2p = the second most recent, i.e. the one before what is live now.
  prev="$(ls -1dt "$ROOT"/releases/*/ 2>/dev/null | sed -n 2p | xargs -r basename)"
  if [ -z "$prev" ]; then
    warn "no previous release to roll back to -- expected on a FIRST deploy."
    warn "the service is left stopped rather than crash-looping; fix, then re-run."
    systemctl stop $SVC 2>/dev/null
    return 1
  fi
  warn "rolling back to $prev"
  ln -sfn "$ROOT/releases/$prev" "$ROOT/current.new" || return 1
  mv -Tf "$ROOT/current.new" "$ROOT/current" || return 1
  systemctl restart $SVC || return 1
  ok "rolled back to $prev"
}

case "${1:-}" in
  --status)   do_status; exit 0 ;;
  --rollback) rollback; exit $? ;;
  "")         err "usage: local-deploy.sh <tag> | --status | --rollback"; exit 2 ;;
esac
TAG="$1"

# ------------------------------------------------------------- preflight --
info "preflight"
[ -d "$SRC/.git" ] || { err "no git clone at $SRC (set DD_SRC=/path/to/clone)"; exit 1; }
if ! git -C "$SRC" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  err "tag '$TAG' not found in $SRC"
  err "fetch it first:  git -C $SRC fetch --tags --force"
  exit 1
fi
[ -f /etc/deerkha/device.env ] || { err "/etc/deerkha/device.env missing -- run provision.sh first"; exit 1; }
[ -d "$ROOT/releases" ] || { err "$ROOT/releases missing -- run provision.sh first"; exit 1; }
free="$(df --output=avail -BM "$ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')"
if [ -n "$free" ] && [ "$free" -lt 500 ]; then err "only ${free}MB free on $ROOT"; exit 1; fi
ok "tag $TAG, device.env present, ${free:-?}MB free"

# --------------------------------------------------------------- stage --
# Extract into a temp dir and move it into place, so an interrupted extraction
# cannot leave a half-written tree at releases/<tag> for systemd to start from.
info "staging $TAG"
stage="$ROOT/releases/.stage.$$"
rm -rf "$stage" && mkdir -p "$stage" || { err "could not create $stage"; exit 1; }
if ! git -C "$SRC" archive --format=tar "$TAG" edge/ | tar -x --strip-components=1 -C "$stage"; then
  err "export failed"; rm -rf "$stage"; exit 1
fi
echo "$TAG" > "$stage/RELEASE"
chown -R "$SVC_USER:$SVC_USER" "$stage"
ok "staged $(find "$stage" -type f | wc -l) files"

# -------------------------------------------------------------- activate --
info "activating"
rm -rf "${ROOT:?}/releases/$TAG"
mv -T "$stage" "$ROOT/releases/$TAG"      || { err "could not move into place"; exit 1; }
ln -sfn "$ROOT/releases/$TAG" "$ROOT/current.new" || { err "symlink failed"; exit 1; }
mv -Tf "$ROOT/current.new" "$ROOT/current"        || { err "atomic swap failed"; exit 1; }
systemctl restart $SVC                            || { err "restart failed"; rollback; exit 1; }
ok "activated $TAG, waiting for health..."

if health_check; then
  ok "healthy on $TAG"
  # Prune old releases, always keeping enough to roll back to.
  ls -1dt "$ROOT"/releases/*/ 2>/dev/null | tail -n +$((KEEP_RELEASES+1)) | xargs -r rm -rf
  echo
  do_status
  exit 0
fi

err "health gate failed"
rollback
exit 1
