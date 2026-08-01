#!/usr/bin/env bash
#
# provision.sh -- prepare ONE fresh Jetson Orin Nano to join the fleet.
# Run once per box, ON the box, as a sudo-capable user:
#
#   scp deploy/provision.sh deerkha@<tailscale-ip>:/tmp/
#   ssh deerkha@<tailscale-ip> 'sudo bash /tmp/provision.sh'
#
# After this, the box has the directory layout, service user, venv and systemd
# unit -- but NO code and NO identity yet. Then, from the office PC:
#
#   1. write /etc/deerkha/device.env on the box (device id + token + server URL)
#   2. ./deploy/push-models.sh <device_id>      # TensorRT engines + sounds
#   3. ./deploy/deploy.sh <tag> --only <device_id>
#
# Everything this script creates is IDENTICAL on every box. The only per-device
# artefacts are /etc/deerkha/device.env and the models built for this SKU.

set -euo pipefail

ROOT=/opt/deerkha
STATE=/var/lib/deerkha
LOGS=/var/log/deerkha
STORAGE="${STORAGE_ROOT:-/mnt/data/video_storage}"
SVC_USER=deerkha

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

echo "==> service user"
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /home/$SVC_USER --shell /bin/bash "$SVC_USER"
fi
# video/render: NVDEC + GPU access. dialout: the USB relay on /dev/ttyUSB0.
# audio: deterrence playback. gpio: /dev/gpiochip* is root:gpio on JetPack, so
# WITHOUT it Jetson.GPIO's setup() raises PermissionError as this user and the
# GPIO relay silently never fires -- while the USB relay keeps working, which
# makes the box look healthy. Without these the pipeline starts and then fails
# in confusing ways at the first camera or the first deterrence trigger.
#
# The gpio group is created by the jetson-gpio package's postinstall, which may
# not have run on a stock image, so create it first rather than let usermod fail.
groupadd -f gpio
usermod -aG video,render,dialout,audio,gpio "$SVC_USER"

echo "==> ssh access for the service user"
# deploy.sh connects to this box AS $SVC_USER with BatchMode=yes, so no password
# prompt is possible. useradd creates the account with no authorized_keys, so
# without this step provisioning LOOKS successful and the first deploy fails
# with "Permission denied (publickey)" against a box that is otherwise fine.
#
# Keys come from DEPLOY_PUBKEY if set, else from whoever is running this script
# -- you reached this box over ssh, so that key is the one that should work.
SSH_DIR="/home/$SVC_USER/.ssh"
AUTH="$SSH_DIR/authorized_keys"
install -d -m 700 -o "$SVC_USER" -g "$SVC_USER" "$SSH_DIR"
touch "$AUTH"
if [ -n "${DEPLOY_PUBKEY:-}" ]; then
  grep -qxF "$DEPLOY_PUBKEY" "$AUTH" 2>/dev/null || echo "$DEPLOY_PUBKEY" >> "$AUTH"
fi
for _src in "/home/${SUDO_USER:-__none__}/.ssh/authorized_keys" /root/.ssh/authorized_keys; do
  [ -s "$_src" ] || continue
  while IFS= read -r _k; do
    [ -n "$_k" ] || continue
    case "$_k" in \#*) continue ;; esac
    grep -qxF "$_k" "$AUTH" 2>/dev/null || echo "$_k" >> "$AUTH"
  done < "$_src"
done
chown "$SVC_USER:$SVC_USER" "$AUTH"
chmod 600 "$AUTH"
if [ -s "$AUTH" ]; then
  echo "    $(grep -c . "$AUTH") key(s) authorised for $SVC_USER"
else
  echo "    no keys installed -- fine if you deploy ON this box with"
  echo "    deploy/local-deploy.sh. Only the ssh-based deploy/deploy.sh needs a key."
  echo "    To enable that later:  ssh-copy-id $SVC_USER@<tailscale-ip>"
fi

echo "==> directories"
install -d -o "$SVC_USER" -g "$SVC_USER" "$ROOT" "$ROOT/releases" "$ROOT/models" "$ROOT/sounds"
install -d -o "$SVC_USER" -g "$SVC_USER" "$STATE" "$LOGS"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 /etc/deerkha
if [ ! -d "$STORAGE" ]; then
  echo "    NOTE: $STORAGE does not exist yet."
  echo "    Mount the NVMe there BEFORE first run -- recording to the eMMC/SD"
  echo "    card will fill it and wear it out within a year."
  install -d -o "$SVC_USER" -g "$SVC_USER" "$STORAGE"
else
  chown "$SVC_USER:$SVC_USER" "$STORAGE"
fi

