#!/usr/bin/env bash
#
# push-models.sh -- ship TensorRT engines and deterrence sounds to the fleet.
#
#   ./deploy/push-models.sh                 # all boxes in fleet.txt
#   ./deploy/push-models.sh jetson-forest-03
#
# Separate from deploy.sh on purpose:
#
#  * These files are large and change rarely -- shipping them on every code
#    deploy would make routine updates slow over a field link.
#  * They are NOT in git (see .gitignore). TensorRT engines are not portable
#    across GPU architecture or TensorRT/JetPack version, so they are BUILT for
#    the fleet rather than version-controlled. The Jetson Nano's Maxwell engines
#    will NOT load on Orin -- they must be rebuilt on a reference Orin box.
#  * They live in /opt/deerkha/models, outside the per-release tree, so a code
#    deploy never disturbs them.
#
# Because every box in this fleet is the same SKU and JetPack version, ONE
# engine build serves all of them. If that ever stops being true, this script
# needs a per-variant source directory.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLEET_FILE="${FLEET_FILE:-$REPO_ROOT/deploy/fleet.txt}"
MODELS_SRC="${MODELS_SRC:-$REPO_ROOT/models}"
SOUNDS_SRC="${SOUNDS_SRC:-$REPO_ROOT/sounds}"
REMOTE_ROOT="/opt/deerkha"
SSH_OPTS="-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes"

ONLY="${1:-}"

if [ ! -d "$MODELS_SRC" ]; then
  echo "No $MODELS_SRC directory."
  echo
  echo "Build the engines on a reference Orin box of the SAME SKU and JetPack,"
  echo "then copy them here as:"
  echo "    models/rfdetr_fp16.engine"
  echo "    models/mobileclip2_s2_image_fp16.engine"
  echo "    models/pt/mobileclip2_s2.pt"
  echo
  echo "Engines built for a different GPU architecture will fail to deserialize"
  echo "at import, and the pipeline will crash-loop on every box you send them to."
  exit 1
fi

fail=0
while read -r id host user _rest; do
  [ -z "${id:-}" ] && continue
  case "$id" in \#*) continue ;; esac
  [ -n "$ONLY" ] && [ "$ONLY" != "$id" ] && continue
  user="${user:-deerkha}"

  echo "==> $id ($host)"
  if ! ssh $SSH_OPTS "$user@$host" "mkdir -p $REMOTE_ROOT/models $REMOTE_ROOT/sounds"; then
    echo "    FAIL: unreachable"; fail=1; continue
  fi

  if ! tar -C "$MODELS_SRC" -cf - . | ssh $SSH_OPTS "$user@$host" "tar -xf - -C $REMOTE_ROOT/models"; then
    echo "    FAIL: model transfer"; fail=1; continue
  fi
  echo "    models pushed"

  if [ -d "$SOUNDS_SRC" ]; then
    if tar -C "$SOUNDS_SRC" -cf - . | ssh $SSH_OPTS "$user@$host" "tar -xf - -C $REMOTE_ROOT/sounds"; then
      echo "    sounds pushed"
    else
      echo "    WARN: sound transfer failed (deterrence audio will be silent)"
    fi
  fi

  ssh $SSH_OPTS "$user@$host" "ls -la $REMOTE_ROOT/models" | sed 's/^/    /'
done < "$FLEET_FILE"

echo
if [ $fail -eq 0 ]; then
  echo "Done. Restart affected boxes to load new engines:"
  echo "  ./deploy/deploy.sh --status"
else
  echo "One or more boxes failed -- see above."
fi
exit $fail
