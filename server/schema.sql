-- =====================================================================
-- Deerkha Drishti -- full database schema.
--
-- Run ONCE against the server's Postgres:
--     psql "$DATABASE_URL" -f schema.sql
--
-- Idempotent (CREATE TABLE IF NOT EXISTS), so re-running is safe.
--
-- IMPORTANT: because every statement is IF NOT EXISTS, this file will NOT
-- alter a table that already exists. Changes made after a database was first
-- created are listed as explicit ALTER statements in comments next to the
-- column they add -- search this file for "MIGRATION".
--
-- All config for every Jetson lives here; the server assembles these rows
-- into one JSON blob at GET /api/config/<device_id>.
--
-- SECRETS: the Telegram bot token IS stored here (cfg_fleet_settings, with an
-- optional per-device override on cfg_global_settings) and IS delivered to
-- devices inside the config blob, so that rotating it is one dashboard edit
-- rather than an SSH session on every box. That means the token reaches every
-- Jetson's on-disk config cache -- moving it here buys operational
-- convenience, NOT a smaller blast radius if a box is stolen. Anything that
-- renders a blob or an audit diff must redact it; see app/assembly.redact()
-- and admin_api.SECRET_FIELDS.
--
-- DATABASE_URL is still NEVER stored here -- it stays in the Jetson's own env
-- file until the egress rework removes the edge's direct DB write entirely.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- for gen_random_uuid()

-- ---------------------------------------------------------------------
-- 0. Detections -- the alert history the dashboard reads.
--
-- Written DIRECTLY by each Jetson (edge/main.py send_supabase_alert), not by
-- this server, so the column names and types below must match that INSERT
-- exactly. Read by app/routers/detections.py; pruned by app/jobs/retention.py.
--
-- This table predates the config schema and used to live in a hosted database
-- that already had it, which is why older versions of this file said it was
-- not touched here. On a fresh server nothing else creates it -- without this
-- block every edge insert fails and the alerts page 500s.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS detections (
    id            bigserial PRIMARY KEY,
    -- The edge sends a NAIVE UTC datetime. The cluster must be UTC (install-server.sh
    -- sets timedatectl and pins timezone='UTC' before Postgres is installed) or
    -- every row lands offset from the rest of the history.
    timestamp     timestamptz NOT NULL DEFAULT now(),
    source        text,          -- "SYSTEM / CAM 1"
    message       text,          -- "DETECTION: LEOPARD at CAM 1 [jetson-forest-01]"
    type          text,          -- 'critical' | 'warning'
    -- JSON *string*, not jsonb: the edge writes json.dumps({label: count}) and the
    -- dashboard does JSON.parse() on it (templates/detections.html). Making this
    -- jsonb would return a decoded object and break that parse.
    animals       text,
    -- Epoch seconds from time.time(). NOT a timestamp -- anything comparing it to
    -- `timestamp` must convert first, e.g. COALESCE(to_timestamp(raw_timestamp), timestamp).
    raw_timestamp double precision
);

-- Ordering index for the dashboard's default query, which sorts by
-- raw_timestamp DESC NULLS LAST, timestamp DESC. Without it every page load
-- sequentially scans and sorts the whole table.
CREATE INDEX IF NOT EXISTS idx_detections_raw_ts
    ON detections (raw_timestamp DESC NULLS LAST, timestamp DESC);