echo "==> python venv"
if [ ! -x "$ROOT/venv/bin/python3" ]; then
  # A single broken third-party repo makes `apt-get update` exit non-zero even
  # though every repo we actually need updated fine. Field boxes accumulate
  # PPAs, so treating that as fatal aborts provisioning for an unrelated reason
  # -- and it aborts it HERE, before the venv, sudoers, systemd unit and
  # device.env exist, leaving a half-built box.
  #
  # The install below is the real test: if the packages resolve, the broken repo
  # was irrelevant.
  if ! apt-get update -qq; then
    echo "    WARNING: apt-get update reported errors -- probably a broken"
    echo "    third-party repo. Continuing; the package install is the real gate."
    echo "    To find it:  grep -rn . /etc/apt/sources.list.d/ | grep -v '^.*:#'"
  fi
  if ! apt-get install -y -qq python3-venv python3-dev; then
    echo "    !! could not install python3-venv / python3-dev."
    echo "    If apt reported a broken repository above, remove it and re-run:"
    echo "        grep -rn . /etc/apt/sources.list.d/"
    echo "        sudo rm /etc/apt/sources.list.d/<the-offending-file>"
    echo "        sudo apt-get update"
    exit 1
  fi
  # --system-site-packages is REQUIRED on JetPack: tensorrt, cv2 (with the
  # CUDA/GStreamer build) and torch come from the system image and cannot be
  # pip-installed into an isolated venv.
  python3 -m venv --system-site-packages "$ROOT/venv"
  chown -R "$SVC_USER:$SVC_USER" "$ROOT/venv"
fi
"$ROOT/venv/bin/pip" install -q --upgrade pip
# Only packages that are NOT part of JetPack. tensorrt, cv2 (the CUDA/GStreamer
# build), torch and gi come from the system image via --system-site-packages and
# must never be pip-installed over.
#
# python-dotenv and open_clip_torch are imported by edge/main.py -- omitting
# them means the pipeline dies at import and systemd crash-loops the unit with
# a bare ModuleNotFoundError.
#
# Jetson.GPIO is deliberately NOT here. JetPack ships it as the apt package
# python3-jetson-gpio (2.1.7) in /usr/lib/python3/dist-packages, which the venv
# already sees via --system-site-packages. Pip-installing it would SHADOW the
# board-matched apt build with a PyPI one.
"$ROOT/venv/bin/pip" install -q \
    requests sqlalchemy psycopg2-binary numpy pygame pyserial \
    python-dotenv pillow

echo "==> deterrence GPIO check"
# Verified on jetson-forest-01 (2026-08-01): both of these must hold or the GPIO
# relay silently never fires while the USB relay keeps working, which makes the
# box look healthy. See main.py for the JETSON_MODEL_NAME default.
if ! sudo -u "$SVC_USER" env JETSON_MODEL_NAME="${JETSON_MODEL_NAME:-JETSON_ORIN_NANO}" \
      "$ROOT/venv/bin/python3" -c "import Jetson.GPIO" 2>/dev/null; then
  echo "    WARN: the service user still cannot import Jetson.GPIO."
  echo "          Reproduce, and read the exception -- it distinguishes the two causes:"
  echo "            sudo -u $SVC_USER $ROOT/venv/bin/python3 -c 'import Jetson.GPIO'"
  echo "          'permissions ... /dev/gpiochip0' -> the gpio group did not take"
  echo "                                              effect yet; reboot or re-login."
  echo "          'Could not determine Jetson model' -> this board's compatible"
  echo "                                                string is not in the installed"
  echo "                                                Jetson.GPIO. Compare:"
  echo "            cat /proc/device-tree/compatible | tr '\\0' '\\n' | head -1"
  echo "            grep -n p3767 /usr/lib/python3/dist-packages/Jetson/GPIO/gpio_pin_data.py"
  echo "          and set JETSON_MODEL_NAME in /etc/deerkha/device.env to override."
else
  echo "    ok: $SVC_USER can import Jetson.GPIO"
fi

# open_clip_torch DEPENDS ON torch AND torchvision. Installing it normally makes
# pip fetch the PyPI wheels, which are built against a newer CUDA runtime than
# the Jetson's driver supports -- and because they land inside the venv they
# SHADOW JetPack's working build. The failure is not at install time; it is the
# first torch.cuda call:
#
#   torch.AcceleratorError: CUDA error: CUDA driver version is insufficient
#   for CUDA runtime version
#
# --no-deps installs open_clip itself and nothing else, so JetPack's torch keeps
# winning. Its remaining dependencies are pure-python and safe to resolve
# normally.
#
# PINNED to the versions proven against JetPack's torch 2.8.0 / torchvision
# 0.23.0 (CUDA 12.6) on Orin. Unpinned, a future open_clip release can require a
# newer torch than the Jetson build provides, and the failure surfaces as a CUDA
# error at runtime rather than a resolver error at install time.
"$ROOT/venv/bin/pip" install -q --no-deps open_clip_torch==3.3.0 timm
# torch's own pure-python dependencies. Safe to resolve normally -- none of them
# pull torch back in.
"$ROOT/venv/bin/pip" install -q \
    filelock typing-extensions sympy networkx jinja2 fsspec \
    regex ftfy tqdm huggingface-hub safetensors

