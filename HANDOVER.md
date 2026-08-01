# Deerkha Drishti — handover

**State as of 2026-08-01.** The system is **live on one Jetson with cameras streaming**, both
halves on `v1.2.1`. The server is finished. What remains is tuning, one unfinished rewrite, and the
housekeeping listed under "Open items".

Since the 07-27 snapshot: the box was moved off the hand-launched legacy script onto the deployed
service, and `v1.2.1` fixed the deterrence relay — the admin panel's relay mode is now honoured,
and the **GPIO relay fires for the first time** (it had never worked on this hardware, for any
user). See "Deploying → deterrence relay".

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
| Tailnet IP | `100.116.93.38` | `100.76.72.67` |
| Source clone | `/root/deerkha` | `~/deerkha-src` (i.e. `/home/visionlogix/deerkha-src`) |

**SSH works with keys from the dev machine** (verified 2026-08-01): `root@100.116.93.38` for the
server and `visionlogix@100.76.72.67` for the Jetson, both key-only, no password. `sudo` on the
Jetson still prompts. The `deerkha` service user on the VPS does **not** accept the dev key — use
`root` there, or `deerkha-admin` from the console.

**Repository:** `github.com/bhavya152002/deerkha-drishti`, branch `master`, tagged releases.
A GitLab mirror exists (`gitlab.com/computer-vision644830/deerkha-drishti`) but is **not** the
deployment source. GitHub has only `master`; GitLab has several branches and its default is not
this release, which is why every clone command pins a tag.

Clone on the Jetson is at `~/deerkha-src`. **Deploys pin a tag — never a branch.**

**Device:** `jetson-forest-01` ("Forest Site 01"). Its `device_token` is in
`/etc/deerkha/device.env` as `CONFIG_API_TOKEN`; rotate from the dashboard if lost, don't re-seed.

---

## Versions deployed

| Component | Running | Latest tag |
|---|---|---|
| Jetson (`edge/`) | **v1.2.1** | v1.2.1 |
| Server (`server/`) | **v1.2.1** | v1.2.1 |

Both deployed 2026-08-01. The edge deploy is health-gated and self-rolls-back; the server deploy is
not — it was verified by hand with `healthz`. Rollback: `sudo bash deploy/local-deploy.sh --rollback`
on the Jetson (`v1.1.7` is still staged); on the server, repoint the `current` symlink at
`/opt/deerkha-server/releases/v1.0.8`.

`v1.2.0` changed server-side **defaults only** — the edge took those over the config poll, with no
release needed. `v1.2.1` is different: it is an **edge code fix** for the deterrence relay, and the
server half only adds the restart flag and a toast. See "Deploying → deterrence relay".

The `v1.0.8 → v1.2.1` server jump added **no columns** — only Python-side defaults for newly
created rows — so no migration was needed and existing config rows were untouched.

---

## Do this first

`v1.2.0` corrected three defaults that were wrong for an Orin Nano. **Changed defaults do not apply
to existing rows**, so the live device needed a one-time update.

**Two of the three are already applied** (verified in the live DB 2026-08-01):
`motion_video_res` is `1280×720` ✓ and `keep_text_tower_resident` is `false` ✓.
**`cleanup_days_to_keep` is still `3`** and wants to be `2`:

```bash
sudo -u postgres psql -d deerkha -c "UPDATE cfg_global_settings SET cleanup_days_to_keep=2;"
```

Then touch any setting in the dashboard and save, to bump `config_version`. The Jetson applies it
within one poll (~30 s), **no restart**.

Box #1 was at ~97% CPU with over-current throttling and ~90% RAM at four cameras when this was
written; the two applied changes are the config half of that fix.

Verify on the Jetson:

```bash
# storage_root is /home/visionlogix/video_storage, NOT /mnt/data -- see Open items #1
ffprobe "$(ls -t /home/visionlogix/video_storage/$(date +%F)/*/*/motion_video/*.mp4 | head -1)" 2>&1 | grep Stream
jtop
```

Expect **1280×720**, CPU well under 70%, RAM near 50%. Expected saving is ~3.4–3.9 GB of RAM and
roughly half the encode CPU.

The server is already on `v1.2.1`, so future seeds get the corrected values.

---

## Open items, ranked by cost if ignored

1. **Storage is NOT at `/mnt/data` — decide whether that is intended.** Resolved and re-scoped
   2026-08-01. The eMMC fear was unfounded: `/` **is** the NVMe (`/dev/nvme0n1p1`, 468 G, 37% used),
   `/mnt/data` is merely a directory on it, and there is no separate mount. But the device's
   configured `storage_root` (`cfg_devices.storage_root`) is
   **`/home/visionlogix/video_storage`**, so that is where recording actually goes — 95 G there
   versus 6.2 G of stale data under `/mnt/data/video_storage`, last written 2026-07-27. Both sit on
   the same NVMe, so nothing is at risk; the problem is that the docs, the retention maths and every
   `du`/`ffprobe` command below pointed at the wrong tree. Either repoint `storage_root` to
   `/mnt/data/video_storage` from the dashboard (restart-class) or accept the home path and delete
   the stale `/mnt/data` copy to reclaim 6.2 G.
