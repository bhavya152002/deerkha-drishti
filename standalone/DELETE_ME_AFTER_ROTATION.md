# This directory is dead. Delete it once you have rotated credentials.

The edge code moved to `../edge/` and the systemd unit to `../deploy/`.
Two files were deliberately left behind:

## `env` — CONTAINS LIVE SECRETS

This file holds a **Telegram bot token** and the **Supabase database password
in plaintext**. It is excluded by `.gitignore`, so it will not enter git
history — but that protection only holds while the ignore rule is in place, and
git history is permanent.

Do this, in order:

1. **Rotate the Telegram bot token** — BotFather → `/revoke` → pick the bot.
2. **Rotate the Supabase database password** — Supabase → Settings → Database.
3. Put the new values in:
   - the office server's `server/.env`
   - each Jetson's `/etc/deerkha/device.env`
4. **Rotate every device token** — dashboard → each device → *Rotate token*.
5. Then delete this whole directory:

   ```
   rm -rf standalone/
   ```

Treat the old values as compromised regardless: they have been sitting in a
plaintext file inside the project tree.

## `roi_zones.json` — superseded

Ignore zones now come from the server (`cfg_cameras.ignore_zones`). Nothing
reads this file any more; it is kept only so you can eyeball the old values
against the dashboard before deleting.
