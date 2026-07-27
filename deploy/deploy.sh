#!/usr/bin/env bash
#
# deploy.sh -- ship one tagged release to the whole Jetson fleet.
#
#   ./deploy/deploy.sh v1.4.0                    # canary, health gate, then the rest
#   ./deploy/deploy.sh v1.4.0 --only jetson-forest-03
#   ./deploy/deploy.sh v1.4.0 --no-canary        # all boxes, still health-gated
#   ./deploy/deploy.sh --status                  # what is each box running?
#   ./deploy/deploy.sh --rollback                # previous release, whole fleet
#   ./deploy/deploy.sh --rollback --only jetson-forest-03
#
# Design decisions worth knowing:
#
#  * CANARY BY DEFAULT. One box first, health-gated, then the rest. A bad
#    release reaching all seven forest sites at once is the failure this exists
#    to prevent -- every one of those is a truck roll.
#  * SEQUENTIAL, not parallel. Seven boxes is fast enough, and a failure stops
#    the rollout instead of racing ahead of it.
#  * ATOMIC SWAP. Code lands in releases/<tag>/, then a symlink is repointed
#    with `mv -T`. A box is never running a half-written tree.
#  * AUTO-ROLLBACK. If a box fails its health gate it is put back on the
#    previous release immediately and the fan-out halts.
#  * NEVER TOUCHES /etc/deerkha/device.env, /var/lib/deerkha, /opt/deerkha/models
#    or the video storage root.
#  * Uses `git archive | ssh | tar` rather than rsync: the office PC is Windows
#    and Git Bash ships ssh+tar but not rsync.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLEET_FILE="${FLEET_FILE:-$REPO_ROOT/deploy/fleet.txt}"
REMOTE_ROOT="/opt/deerkha"
KEEP_RELEASES="${KEEP_RELEASES:-5}"
# How long to wait for a box to come back healthy after restart.
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"
SSH_OPTS="-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
info() { printf '%s==>%s %s\n' "$BLD" "$RST" "$*"; }
ok()   { printf '  %sok%s   %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '  %swarn%s %s\n' "$YEL" "$RST" "$*"; }
err()  { printf '  %sFAIL%s %s\n' "$RED" "$RST" "$*" >&2; }

TAG=""; ONLY=""; MODE="deploy"; CANARY=1
while [ $# -gt 0 ]; do
  case "$1" in
    --status)    MODE="status" ;;
    --rollback)  MODE="rollback" ;;
    --only)      ONLY="${2:-}"; shift ;;
    --no-canary) CANARY=0 ;;
    -h|--help)   sed -n '2,30p' "$0"; exit 0 ;;
    v*|[0-9]*)   TAG="$1" ;;
    *)           err "unknown argument: $1"; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------- inventory --
declare -a IDS HOSTS USERS
while read -r id host user _rest; do
  [ -z "${id:-}" ] && continue
  case "$id" in \#*) continue ;; esac
  IDS+=("$id"); HOSTS+=("$host"); USERS+=("${user:-deerkha}")
done < "$FLEET_FILE"

if [ "${#IDS[@]}" -eq 0 ]; then
  err "no enabled boxes in $FLEET_FILE"; exit 2
fi

sshq() { # sshq <idx> <command...>
  local i="$1"; shift
  ssh $SSH_OPTS "${USERS[$i]}@${HOSTS[$i]}" "$@"
}

# ------------------------------------------------------------------ status --
remote_current() { sshq "$1" "readlink ${REMOTE_ROOT}/current 2>/dev/null | xargs -r basename" 2>/dev/null; }

