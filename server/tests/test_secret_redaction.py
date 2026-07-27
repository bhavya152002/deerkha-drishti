"""
test_secret_redaction.py -- secrets in the config blob must not reach a human.

WHY THIS EXISTS
---------------
The Telegram bot token is delivered to devices INSIDE the config blob so it can
be rotated from the dashboard instead of by SSH-ing to every box. That buys real
operational value and costs a new hazard: the blob is also rendered by
preview-config, and field values are copied verbatim into cfg_audit_log.diff,
which is kept for months and displayed on a dashboard page.

Three failure modes this pins down, all of which are silent:

  1. A new secret column is added and the blanket `{col.name: getattr(...)}`
     dumps expose it by default.
  2. redact() mutates the blob in place -- the same object is held in cache.py
     and served to devices, so redacting in place would push a blanked token to
     the entire fleet and break alerting everywhere at once.
  3. An untouched password field posts "" and wipes the stored token, so saving
     an unrelated setting on the same page silently disables Telegram.

No database is required: these are pure functions.

Run:  python -m pytest tests/ -q     (from server/)
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SESSION_SECRET", "x" * 48)
os.environ.setdefault("ADMIN_USERNAME", "test")
os.environ.setdefault("ADMIN_PASSWORD", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")
os.environ.setdefault("DD_SERVER_DATA_DIR", os.path.join(os.path.dirname(__file__), "_tmpdata"))

from app.assembly import REDACTED, _resolve_bot_token, redact
from app.routers.admin_api import (
    GLOBAL_FIELDS,
    SECRET_DIFF_FIELDS,
    WRITE_ONLY_FIELDS,
    _apply,
)

TOKEN = "1234567890:AAExampleBotTokenValueDoNotUse"
RTSP = "rtsp://admin:hunter2@192.0.2.10:554/Streaming/Channels/101"


def _blob():
    return {
        "device_id": "jetson-test",
        "config_version": "sha256:abc123",
        "flags": {"telegram_chat_id": "-1001234", "telegram_bot_token": TOKEN},
        "cameras": [
            {"cam_name": "CAM 1", "rtsp_url": RTSP, "enabled": True},
            {"cam_name": "CAM 2", "rtsp_url": None, "enabled": False},
        ],
    }


def _serialized(obj):
    """Everything a caller could read out of the structure, as one string."""
    import json

    return json.dumps(obj, default=str)


# --------------------------------------------------------------- redact() ---

def test_redact_removes_token_and_rtsp():
    out = redact(_blob())
    assert TOKEN not in _serialized(out)
    assert RTSP not in _serialized(out)
    assert "hunter2" not in _serialized(out)
    assert out["flags"]["telegram_bot_token"] == REDACTED
    assert out["cameras"][0]["rtsp_url"] == REDACTED


def test_redact_does_not_mutate_the_input():
    """The same object is cached and served to devices. Mutating it here would
    push a blanked token to the whole fleet."""
    original = _blob()
    redact(original)
    assert original["flags"]["telegram_bot_token"] == TOKEN
    assert original["cameras"][0]["rtsp_url"] == RTSP


def test_redact_keeps_config_version_and_non_secrets():
    out = redact(_blob())
    assert out["config_version"] == "sha256:abc123"
    assert out["flags"]["telegram_chat_id"] == "-1001234"
    assert out["cameras"][0]["cam_name"] == "CAM 1"


def test_redact_handles_none_and_absent_fields():
    assert redact(None) is None
    assert redact({}) == {}                       # no flags, no cameras: no crash
    assert redact({"cameras": None}) == {"cameras": None}
    # A camera with no URL must not gain a fake redacted one -- that would read
    # as "configured but hidden" when the real state is "not configured".
    out = redact(_blob())
    assert out["cameras"][1]["rtsp_url"] is None


# ------------------------------------------------------- _resolve_bot_token ---

def test_device_override_wins_over_fleet():
    g = types.SimpleNamespace(telegram_bot_token="device-token")
    fleet = types.SimpleNamespace(telegram_bot_token="fleet-token")
    assert _resolve_bot_token(g, fleet) == "device-token"


def test_blank_override_falls_back_to_fleet():
    for blank in (None, "", "   "):
        g = types.SimpleNamespace(telegram_bot_token=blank)
        fleet = types.SimpleNamespace(telegram_bot_token="fleet-token")
        assert _resolve_bot_token(g, fleet) == "fleet-token"


def test_no_token_anywhere_is_none_not_empty_string():
    """The edge treats None and "" alike, but None keeps the blob honest and
    keeps config_version stable against a NULL/'' flip in the database."""
    assert _resolve_bot_token(None, None) is None
    assert _resolve_bot_token(types.SimpleNamespace(telegram_bot_token=""),
                              types.SimpleNamespace(telegram_bot_token="")) is None


# ---------------------------------------------------------------- _apply() ---

def test_secret_value_never_enters_the_audit_diff():
    obj = types.SimpleNamespace(telegram_bot_token=None, telegram_chat_id=None)
    diff = _apply(obj, {"telegram_bot_token": TOKEN, "telegram_chat_id": "-100"}, GLOBAL_FIELDS)
    assert obj.telegram_bot_token == TOKEN          # stored
    assert diff["telegram_bot_token"] == REDACTED   # but not audited
    assert TOKEN not in _serialized(diff)
    assert diff["telegram_chat_id"] == "-100"       # non-secrets still audited


def test_blank_secret_submit_is_a_no_op():
    """An untouched password field posts "". If that cleared the token, saving
    any unrelated setting on the globals page would disable Telegram."""
    for blank in ("", "   ", None):
        obj = types.SimpleNamespace(telegram_bot_token=TOKEN)
        diff = _apply(obj, {"telegram_bot_token": blank}, GLOBAL_FIELDS)
        assert obj.telegram_bot_token == TOKEN
        assert "telegram_bot_token" not in diff


def test_explicit_clear_sentinel_unsets_the_secret():
    obj = types.SimpleNamespace(telegram_bot_token=TOKEN)
    diff = _apply(obj, {"telegram_bot_token__clear": True}, GLOBAL_FIELDS)
    assert obj.telegram_bot_token is None
    assert diff["telegram_bot_token"] == "<cleared>"


def test_secret_is_stripped_before_storage():
    """A token pasted from a chat window usually carries whitespace, and a
    trailing newline produces a 404 from Telegram that reads like a bad token."""
    obj = types.SimpleNamespace(telegram_bot_token=None)
    _apply(obj, {"telegram_bot_token": f"  {TOKEN}\n"}, GLOBAL_FIELDS)
    assert obj.telegram_bot_token == TOKEN


def test_rtsp_url_is_redacted_in_diffs_but_still_writable():
    """rtsp_url is not write-only -- the camera form renders and reposts it --
    but its value must not persist into the audit log."""
    obj = types.SimpleNamespace(rtsp_url=None)
    diff = _apply(obj, {"rtsp_url": RTSP}, {"rtsp_url"})
    assert obj.rtsp_url == RTSP
    assert diff["rtsp_url"] == REDACTED
    assert "hunter2" not in _serialized(diff)


# ------------------------------------------------------------ registration ---

def test_token_is_registered_as_both_secret_and_write_only():
    """The two behaviours are separate sets, so it is possible to register a
    field in one and forget the other. Both matter for the token."""
    assert "telegram_bot_token" in SECRET_DIFF_FIELDS
    assert "telegram_bot_token" in WRITE_ONLY_FIELDS
    assert "telegram_bot_token" in GLOBAL_FIELDS
