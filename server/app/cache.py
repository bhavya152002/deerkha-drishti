"""
cache.py -- in-process config blob cache.

Why this exists
---------------
Assembling one device's config blob costs 8 DB statements plus a full
json.dumps + SHA-256 (see assembly.assemble_config). The edge polls
/api/config/<id>/version every ~30s per device, and that endpoint used to run
the entire assembly just to return a ~200 byte version string. At 7 devices
that is ~2.1 remote-Postgres round-trips per second, permanently, to answer a
question whose answer is already stored in cfg_devices.config_version.

Design: content-addressed
-------------------------
The cache key is (device_id, config_version) -- and config_version IS the
SHA-256 of the blob's contents. That makes the cache structurally incapable of
serving stale data:

  * If the config changes, config_version changes, so the key changes, so the
    old entry is simply never looked up again.
  * There is no invalidation protocol to get wrong, no TTL to tune, and no
    coherence problem between multiple uvicorn workers -- a second worker with
    a cold cache does more work, never wrong work.

The only way to serve a stale blob would be for two different configs to hash
to the same SHA-256.

Bounded
-------
One entry per device per version. put() drops any other version for that
device, so steady-state size is one blob per device.
"""

import threading

# (device_id, config_version) -> assembled blob
_BLOBS: dict[tuple[str, str], dict] = {}
_LOCK = threading.Lock()


def get(device_id: str, version: str | None) -> dict | None:
    """Return the cached blob for this exact version, or None."""
    if not version:
        return None
    return _BLOBS.get((device_id, version))


def put(device_id: str, version: str | None, blob: dict) -> None:
    """Cache a blob and evict any older version for the same device."""
    if not version or blob is None:
        return
    with _LOCK:
        for key in [k for k in _BLOBS if k[0] == device_id and k[1] != version]:
            _BLOBS.pop(key, None)
        _BLOBS[(device_id, version)] = blob


def drop(device_id: str) -> None:
    """Forget everything for a device. Only needed when the device row itself
    goes away -- config edits are handled by the version changing."""
    with _LOCK:
        for key in [k for k in _BLOBS if k[0] == device_id]:
            _BLOBS.pop(key, None)


def stats() -> dict:
    return {"entries": len(_BLOBS)}
