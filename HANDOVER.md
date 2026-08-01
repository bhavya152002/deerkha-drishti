# Deerkha Drishti — handover

**State as of 2026-07-27.** The system is **live on one Jetson with cameras streaming**. The
server is finished. What remains is tuning, one unfinished rewrite, and the housekeeping listed
under "Open items".

This file is the state of the deployment. For *how to deploy from scratch*, read
`docs/deployment-guide.html`. For *why the architecture is what it is*, read the scaling plan
(Parts 1–13) and `docs/Deerkha-Drishti-Technical-Review.pptx`.

---

## What this is

Wildlife detection and deterrence for a forest contract. Cameras → Jetson (RF-DETR TensorRT
detection, MobileCLIP species confirmation, deterrence relay + audio) → server (config,
dashboard, alert history) → Telegram.

Scaling from 4 cameras on one box to **50+ cameras across ~7 Jetson Orin Nano boxes**.

---

## Inventory

| | Server | Edge |
|---|---|---|
| Host | Hostinger VPS `srv1859075`, Mumbai | `visionlogix-desktop`, Jetson Orin Nano 8 GB Super |
| Public | `https://admin.visionlogix.io` | none — tailnet only |
| Tailnet name | **`deerkha-server`** | — |
| systemd unit | `deerkha-server` | `deerkha-drishti` |
| Code | `/opt/deerkha-server/current` | `/opt/deerkha/current` |
| Config | `/etc/deerkha-server/server.env` | `/etc/deerkha/device.env` |
| Logs | `journalctl -u deerkha-server` | `/var/log/deerkha/deerkha.log` |
| Login user | `deerkha-admin` (sudo), root via Hostinger web console | `visionlogix` |

**Repository:** `github.com/bhavya152002/deerkha-drishti`, branch `master`, tagged releases.
A GitLab mirror exists (`gitlab.com/computer-vision644830/deerkha-drishti`) but is **not** the
deployment source. GitHub has only `master`; GitLab has several branches and its default is not
this release, which is why every clone command pins a tag.

Clone on the Jetson is at `/deerkha-src`. **Deploys pin a tag — never a branch.**

**Device:** `jetson-forest-01` ("Forest Site 01"). Its `device_token` is in
`/etc/deerkha/device.env` as `CONFIG_API_TOKEN`; rotate from the dashboard if lost, don't re-seed.

---

## Versions deployed

| Component | Running | Latest tag |
|---|---|---|
| Jetson (`edge/`) | **v1.1.7** | v1.2.0 |
| Server (`server/`) | **v1.0.8** | v1.2.0 |

`v1.2.0` changes server-side **defaults only** (see "Do this first"). The edge does not need a new
release for it — the values arrive over the config poll.

---

## Do this first

`v1.2.0` corrects three defaults that were wrong for an Orin Nano. **Changed defaults do not
apply to existing rows**, so the live device needs a one-time update. Box #1 currently sits at
~97% CPU with over-current throttling and ~90% RAM at four cameras; this is the fix for the
config half of that.

On the VPS:

```bash
sudo -u postgres psql -d deerkha -c "
UPDATE cfg_global_settings SET motion_video_res_w=1280, motion_video_res_h=720, cleanup_days_to_keep=2;
UPDATE cfg_clip_distractors SET keep_text_tower_resident=false;
"
```

Then touch any setting in the dashboard and save, to bump `config_version`. The Jetson applies
both within one poll (~30 s), **no restart**.

Verify on the Jetson:

```bash
ffprobe /mnt/data/video_storage/$(date +%F)/*/CAM_1/motion_video/*.mp4 2>&1 | grep Stream
jtop
```

Expect **1280×720**, CPU well under 70%, RAM near 50%. Expected saving is ~3.4–3.9 GB of RAM and
roughly half the encode CPU.

Also redeploy the server to `v1.2.0` so future seeds get the corrected values (see below).

---

## Open items, ranked by cost if ignored

1. **Confirm `/mnt/data` is the NVMe.** The Disk Usage Analyzer showed it inside the 401 GB root
   tree, which suggests the NVMe is *not* separately mounted. `provision.sh` creates the
   directory on whatever filesystem is there, so nothing warns you. Recording to eMMC destroys it
   within a year. `df -h /mnt/data` — it must be a separate ~465 GB device.
2. **Rotate the credentials in `standalone/env`**, then delete the file. It still holds a live
   Telegram bot token and the old database password. If the token now set in the dashboard is the
   *old* one, the whole fleet is using a known-exposed credential.