echo "==> verifying CUDA-capable torch"
# Provisioning must fail HERE, not at the first deploy. Without this the box
# looks provisioned, the deploy activates, and the pipeline crash-loops on an
# error that points at the application rather than at the environment.
if ! "$ROOT/venv/bin/python" - <<'PYEOF'
import sys
try:
    import torch
except Exception as e:
    sys.exit(f"torch not importable: {e}")
if not torch.cuda.is_available():
    sys.exit("torch imported but CUDA is NOT available")
print(f"    torch {torch.__version__}, CUDA {torch.version.cuda}, "
      f"device: {torch.cuda.get_device_name(0)}")
PYEOF
then
  echo "    !! torch in this venv cannot use CUDA."
  echo
  echo "    Almost always: a PyPI torch wheel is shadowing JetPack's build."
  echo "    Check where it is coming from:"
  echo "        $ROOT/venv/bin/python -c 'import torch; print(torch.__file__)'"
  echo "    If that path is inside $ROOT/venv, remove it so the system build wins:"
  echo "        $ROOT/venv/bin/pip uninstall -y torch torchvision"
  echo "    JetPack does not ship torch by default; install NVIDIA's Jetson wheel"
  echo "    system-wide (not in the venv) if 'python3 -c \"import torch\"' also fails."
  exit 1
fi

echo "==> sudoers (service restart only)"
# deploy.sh restarts the unit over ssh as the service user. Scope the grant to
# exactly that -- not blanket NOPASSWD.
cat > /etc/sudoers.d/deerkha <<EOF
$SVC_USER ALL=(root) NOPASSWD: /bin/systemctl restart deerkha-drishti, /bin/systemctl stop deerkha-drishti, /bin/systemctl start deerkha-drishti
EOF
chmod 440 /etc/sudoers.d/deerkha

echo "==> systemd unit"
if [ -f /tmp/deerkha-drishti.service ]; then
  install -m 644 /tmp/deerkha-drishti.service /etc/systemd/system/deerkha-drishti.service
  systemctl daemon-reload
  systemctl enable deerkha-drishti
  echo "    enabled (not started -- no code deployed yet)"
else
  echo "    !! deploy/deerkha-drishti.service not found at /tmp/. Copy it and re-run:"
  echo "       scp deploy/deerkha-drishti.service $SVC_USER@<ip>:/tmp/"
fi

echo "==> log rotation"
cat > /etc/logrotate.d/deerkha <<EOF
$LOGS/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
EOF

echo "==> device identity template"
if [ ! -f /etc/deerkha/device.env ]; then
  cat > /etc/deerkha/device.env <<'EOF'
# The ONLY per-device file on this box. Everything else -- cameras, RTSP
# credentials, ROI, classes, prompts, thresholds, retention, Telegram bot token
# and chat id -- comes from the server. A deploy never reads or writes this file.
JETSON_DEVICE_ID=CHANGE_ME
CONFIG_API_TOKEN=CHANGE_ME

# The server's Tailscale name or IP. Prefer the MagicDNS name -- if the server
# ever moves, an IP here means editing this file on every box in the fleet.
# Verify it resolves ON THIS BOX before deploying:  getent hosts deerkha-server
CONFIG_SERVER_URL=http://CHANGE_ME:8000

# Until the egress rework lands, the edge still writes detections to Postgres
# directly, so this fleet-wide secret must live here. Remove it once alerts are
# routed via the server.
DATABASE_URL=

# The Telegram bot token and chat id normally come FROM THE SERVER, in the
# config blob -- set them under Global settings on the dashboard, not here, so
# that rotating them is one edit for the whole fleet instead of an SSH session
# per box. A box with neither a server value nor these still starts: it logs
# alerts and sends nothing until its first successful config poll.
#
# Leave these blank. Fill them ONLY as a break-glass fallback for a box that
# must alert before it can reach the server.
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_CHAT_ID=
EOF
  chmod 600 /etc/deerkha/device.env
  chown "$SVC_USER:$SVC_USER" /etc/deerkha/device.env
  echo "    created /etc/deerkha/device.env -- FILL IT IN before deploying"
else
  echo "    /etc/deerkha/device.env already exists, left untouched"
fi

cat <<EOF

Provisioned. Next:

  1. CHECK THE NVMe IS ACTUALLY MOUNTED:
         df -h $STORAGE
     If that shows the root filesystem rather than a separate ~465GB device,
     recording will go to the eMMC and wear it out within a year. This script
     CREATES the directory either way, so its existence proves nothing.

  2. edit /etc/deerkha/device.env   (device id + token from the dashboard)

  3. put the TensorRT engines in $ROOT/models  (they are not in git)

  4. deploy, whichever way you work:
         ON this box:      sudo bash deploy/local-deploy.sh <tag>
         over ssh:         ./deploy/push-models.sh <device_id>
                           ./deploy/deploy.sh <tag> --only <device_id>
EOF
