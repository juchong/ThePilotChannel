"""The secrets store: one JSON file beside the config.

It holds what must never be in config.yaml or in an API response: the admin
account (a password hash), sessions and API keys (hashes of their tokens), the
upstream credentials and the camera stream addresses. Credentials and addresses
are kept in clear because the app has to send them.

The file is written atomically with mode 0600 and one backup. A file that cannot
be read is never treated as "no account yet": the store is then locked, and
everything that needs it refuses until the file is repaired or removed.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from .config import CONFIG_PATH

log = logging.getLogger("hangar.store")

STORE_PATH = os.environ.get("HANGAR_SECRETS") or os.path.join(os.path.dirname(CONFIG_PATH) or ".", "secrets.json")
TOUCH_SAVE_S = 300.0      # last-use times reach the disk at most this often
MAX_STREAMS = 12

SIMPLE_CREDENTIALS = ("llm_api_key", "aggregator_api_key")
NMS = "nms"               # the FAA client ID and secret, with the environment they were issued for


class StoreLocked(RuntimeError):
    """The store file exists but cannot be read."""


def _empty() -> Dict[str, Any]:
    return {"version": 1, "account": None, "sessions": [], "api_keys": [], "credentials": {}, "streams": [], "imported": {}}


class SecretStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or STORE_PATH
        self.lock = threading.RLock()
        self.data: Dict[str, Any] = _empty()
        self.error: Optional[str] = None     # why the store is locked
        self.exists = False                  # a store file has been read or written
        self._stamp: Optional[tuple] = None  # identity of the file as last read or written
        self._dirty = False
        self._saved_at = 0.0
        self._load()

    # ---- file ------------------------------------------------------------
    @property
    def backup(self) -> str:
        return self.path + ".bak"

    def _stat(self) -> Optional[tuple]:
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _load(self) -> None:
        self.data, self.error, self.exists = _empty(), None, False
        self._stamp = self._stat()
        candidates = [p for p in (self.path, self.backup) if os.path.exists(p)]
        problem = None
        for candidate in candidates:
            try:
                with open(candidate) as fh:
                    raw = json.load(fh)
                if not isinstance(raw, dict):
                    raise ValueError("top level is not an object")
            except (OSError, ValueError) as exc:
                problem = problem or f"{candidate}: {exc}"
                log.error("secrets store unreadable: %s: %s", candidate, exc)
                continue
            self.data = {**_empty(), **raw}
            self.exists = True
            if candidate != self.path:
                log.warning("secrets store read from its backup %s", candidate)
            return
        if candidates:
            self.error = problem

    def refresh(self) -> None:
        """Read the file again when something else changed it (the command-line
        tool, a restored copy)."""
        with self.lock:
            if self._stat() != self._stamp:
                self._load()

    def require(self) -> None:
        if self.error:
            raise StoreLocked(self.error)

    def save(self) -> None:
        """Write the store now. The previous file becomes the backup."""
        with self.lock:
            self.require()
            dirname = os.path.dirname(self.path) or "."
            os.makedirs(dirname, exist_ok=True)
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(self.data, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            if os.path.exists(self.path):
                os.replace(self.path, self.backup)
            os.replace(tmp, self.path)
            try:
                dfd = os.open(dirname, os.O_RDONLY)
            except OSError:
                dfd = None
            if dfd is not None:
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            self.exists = True
            self._stamp = self._stat()
            self._dirty = False
            self._saved_at = time.monotonic()

    def touch(self) -> None:
        """Note a change that need not reach the disk at once (a last-use time)."""
        with self.lock:
            self._dirty = True
            if time.monotonic() - self._saved_at >= TOUCH_SAVE_S:
                self.save()

    def flush(self) -> None:
        with self.lock:
            if self._dirty and not self.error:
                self.save()

    # ---- upstream credentials ----------------------------------------------
    def credential(self, name: str) -> str:
        with self.lock:
            entry = self.data["credentials"].get(name)
            return str(entry.get("value") or "") if isinstance(entry, dict) else ""

    def set_credential(self, name: str, value: str) -> None:
        if name not in SIMPLE_CREDENTIALS:
            raise KeyError(name)
        with self.lock:
            self.require()
            self.data["credentials"][name] = {"value": value, "changed": time.time()}
            self.save()

    def nms_pair(self, environment: str) -> tuple[str, str]:
        """The FAA client ID and secret, only for the environment they were stored for."""
        with self.lock:
            entry = self.data["credentials"].get(NMS)
            if not isinstance(entry, dict) or entry.get("environment") != environment:
                return "", ""
            return str(entry.get("client_id") or ""), str(entry.get("client_secret") or "")

    def set_nms(self, client_id: str, client_secret: str, environment: str) -> None:
        with self.lock:
            self.require()
            self.data["credentials"][NMS] = {
                "client_id": client_id, "client_secret": client_secret,
                "environment": environment, "changed": time.time(),
            }
            self.save()

    def clear_credential(self, name: str) -> bool:
        if name != NMS and name not in SIMPLE_CREDENTIALS:
            raise KeyError(name)
        with self.lock:
            self.require()
            if name not in self.data["credentials"]:
                return False
            del self.data["credentials"][name]
            self.save()
            return True

    def credential_state(self) -> Dict[str, Dict]:
        """Which credentials are stored and when they changed; never a value."""
        with self.lock:
            creds = self.data["credentials"]
            out: Dict[str, Dict] = {}
            for name in SIMPLE_CREDENTIALS:
                entry = creds.get(name) if isinstance(creds.get(name), dict) else {}
                out[name] = {"set": bool(entry.get("value")), "changed": entry.get("changed")}
            entry = creds.get(NMS) if isinstance(creds.get(NMS), dict) else {}
            out[NMS] = {
                "set": bool(entry.get("client_id") and entry.get("client_secret")),
                "environment": entry.get("environment"),
                "changed": entry.get("changed"),
            }
            return out

    # ---- camera streams ------------------------------------------------------
    def streams(self) -> List[Dict]:
        with self.lock:
            return [dict(s) for s in self.data["streams"]]

    def stream(self, stream_id: str) -> Optional[Dict]:
        with self.lock:
            return next((dict(s) for s in self.data["streams"] if s["id"] == stream_id), None)

    def add_stream(self, name: str, url: str, stream_id: Optional[str] = None) -> Dict:
        """Store a stream under the lowest free camN id (or the id given)."""
        with self.lock:
            self.require()
            taken = {s["id"] for s in self.data["streams"]}
            if stream_id is None:
                stream_id = next((f"cam{n}" for n in range(1, MAX_STREAMS + 1) if f"cam{n}" not in taken), None)
            if stream_id is None or len(taken) >= MAX_STREAMS:
                raise ValueError(f"at most {MAX_STREAMS} streams")
            if stream_id in taken:
                raise ValueError(f"stream {stream_id} exists")
            entry = {"id": stream_id, "name": name, "url": url, "changed": time.time()}
            self.data["streams"].append(entry)
            self.data["streams"].sort(key=lambda s: (len(s["id"]), s["id"]))
            self.save()
            return dict(entry)

    def update_stream(self, stream_id: str, name: Optional[str] = None, url: Optional[str] = None) -> Optional[Dict]:
        with self.lock:
            self.require()
            entry = next((s for s in self.data["streams"] if s["id"] == stream_id), None)
            if entry is None:
                return None
            if name is not None:
                entry["name"] = name
            if url is not None:
                entry["url"] = url
            entry["changed"] = time.time()
            self.save()
            return dict(entry)

    def remove_stream(self, stream_id: str) -> bool:
        with self.lock:
            self.require()
            before = len(self.data["streams"])
            self.data["streams"] = [s for s in self.data["streams"] if s["id"] != stream_id]
            if len(self.data["streams"]) == before:
                return False
            self.save()
            return True

    # ---- values every response and log line must stay free of -----------------
    def secret_values(self) -> List[str]:
        with self.lock:
            creds = self.data["credentials"]
            out = [self.credential(n) for n in SIMPLE_CREDENTIALS]
            nms = creds.get(NMS) if isinstance(creds.get(NMS), dict) else {}
            out += [str(nms.get("client_id") or ""), str(nms.get("client_secret") or "")]
            out += [s["url"] for s in self.data["streams"]]
            return [v for v in out if v]


STORE = SecretStore()