2. **Rotate the credentials in `standalone/env`**, then delete the file. It still holds a live
   Telegram bot token and the old database password. If the token now set in the dashboard is the
   *old* one, the whole fleet is using a known-exposed credential.
3. **Make the GitHub repo private.** It is still public. It documents the full deployment
   topology, including that admin auth is a single shared env credential.
4. **Measure real disk use.** `du -sh /home/visionlogix/video_storage/*/` after one night (note the
   path — see #1). **Every disk figure in the plan and the deck is modelled, not measured.** This is
   what decides whether `cleanup_days_to_keep` stays at 2 and whether camera bitrate must drop to
   2 Mbps. First real data point: 95 G accumulated at four cameras, oldest day 2026-07-27.
5. **E1.1 passthrough mux** — the real fix for CPU, and mandatory for 8 cameras. See "Known
   limits".
6. **Reboot both boxes** and confirm they return unattended. Neither has been rebooted since
   setup; the VPS also has a pending kernel upgrade. Check the GPIO relay comes back too — the
   `gpio` group membership is persistent, but a reboot is the only thing that has not yet proven it.
7. **Rebuild the TensorRT engines on this Orin.** The current ones were built on a different
   Jetson SKU — they load, but TensorRT warns performance may suffer.
8. **Exercise the relay-mode dropdown.** `v1.2.1` made it real, but only `both` has been proven on
   hardware (that is what the DB already held). Selecting `gpio` or `usb` now flags the device for
   restart, it bounces itself, and comes back in that mode — untested end to end. The
   `Deterrence hardware config: mode=…` line in `deerkha.log` tells you immediately whether it took.
9. **`deploy.sh` for the fleet** before box #3. SSH keys now work to both boxes (see Inventory), so
   the ssh deploy path is usable; it just has not been exercised. Password auth is still enabled on
   the VPS and should be turned off. Visiting seven sites per update is the cost this avoids.
10. **Diff the old script.** `edge/main.py` was forked from `..._newmot_2.py`, but the box was
    running `..._newmot_2e.py`, which was never committed and exists only at
    `/home/visionlogix/visionlogix_project/`. Any fix made in `2e` is absent from what runs now.
    The box no longer runs `2e` — it is on the deployed service — so this is now a code-archaeology
    task, not a live-divergence one.

---

## Deploying

**Edge (on the Jetson — no ssh needed):**

```bash
cd ~/deerkha-src && git fetch --tags --force && git checkout <tag>
sudo bash deploy/local-deploy.sh <tag>
sudo bash deploy/local-deploy.sh --status
sudo bash deploy/local-deploy.sh --rollback
```

The `git checkout` is only so the *deploy script itself* is the tagged one — staging reads the tag
directly (`git archive <tag> edge/`), so an uncommitted edit in the clone is never deployed.

Stages the release, swaps the `current` symlink atomically, restarts, then requires the service to
be active, not restart-looping, and to log a `HEARTBEAT` within 90 s — otherwise it rolls back and
dumps the logs. It never touches `device.env`, `/var/lib/deerkha`, `/opt/deerkha/models` or the
video storage.

`deploy/deploy.sh` is the ssh equivalent for the fleet, with canary-first rollout that halts on
failure. Switch to it once there is more than one box.

### The deterrence relay — fixed in v1.2.1

Two unrelated defects made the panel's **Relay & blink** card behave wrongly: selecting `gpio`
still fired the USB relay, and selecting `both` fired only USB. Both are fixed; the history matters
because a new box can reproduce either.

**1. The relay settings never reached the device.** `RELAY_MODE`, the GPIO pins and the USB port
were module constants, and `init_relay()` ran at *import* — which happens before `config_client` is
imported, so `cfg_deterrence_global` could not be read on any path, restart included. The box was
permanently in `both` and the dropdown was decorative. Importing `trigger_jetson_3` no longer
touches hardware; `main.py` calls `deterrence.init_hardware(cfg["deterrence"])` once the boot
snapshot exists.

Those fields are **restart-class**: the pins are exported and the serial port opened against them,
so they cannot be swapped under a running blink loop. Saving one now sets `restart_required` and
the box bounces itself. `audio_playback_mode` and the four `blink_*` values stay hot — they apply
within one poll, no restart. `apply_config()` logs any relay change it cannot hot-apply rather than
ignoring it.

**2. GPIO had never fired on this hardware, for any user.** Two blockers stacked, in the order
Jetson.GPIO hits them at import — so fixing only the first looks like no progress:

- **Permissions.** `/dev/gpiochip*` is `root:gpio 0660` and the `deerkha` service user was not in
  the `gpio` group (`gpio.py:33`). `provision.sh` now adds it. **A deploy will not do this** —
  `local-deploy.sh` / `deploy.sh` only stage code and swap the symlink. Group changes also need a
  service restart, since systemd resolves supplementary groups at exec.
- **Board detection.** The board reports `nvidia,p3768-0000+p3767-0005-super`, but Jetson.GPIO
  2.1.7 (apt `python3-jetson-gpio`) only lists `nvidia,p3768-0000+p3767-0005` — the **`-super`**
  suffix on the Orin Nano Super breaks its exact-match lookup, so `get_model()` raises
  "Could not determine Jetson model" (`gpio.py:69`). `main.py` sets
  `JETSON_MODEL_NAME=JETSON_ORIN_NANO` before the import — Jetson.GPIO's own documented override —
  via `setdefault`, so `/etc/deerkha/device.env` still wins on a board that needs another value.

The second blocker hits `visionlogix` too, so the hand-run legacy script never fired GPIO either.
**"The standalone works" only ever meant the USB relay works.** Do not read it as evidence the GPIO
path is fine.

Do **not** pip-install `Jetson.GPIO`. JetPack ships it as apt `python3-jetson-gpio`, which the venv
already sees via `--system-site-packages`; a pip copy would shadow the board-matched build.

Verify on a box:

```bash
id deerkha | grep -o gpio
sudo -u deerkha /opt/deerkha/venv/bin/python3 -c "import Jetson.GPIO; print('ok')"
grep -aiE "relay|GPIO" /var/log/deerkha/deerkha.log | tail
```

Expect `Deterrence hardware config: mode=…` (proving the mode came from the server, not a
constant), then `GPIO relay ready on pins 11/13` **and** `USB relay ready on /dev/ttyUSB0`.

If only one backend comes up in `both`, that is now an **explicit error at init** naming the dead
one, and the heartbeat carries `relay=<mode> gpio:<ok|down|-> usb:<...>` into
`cfg_device_status.notes` — visible on the dashboard. Previously it was completely silent:
`_set_relays_gpio()` returned False with no log once `_gpio_ready` was False, and `set_relays()`
returns `gpio or usb`, so the working USB relay masked the dead GPIO one entirely.

Box #1 as of 2026-08-01: `relay=both gpio:ok usb:ok`, pins 11/13 active-low, `/dev/ttyUSB0`.
All six `cfg_deterrence_targets` audio paths resolve to real files in `/opt/deerkha/sounds/`.
Note `animal` and `deer` are `light_only`, so the common unclassified detection fires **lights
only** — silence there is correct behaviour, not a broken audio engine.

**Server (on the VPS):**

```bash
cd /root/deerkha && git fetch --tags --force && git checkout <tag>
TAG=<tag>
sudo install -d -o deerkha -g deerkha /opt/deerkha-server/releases/$TAG
sudo cp -a /root/deerkha/server/. /opt/deerkha-server/releases/$TAG/
sudo chown -R deerkha:deerkha /opt/deerkha-server/releases/$TAG
sudo ln -sfn /opt/deerkha-server/releases/$TAG /opt/deerkha-server/current.new
sudo mv -Tf /opt/deerkha-server/current.new /opt/deerkha-server/current
sudo systemctl restart deerkha-server
curl -s localhost:8000/healthz          # {"ok":true,"db":"ok"}
```

There is no health gate or auto-rollback on this side — check `healthz` yourself, and roll back by
repointing `current` at the previous release directory. Before a multi-version jump, diff
`server/app/models.py` across the two tags: a **new column** needs a hand-written `ALTER TABLE`
first, because `schema.sql` is all `CREATE TABLE IF NOT EXISTS` and will not alter a table that
already exists. Default-only changes affect new rows only and need nothing.

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
| Deterrence relay + audio | `edge/trigger_jetson_3.py` — `init_hardware()` binds restart-class relay settings, `apply_config()` the hot ones |
| Config contract | `server/app/assembly.py` ← → `edge/config_client.py` |
| New-device defaults | `server/schema.sql`, `server/app/models.py`, `server/seed_data.py`, `server/seed.py` — **all four must agree** |
| Guard tests | `server/tests/` — route auth and secret redaction |

**Not yet built:** the client-facing dashboard (Part 10). The nginx `app.visionlogix.io` vhost is
deliberately commented out — both hostnames proxy the same app, so enabling it would publish the
admin login to the internet behind one shared password.
