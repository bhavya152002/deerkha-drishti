"""
settings.py -- process-wide configuration loaded from environment / .env.

Loaded once at import. Everything the office server needs to boot lives here so
there are no scattered os.environ lookups. Secrets never leave this module's
process (they are not exposed via any API route).
"""

import os
from dotenv import load_dotenv

# Where configuration comes from, in order. load_dotenv() never overrides a
# variable that is already set, so systemd's EnvironmentFile always wins and
# these are only fallbacks.
#
#   1. DD_SERVER_ENV_FILE      -- explicit override, for odd layouts
#   2. server/.env             -- a development checkout
#   3. /etc/deerkha-server/server.env
#
# (3) is what makes the CLI tools work on a deployed box. The service gets its
# environment from systemd, but `seed.py`, `verify_seed.py` and
# `retention.py --dry-run` are run BY HAND and inherit nothing. Looking only
# beside the code fails there by construction: the release tree is immutable and
# replaced wholesale on deploy, so it neither has nor should have a .env in it.
_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_CANDIDATES = [
    os.environ.get("DD_SERVER_ENV_FILE"),
    os.path.join(_here, ".env"),
    "/etc/deerkha-server/server.env",
]
for _candidate in _ENV_CANDIDATES:
    if _candidate and os.path.isfile(_candidate):
        load_dotenv(_candidate)
        break
else:
    load_dotenv()


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable '{name}'.\n"
            f"Looked for an env file at: "
            + ", ".join(c for c in _ENV_CANDIDATES if c)
            + "\nOn a deployed server, set it in /etc/deerkha-server/server.env "
            "(the same file the systemd unit reads). In a development checkout, "
            "copy server/.env.example to server/.env."
        )
    return val


SERVER_ROOT = _here
DATABASE_URL = _require("DATABASE_URL")

# Fail closed. These previously defaulted to a published constant and to
# "admin"/"admin". A silently-defaulted signing key means anyone who can read
# this repo can forge an admin session cookie -- across every site in the
# fleet. Refusing to boot is the correct behaviour: a server that will not
# start is loud, and a server with a forgeable admin cookie is silent.
SESSION_SECRET = _require("SESSION_SECRET")
ADMIN_USERNAME = _require("ADMIN_USERNAME")
ADMIN_PASSWORD = _require("ADMIN_PASSWORD")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# Persistent data that is NOT code. Must live outside the checkout.
#
# This used to be `SERVER_ROOT/reference_frames`, i.e. inside the source tree.
# That is a landmine that arms itself on first use: the moment an operator
# uploads a reference frame, it lands in the deployed release directory. Deploy
# by git pull, release symlink or fresh clone and the file is gone -- while
# cfg_cameras.reference_frame_url still points at it, so the ROI editor
# background 404s with no obvious cause. It is 0 bytes today, which is exactly
# why this is the right moment to move it: there is nothing to migrate.
#
# DD_SERVER_DATA_DIR lets systemd point this at StateDirectory=. Note the
# makedirs below runs AT IMPORT, before FastAPI or logging exist -- so an
# unwritable path is not a broken upload, it is a service that refuses to
# start with a bare traceback. Under `ProtectSystem=strict` that happens on a
# fresh provision but not in local testing, because makedirs(exist_ok=True)
# succeeds on a directory that already exists even under a read-only mount.
DATA_DIR = os.environ.get("DD_SERVER_DATA_DIR") or os.path.join(SERVER_ROOT, "data")
REFERENCE_FRAME_DIR = os.path.join(DATA_DIR, "reference_frames")
try:
    os.makedirs(REFERENCE_FRAME_DIR, exist_ok=True)
except OSError as e:
    raise RuntimeError(
        f"Cannot create reference frame directory {REFERENCE_FRAME_DIR!r}: {e}. "
        f"Set DD_SERVER_DATA_DIR to a writable path (systemd: StateDirectory=)."
    ) from e
