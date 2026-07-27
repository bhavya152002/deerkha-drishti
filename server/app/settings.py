"""
settings.py -- process-wide configuration loaded from environment / .env.

Loaded once at import. Everything the office server needs to boot lives here so
there are no scattered os.environ lookups. Secrets never leave this module's
process (they are not exposed via any API route).
"""

import os
from dotenv import load_dotenv

# Load .env sitting next to the `server/` directory (server/.env).
_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_path = os.path.join(_here, ".env")
if os.path.isfile(_env_path):
    load_dotenv(_env_path)
else:
    load_dotenv()


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"Copy server/.env.example to server/.env and fill it in."
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