do_status() {
  printf '%-22s %-16s %-14s %-10s %s\n' DEVICE HOST RELEASE SERVICE UPTIME
  for i in "${!IDS[@]}"; do
    [ -n "$ONLY" ] && [ "$ONLY" != "${IDS[$i]}" ] && continue
    local rel svc up
    rel="$(remote_current "$i")" || rel=""
    if [ -z "$rel" ]; then
      printf '%-22s %-16s %s%-14s%s %s\n' "${IDS[$i]}" "${HOSTS[$i]}" "$RED" "UNREACHABLE" "$RST" "-"
      continue
    fi
    svc="$(sshq "$i" "systemctl is-active deerkha-drishti 2>/dev/null" || echo unknown)"
    up="$(sshq "$i" "systemctl show -p ActiveEnterTimestamp --value deerkha-drishti 2>/dev/null" || echo '')"
    local colour="$GRN"; [ "$svc" = active ] || colour="$RED"
    printf '%-22s %-16s %-14s %s%-10s%s %s\n' \
      "${IDS[$i]}" "${HOSTS[$i]}" "$rel" "$colour" "$svc" "$RST" "$up"
  done
}

# ------------------------------------------------------------ health gate --
# Passes only if the service is active, has NOT been restarting in a loop, and
# has logged a fresh heartbeat. "systemctl is-active" alone is not enough: a
# crash-looping unit reports 'activating' or briefly 'active' between crashes.
health_check() {
  local i="$1" deadline=$(( SECONDS + HEALTH_TIMEOUT ))
  local restarts_before
  restarts_before="$(sshq "$i" "systemctl show -p NRestarts --value deerkha-drishti" 2>/dev/null || echo 0)"
  sleep 8
  while [ $SECONDS -lt $deadline ]; do
    local active restarts fresh
    active="$(sshq "$i" "systemctl is-active deerkha-drishti" 2>/dev/null || echo dead)"
    restarts="$(sshq "$i" "systemctl show -p NRestarts --value deerkha-drishti" 2>/dev/null || echo 0)"
    if [ "$active" = "active" ]; then
      if [ "${restarts:-0}" -gt "${restarts_before:-0}" ]; then
        err "service is restart-looping (NRestarts ${restarts_before} -> ${restarts})"
        return 1
      fi
      # A heartbeat line written in the last 3 minutes means the pipeline got
      # far enough to load its config and enumerate cameras -- i.e. the import
      # chain, the TensorRT engines and the config client all work.
      fresh="$(sshq "$i" "find /var/log/deerkha/deerkha.log -mmin -3 2>/dev/null | head -1")"
      if [ -n "$fresh" ] && sshq "$i" "tail -200 /var/log/deerkha/deerkha.log | grep -q 'HEARTBEAT'"; then
        return 0
      fi
    fi
    sleep 6
  done
  err "did not become healthy within ${HEALTH_TIMEOUT}s"
  sshq "$i" "journalctl -u deerkha-drishti -n 25 --no-pager" 2>/dev/null | sed 's/^/      | /'
  return 1
}

rollback_box() {
  local i="$1"
  local prev
  prev="$(sshq "$i" "ls -1dt ${REMOTE_ROOT}/releases/*/ 2>/dev/null | sed -n 2p | xargs -r basename")"
  if [ -z "$prev" ]; then err "no previous release to roll back to"; return 1; fi
  warn "rolling back to $prev"
  sshq "$i" "set -e
    ln -sfn ${REMOTE_ROOT}/releases/$prev ${REMOTE_ROOT}/current.new
    mv -Tf ${REMOTE_ROOT}/current.new ${REMOTE_ROOT}/current
    sudo systemctl restart deerkha-drishti" || return 1
  ok "rolled back to $prev"
}

# ---------------------------------------------------------------- deploy --
deploy_box() {
  local i="$1" tag="$2"
  info "${IDS[$i]} (${HOSTS[$i]}) <- $tag"

  # Preflight. Cheaper to fail here than half-way through a rollout.
  local free
  free="$(sshq "$i" "df --output=avail -BM ${REMOTE_ROOT} 2>/dev/null | tail -1 | tr -dc '0-9'")" || {
    err "unreachable over ssh"; return 1; }
  if [ -n "$free" ] && [ "$free" -lt 500 ]; then
    err "only ${free}MB free on ${REMOTE_ROOT}"; return 1
  fi
  if ! sshq "$i" "test -f /etc/deerkha/device.env"; then
    err "/etc/deerkha/device.env missing -- run provision.sh on this box first"; return 1
  fi

  # Ship the tagged tree into a temp dir, then move it into place. Extracting
  # straight into releases/<tag> would leave a half-written tree there if the
  # transfer died mid-way.
  local stage="${REMOTE_ROOT}/releases/.stage.$$"
  sshq "$i" "rm -rf $stage && mkdir -p $stage" || { err "could not stage"; return 1; }
  if ! git -C "$REPO_ROOT" archive --format=tar "$tag" edge/ \
        | sshq "$i" "tar -x --strip-components=1 -C $stage"; then
    err "transfer failed"; sshq "$i" "rm -rf $stage"; return 1
  fi
  sshq "$i" "echo '$tag' > $stage/RELEASE"

  sshq "$i" "set -e
    rm -rf ${REMOTE_ROOT}/releases/$tag
    mv -T $stage ${REMOTE_ROOT}/releases/$tag
    ln -sfn ${REMOTE_ROOT}/releases/$tag ${REMOTE_ROOT}/current.new
    mv -Tf ${REMOTE_ROOT}/current.new ${REMOTE_ROOT}/current
    sudo systemctl restart deerkha-drishti" || { err "activation failed"; return 1; }
  ok "activated $tag, waiting for health..."

  if health_check "$i"; then
    ok "healthy"
    # Prune old releases, always keeping the one we could roll back to.
    sshq "$i" "ls -1dt ${REMOTE_ROOT}/releases/*/ 2>/dev/null | tail -n +$((KEEP_RELEASES+1)) | xargs -r rm -rf" || true
    return 0
  fi
  rollback_box "$i"
  return 1
}