3. **Make the GitHub repo private.** It is still public. It documents the full deployment
   topology, including that admin auth is a single shared env credential.
4. **Measure real disk use.** `du -sh /mnt/data/video_storage/*/` after one night. **Every disk
   figure in the plan and the deck is modelled, not measured.** This is what decides whether
   `cleanup_days_to_keep` stays at 2 and whether camera bitrate must drop to 2 Mbps.
5. **E1.1 passthrough mux** — the real fix for CPU, and mandatory for 8 cameras. See "Known
   limits".
6. **Reboot both boxes** and confirm they return unattended. Neither has been rebooted since
   setup; the VPS also has a pending kernel upgrade.
7. **Rebuild the TensorRT engines on this Orin.** The current ones were built on a different
   Jetson SKU — they load, but TensorRT warns performance may suffer.
8. **SSH keys + `deploy.sh`** before box #3. Password auth is still enabled on the VPS, and
   visiting seven sites per update is the cost the ssh deploy path exists to avoid.
9. **Diff the old script.** `edge/main.py` was forked from `..._newmot_2.py`, but the box was
   running `..._newmot_2e.py`, which was never committed and exists only at
   `/home/visionlogix/visionlogix_project/`. Any fix made in `2e` is absent from what runs now.

---

## Deploying

**Edge (on the Jetson — no ssh needed):**

```bash
cd /deerkha-src && git fetch --tags --force && git checkout <tag>
sudo bash deploy/local-deploy.sh <tag>
sudo bash deploy/local-deploy.sh --status
sudo bash deploy/local-deploy.sh --rollback
```

Stages the release, swaps the `current` symlink atomically, restarts, then requires the service to
be active, not restart-looping, and to log a `HEARTBEAT` within 90 s — otherwise it rolls back and
dumps the logs. It never touches `device.env`, `/var/lib/deerkha`, `/opt/deerkha/models` or the
video storage.

`deploy/deploy.sh` is the ssh equivalent for the fleet, with canary-first rollout that halts on
failure. Switch to it once there is more than one box.

**The GPIO relay has never fired on `jetson-forest-01`, and a deploy alone will not fix it.**
Confirmed on the box 2026-08-01. Two independent blockers stack, and Jetson.GPIO hits them in this
order at import — so fixing only the first just reveals the second:

1. **Permissions.** `/dev/gpiochip*` is `root:gpio 0660` and the `deerkha` service user was not in
   the `gpio` group (`gpio.py:33`). `provision.sh` now adds it.
2. **Board detection.** The board reports `nvidia,p3768-0000+p3767-0005-super`, but Jetson.GPIO
   2.1.7 (apt `python3-jetson-gpio`) only lists `nvidia,p3768-0000+p3767-0005` — the **`-super`**
   suffix on the Orin Nano Super breaks its exact-match lookup, so `get_model()` raises
   "Could not determine Jetson model" (`gpio.py:69`). `main.py` now sets
   `JETSON_MODEL_NAME=JETSON_ORIN_NANO` before the import, which is Jetson.GPIO's own documented
   override. Override it in `/etc/deerkha/device.env` for a board that needs a different value.

Blocker 2 hits `visionlogix` too, so the hand-run legacy script never fired GPIO either — "the
standalone works" meant *the USB relay* works. Do not read it as evidence the GPIO path is fine.

`local-deploy.sh` / `deploy.sh` only stage code and swap the symlink; group membership comes from
`sudo bash deploy/provision.sh`. Group changes need a service restart (systemd re-resolves
supplementary groups at exec) — reboot if in doubt. Then confirm:

```bash
id deerkha | grep -o gpio
sudo -u deerkha /opt/deerkha/venv/bin/python3 -c "import Jetson.GPIO; print('ok')"
grep -aiE "relay|GPIO" /var/log/deerkha/deerkha.log | tail
```

Expect `GPIO relay ready on pins 11/13` **and** `USB relay ready on /dev/ttyUSB0`. Until then the
failure is silent by design of the old code: `_set_relays_gpio()` returned False with no log once
`_gpio_ready` was False, and in `relay_mode=both` `set_relays()` returns `gpio or usb`, so the
working USB relay masked it completely. That is now an explicit error at init, and the heartbeat
carries `relay=<mode> gpio:<ok|down|-> usb:<...>` into `cfg_device_status.notes`.

**Server (on the VPS):**