-- ---------------------------------------------------------------------
-- 1. Devices -- one row per Jetson
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_devices (
    device_id           text PRIMARY KEY,               -- matches JETSON_DEVICE_ID
    display_name        text NOT NULL DEFAULT '',
    device_token        text NOT NULL,                  -- bearer token the Jetson uses
    -- Fleet layout (deploy/provision.sh mounts the NVMe here). MIGRATION for a
    -- database created before this changed:
    --   ALTER TABLE cfg_devices ALTER COLUMN storage_root
    --     SET DEFAULT '/mnt/data/video_storage';
    storage_root        text NOT NULL DEFAULT '/mnt/data/video_storage',
    rfdetr_engine_path  text,
    clip_engine_path    text,
    clip2_checkpoint    text,
    clip2_model_dir     text,
    clip_img_input_size int NOT NULL DEFAULT 256,
    rf_nms_iou          double precision NOT NULL DEFAULT 0.7,
    rf_predict_threshold double precision NOT NULL DEFAULT 0.25,
    tailscale_ip        text,
    restart_required    boolean NOT NULL DEFAULT false,
    config_version      text,                            -- sha256 of assembled blob
    enabled             boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 2. Cameras -- per device, per camera
--    roi_polygon stored at 1080x720 (matches ROI_POLYGONS_RAW_ORIGINAL).
--    ignore_zones stored at native camera res ~1920x1080 (matches roi_zones.json).
--    Motion fields NULL => inherit cfg_global_settings.motion_default_*.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_cameras (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id           text NOT NULL REFERENCES cfg_devices(device_id) ON DELETE CASCADE,
    cam_name            text NOT NULL,                   -- "CAM 1"
    -- 0-based ordinal AND the live-view addressing key: the dashboard proxies
    -- /snapshot/{cam_index} to the Jetson, which maps it back to a camera. Holes
    -- left by deletions are fine and are never renumbered; duplicates are not.
    cam_index           int  NOT NULL,
    rtsp_url            text NOT NULL,
    codec               text NOT NULL DEFAULT 'h265',    -- h265 | h264
    enabled             boolean NOT NULL DEFAULT true,
    motion_min_frames   int,
    motion_threshold    int,
    motion_kernel_w     int,
    motion_kernel_h     int,
    motion_area_min     int,
    roi_polygon         jsonb NOT NULL DEFAULT '[]'::jsonb,
    roi_drawn_w         int NOT NULL DEFAULT 1080,
    roi_drawn_h         int NOT NULL DEFAULT 720,
    ignore_zones        jsonb NOT NULL DEFAULT '[]'::jsonb,
    ignore_drawn_w      int NOT NULL DEFAULT 1920,
    ignore_drawn_h      int NOT NULL DEFAULT 1080,
    -- Bare FILENAME of the uploaded ROI backdrop, e.g. "<cam-uuid>_a1b2c3d4.jpg".
    -- It used to hold a public URL path ("/reference_frames/<name>") because the
    -- files were served by an unauthenticated static mount. They are now behind
    -- an admin-guarded route, so only the filename is stored and the serving
    -- path can change without rewriting rows.
    -- MIGRATION for an existing database (schema.sql is CREATE TABLE IF NOT
    -- EXISTS, so this will NOT be applied automatically):
    --   UPDATE cfg_cameras
    --      SET reference_frame_url = regexp_replace(reference_frame_url,
    --                                               '^/reference_frames/', '')
    --    WHERE reference_frame_url LIKE '/reference_frames/%';
    -- The frontend normalises both forms, so this is tidy-up, not a hard cutover.
    reference_frame_url text,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (device_id, cam_name),
    CONSTRAINT uq_cfg_cameras_dev_idx UNIQUE (device_id, cam_index)
);
CREATE INDEX IF NOT EXISTS idx_cfg_cameras_device ON cfg_cameras (device_id, cam_index);

-- MIGRATION for an existing database (CREATE TABLE IF NOT EXISTS above will NOT
-- add the constraint to a table that already exists). Check for duplicates first:
--     SELECT device_id, cam_index, count(*) FROM cfg_cameras
--       GROUP BY 1,2 HAVING count(*) > 1;
-- resolve any rows it returns, then:
--     ALTER TABLE cfg_cameras
--       ADD CONSTRAINT uq_cfg_cameras_dev_idx UNIQUE (device_id, cam_index);

-- ---------------------------------------------------------------------
-- 3. Per-class detection settings (RF thresh + color + CLIP logit)
--    Includes pseudo-classes "Animal" and "default".
--    Colors stored as B/G/R ints (edge consumes BGR tuples).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_class_settings (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id           text NOT NULL REFERENCES cfg_devices(device_id) ON DELETE CASCADE,
    class_name          text NOT NULL,                   -- 'wild boar'..'deer','Animal','default'
    rf_class_id         int,                             -- 0..5, NULL for Animal/default
    rf_thresh           double precision NOT NULL,
    color_b             int NOT NULL DEFAULT 255,
    color_g             int NOT NULL DEFAULT 255,
    color_r             int NOT NULL DEFAULT 255,
    clip_min_logit      double precision,                            -- NULL if not a CLIP class
    is_clip_alert_class boolean NOT NULL DEFAULT false,
    sort_order          int NOT NULL DEFAULT 0,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (device_id, class_name)
);

-- ---------------------------------------------------------------------
-- 4. CLIP prompt lists (positive / fallback / cross-species) per class
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_clip_prompts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id           text NOT NULL REFERENCES cfg_devices(device_id) ON DELETE CASCADE,
    class_name          text NOT NULL,
    positive_prompts    jsonb NOT NULL DEFAULT '[]'::jsonb,   -- CLIP_PROMPTS[class]
    fallbacks           jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"buffalo":[...], ...}
    cross_species       text,                                 -- CROSS_SPECIES_FALLBACK[class]
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (device_id, class_name)
);