# ------------------------------------------------------------------- main --
case "$MODE" in
  status) do_status; exit 0 ;;
  rollback)
    rc=0
    for i in "${!IDS[@]}"; do
      [ -n "$ONLY" ] && [ "$ONLY" != "${IDS[$i]}" ] && continue
      info "${IDS[$i]}"; rollback_box "$i" || rc=1
    done
    exit $rc ;;
esac

if [ -z "$TAG" ]; then err "usage: deploy.sh <tag> | --status | --rollback"; exit 2; fi
if ! git -C "$REPO_ROOT" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  err "'$TAG' is not a git tag. Deploys are always from a tag so every box runs"
  err "a named, reproducible version:  git tag -a $TAG -m 'reason' && git push --tags"
  exit 2
fi

declare -a ORDER=()
for i in "${!IDS[@]}"; do
  [ -n "$ONLY" ] && [ "$ONLY" != "${IDS[$i]}" ] && continue
  ORDER+=("$i")
done
if [ "${#ORDER[@]}" -eq 0 ]; then err "no boxes matched --only $ONLY"; exit 2; fi

declare -a OKS=() BAD=() SKIPPED=()
first=1
for i in "${ORDER[@]}"; do
  if deploy_box "$i" "$TAG"; then
    OKS+=("${IDS[$i]}")
  else
    BAD+=("${IDS[$i]}")
    err "halting rollout -- remaining boxes untouched"
    break
  fi
  if [ $first -eq 1 ] && [ $CANARY -eq 1 ] && [ "${#ORDER[@]}" -gt 1 ]; then
    info "canary ${IDS[$i]} healthy on $TAG -- continuing to ${#ORDER[@]} total"
    first=0
  fi
done

for i in "${ORDER[@]}"; do
  id="${IDS[$i]}"
  case " ${OKS[*]-} ${BAD[*]-} " in *" $id "*) ;; *) SKIPPED+=("$id") ;; esac
done

echo
info "summary for $TAG"
[ "${#OKS[@]}"     -gt 0 ] && printf '  %sdeployed%s  %s\n' "$GRN" "$RST" "${OKS[*]}"
[ "${#BAD[@]}"     -gt 0 ] && printf '  %sfailed%s    %s (rolled back)\n' "$RED" "$RST" "${BAD[*]}"
[ "${#SKIPPED[@]}" -gt 0 ] && printf '  %sskipped%s   %s\n' "$YEL" "$RST" "${SKIPPED[*]}"
echo "  run './deploy/deploy.sh --status' to confirm."
[ "${#BAD[@]}" -eq 0 ] && [ "${#SKIPPED[@]}" -eq 0 ]