```bash
cd ~/deerkha && git fetch --tags --force && git checkout <tag>
TAG=<tag>
sudo install -d -o deerkha -g deerkha /opt/deerkha-server/releases/$TAG
sudo cp -a ~/deerkha/server/. /opt/deerkha-server/releases/$TAG/
sudo chown -R deerkha:deerkha /opt/deerkha-server/releases/$TAG
sudo ln -sfn /opt/deerkha-server/releases/$TAG /opt/deerkha-server/current.new
sudo mv -Tf /opt/deerkha-server/current.new /opt/deerkha-server/current
sudo systemctl restart deerkha-server
curl -s localhost:8000/healthz          # {"ok":true,"db":"ok"}
```

---

## Known limits and gotchas

**The board has no hardware video encoder.** NVIDIA removed NVENC from the Orin Nano. Every
recorded frame is encoded on the CPU, and `_get_motion_fourcc()` prefers `avc1` — x264 in
software. This is the dominant CPU cost and the reason **E1.1 (passthrough mux)** is mandatory
rather than optional: tee the camera's H.265 *before* the decoder and `splitmuxsink` it, so
nothing is re-encoded.

**`videoconvert` runs at full 1080p on the CPU** for every frame of every camera
(`edge/gst_camera_stream_jetson.py:70-77`). `motion_video_res` does **not** affect this — it only
changes what the writer records. Scaling in `nvvidconv` (VIC hardware) would cut it 2.25×, at the
cost of 720p stills.

**TensorRT engines are not in git and cannot be.** They don't survive a change of GPU
architecture. They live in `/opt/deerkha/models/`, built on a reference Orin. `mobileclip2_s2.pt`
sits in `models/pt/`. Deterrence `.mp3`s go in `/opt/deerkha/sounds/`.

**torch must come from JetPack, never pip.** `open_clip_torch` depends on torch, so a plain
`pip install` drags in the PyPI build, which shadows the working one and fails at the first
`torch.cuda` call with *"CUDA driver version is insufficient"*. `provision.sh` now uses
`--no-deps` and verifies `torch.cuda.is_available()` at provision time. The proven stack is
**torch 2.8.0 / torchvision 0.23.0 / open_clip_torch 3.3.0**, CUDA 12.6, copied from
`/home/visionlogix/visionlogix_project/ds_env/`.

**The low-space cleanup rule is effectively a no-op.** `storage_cleanup.py:272-278` breaks on
`len(candidates) <= min_keep`; with `days_to_keep` at 2–3 the age rule leaves too few deletable
folders, so it exits having deleted nothing and then logs *"The disk is full of NON-VIDEO data"*,
which is wrong. Fixes, in order: lower camera bitrate; `DEFAULT_MIN_KEEP_FOLDERS` 2 → 1 (**changes
an `rmtree` path — decide deliberately**); or E1.4, a file-level sweep of `motion_video/`.

**Class order matters and fails silently.** `MY_CLASSES` in `edge/rfdetr_trt.py` must match the
label order used when the ONNX was exported. A mismatch gives confident detections with the wrong
species attached — wrong threshold, wrong deterrence — and nothing errors.

**Telegram:** the bot token is fleet-wide (`cfg_fleet_settings`), the chat id is per device, and
several devices may share one chat. Both are dashboard settings and hot-reload. The token *is*
delivered to every box in the config blob — that makes rotation one edit, it does **not** make a
stolen box harmless. It is redacted from preview-config, `get_global` and audit diffs; the on-disk
cache is 0600.

**Terminal paste mangling** ate `*`, `__` and multi-line commands repeatedly during setup,
producing errors that looked real. Paste one line at a time, or write longer commands to a file.

---

## Where things are

| | |
|---|---|
| Deployment guide | `docs/deployment-guide.html` |
| Technical review deck | `docs/Deerkha-Drishti-Technical-Review.pptx` (+ `build_deck.py` to regenerate) |
| Scaling plan | Parts 1–13; Part 13 covers the CPU/RAM defaults |
| Edge pipeline | `edge/main.py` |
| Config contract | `server/app/assembly.py` ← → `edge/config_client.py` |
| New-device defaults | `server/schema.sql`, `server/app/models.py`, `server/seed_data.py`, `server/seed.py` — **all four must agree** |
| Guard tests | `server/tests/` — route auth and secret redaction |

**Not yet built:** the client-facing dashboard (Part 10). The nginx `app.visionlogix.io` vhost is
deliberately commented out — both hostnames proxy the same app, so enabling it would publish the
admin login to the internet behind one shared password.