-- ---------------------------------------------------------------------
-- 5. CLIP distractors (global list) + CLIP scalar knobs, one row per device
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_clip_distractors (
    device_id           text PRIMARY KEY REFERENCES cfg_devices(device_id) ON DELETE CASCADE,
    distractors         jsonb NOT NULL DEFAULT '[]'::jsonb,
    fallback_min_logit  double precision NOT NULL DEFAULT 27.0,
    bbox_pad            double precision NOT NULL DEFAULT 0.30,
    min_crop_w          int  NOT NULL DEFAULT 45,
    min_crop_h          int  NOT NULL DEFAULT 45,
    min_rf_conf         double precision NOT NULL DEFAULT 0.40,
    clip_cooldown       double precision NOT NULL DEFAULT 5.0,
    keep_text_tower_resident boolean NOT NULL DEFAULT true,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 6. Global settings -- one row per device
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_global_settings (
    device_id           text PRIMARY KEY REFERENCES cfg_devices(device_id) ON DELETE CASCADE,
    roi_start_hour      int  NOT NULL DEFAULT 18,
    roi_end_hour        int  NOT NULL DEFAULT 6,
    roi_end_minute      int  NOT NULL DEFAULT 31,
    detection_res_w     int  NOT NULL DEFAULT 1280,
    detection_res_h     int  NOT NULL DEFAULT 720,
    polygons_drawn_res_w int NOT NULL DEFAULT 1080,
    polygons_drawn_res_h int NOT NULL DEFAULT 720,
    motion_video_res_w  int,                             -- NULL => native
    motion_video_res_h  int,
    cooldown_specific   int  NOT NULL DEFAULT 3,
    cooldown_generic    int  NOT NULL DEFAULT 15,
    motion_close_delay  int  NOT NULL DEFAULT 8,
    motion_min_clip_duration int NOT NULL DEFAULT 30,
    motion_max_clip_duration int NOT NULL DEFAULT 3600,
    animal_rotate_sec   int  NOT NULL DEFAULT 10,
    fps                 double precision NOT NULL DEFAULT 10.0,
    -- Per-camera inference rate. Paces the edge inference loop; does NOT
    -- affect capture/motion/recording rates. GPU cost scales with this times
    -- the number of cameras in power mode. Added after the initial schema:
    -- because every table here is CREATE TABLE IF NOT EXISTS, an existing DB
    -- needs this applied by hand:
    --   ALTER TABLE cfg_global_settings
    --     ADD COLUMN IF NOT EXISTS inference_target_fps double precision NOT NULL DEFAULT 5.0;
    inference_target_fps double precision NOT NULL DEFAULT 5.0,
    min_confirm_frames  int  NOT NULL DEFAULT 18,
    buffer_window       int  NOT NULL DEFAULT 25,
    animal_min_confirm_frames int NOT NULL DEFAULT 30,
    animal_buffer_window int NOT NULL DEFAULT 40,
    min_bbox_ratio      double precision NOT NULL DEFAULT 0.0026,
    cleanup_days_to_keep int NOT NULL DEFAULT 3,
    cleanup_low_space_free_percent double precision NOT NULL DEFAULT 10,
    heartbeat_interval_sec int NOT NULL DEFAULT 60,
    poll_interval_sec   int  NOT NULL DEFAULT 30,
    -- default motion block (fallback when a camera leaves motion fields NULL)
    motion_default_min_frames int NOT NULL DEFAULT 5,
    motion_default_threshold  int NOT NULL DEFAULT 20,
    motion_default_kernel_w   int NOT NULL DEFAULT 7,
    motion_default_kernel_h   int NOT NULL DEFAULT 7,
    motion_default_area_min   int NOT NULL DEFAULT 300,
    -- toggles
    supabase_signal_enabled boolean NOT NULL DEFAULT true,
    detailed_telegram_msg   boolean NOT NULL DEFAULT true,
    send_fallback_telegram  boolean NOT NULL DEFAULT false,
    ignore_zones_enabled    boolean NOT NULL DEFAULT true,
    animal_supabase_trigger boolean NOT NULL DEFAULT true,
    telegram_chat_id        text,                        -- non-secret routing target
    -- Per-device Telegram bot token. NULL (the normal case) means "use the
    -- fleet-wide token from cfg_fleet_settings". Set it only when a site needs
    -- its own bot. MIGRATION for an existing database:
    --   ALTER TABLE cfg_global_settings
    --     ADD COLUMN IF NOT EXISTS telegram_bot_token text;
    telegram_bot_token      text,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 6b. Fleet-wide settings -- exactly one row, shared by every device.
--
-- Holds the Telegram bot token for the whole fleet so that rotating it after a
-- BotFather /revoke is a single edit. cfg_global_settings.telegram_bot_token
-- overrides this per device when a site runs its own bot.
--
-- The CHECK constraint is what makes this a singleton: a second INSERT fails
-- rather than silently creating a row nothing reads.
--
-- MIGRATION for an existing database: run both statements below by hand.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_fleet_settings (
    id                 smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    telegram_bot_token text,
    updated_at         timestamptz NOT NULL DEFAULT now()
);
INSERT INTO cfg_fleet_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------
-- 7. Deterrence -- per-device global + per-class targets
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_deterrence_global (
    device_id           text PRIMARY KEY REFERENCES cfg_devices(device_id) ON DELETE CASCADE,
    relay_mode          text NOT NULL DEFAULT 'both',    -- gpio | usb | both
    audio_playback_mode text NOT NULL DEFAULT 'mixed',   -- mixed | single | sequential
    relay_ch1_pin       int  NOT NULL DEFAULT 11,
    relay_ch2_pin       int  NOT NULL DEFAULT 13,
    relay_active_low    boolean NOT NULL DEFAULT true,
    usb_relay_port      text NOT NULL DEFAULT '/dev/ttyUSB0',
    usb_relay_baud      int  NOT NULL DEFAULT 9600,
    usb_on_cmd_hex      text NOT NULL DEFAULT 'A00101A2',
    usb_off_cmd_hex     text NOT NULL DEFAULT 'A00100A1',
    blink_total_duration int NOT NULL DEFAULT 30,
    blink_active_phase   int NOT NULL DEFAULT 10,
    blink_rest_phase     int NOT NULL DEFAULT 3,
    blink_interval       double precision NOT NULL DEFAULT 0.1,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cfg_deterrence_targets (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id           text NOT NULL REFERENCES cfg_devices(device_id) ON DELETE CASCADE,
    class_name          text NOT NULL,                   -- leopard, elephant, ..., animal
    action              text NOT NULL DEFAULT 'none',    -- light_only | light_and_audio | none
    audio_files         jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (device_id, class_name)
);

-- ---------------------------------------------------------------------
-- 8. Heartbeat / status ingest (latest + history)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_device_status (
    id                  bigserial PRIMARY KEY,
    device_id           text NOT NULL REFERENCES cfg_devices(device_id) ON DELETE CASCADE,
    received_at         timestamptz NOT NULL DEFAULT now(),
    cameras_alive       int,
    cameras_total       int,
    per_camera          jsonb,      -- [{cam_name, thread_alive, last_frame_age_sec, power_mode, ...}]
    config_version      text,       -- version the edge reports it is running
    pending_restart     boolean,
    app_uptime_sec      bigint,
    disk_free_percent   double precision,
    notes               text
);
CREATE INDEX IF NOT EXISTS idx_cfg_device_status ON cfg_device_status (device_id, received_at DESC);

-- ---------------------------------------------------------------------
-- 9. Audit log of every config edit
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_audit_log (
    id                  bigserial PRIMARY KEY,
    device_id           text,
    table_name          text,
    row_pk              text,
    action              text,       -- insert | update | delete
    actor               text,
    diff                jsonb,
    new_config_version  text,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cfg_audit_log ON cfg_audit_log (device_id, created_at DESC);
