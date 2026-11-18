"""Who may do what: the admin account, browser sessions, API keys, and the
dependencies that put every route in one access class.

    open      anyone on the network (the kiosk has no credentials)
    control   a signed-in session, or any API key
    settings  a signed-in session, or an API key of scope "settings"
    session   a signed-in session only (accounts, keys, secrets, stream addresses)

Passwords are stored as scrypt hashes; session and API key tokens as SHA-256 of
a random value that is shown or sent once. `python -m app.access set-password`
creates the account or replaces a forgotten password.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import sys
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from .config import NMS_HOSTS, validate_stream_url
from .store import MAX_STREAMS, NMS, STORE, SecretStore, StoreLocked

log = logging.getLogger("hangar.access")

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
PASSWORD_MIN = 8
PASSWORD_MAX = 256
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 2 ** 15, 8, 1, 32

SESSION_COOKIE = "tpc_session"
SESSION_TTL_S = 30 * 86400
SESSION_SLIDE_S = 3600        # a session's expiry moves at most this often
MAX_SESSIONS = 20

KEY_PREFIX = "tpc_"
KEY_SHOWN = 8                 # leading characters of a key kept to tell keys apart
MAX_KEYS = 20
SCOPES = ("control", "settings")
KEY_NAME_MAX = 60

LOCKED_DETAIL = (
    "the secrets store cannot be read; repair or remove data/secrets.json, "
    "then run: docker compose exec app python -m app.access set-password"
)


class AccessError(Exception):
    def __init__(self, status: int, detail: str, retry_after: Optional[float] = None):
        super().__init__(detail)
        self.status, self.detail, self.retry_after = status, detail, retry_after


# ---- password hashing ------------------------------------------------------------
def scrypt(password: bytes, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(password, salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=max(64 << 20, 256 * n * r))


def hash_password(password: str) -> Dict[str, Any]:
    salt = os.urandom(16)
    digest = scrypt(password.encode("utf-8"), salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
    return {
        "algo": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
        "salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(digest).decode(),
    }


def verify_password(password: str, record: Any) -> bool:
    try:
        salt = base64.b64decode(record["salt"])
        want = base64.b64decode(record["hash"])
        got = scrypt(password.encode("utf-8"), salt, int(record["n"]), int(record["r"]), int(record["p"]), len(want))
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(got, want)


_DUMMY = hash_password("no such account")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise AccessError(422, "username: 1 to 32 letters, digits, '.', '_' or '-'")
    return username


def check_password(password: str) -> str:
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise AccessError(422, f"password: {PASSWORD_MIN} to {PASSWORD_MAX} characters")
    return password


# ---- sign-in throttle --------------------------------------------------------------
class LoginThrottle:
    """Failed sign-ins lock further attempts for a growing time, per client
    address and for everyone once many clients fail."""
    FREE = 5
    GLOBAL_FREE = 20
    BASE_S = 5.0
    MAX_S = 300.0
    FORGET_S = 900.0
    MAX_CLIENTS = 1024

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._clients: Dict[str, List[float]] = {}   # address -> [failures, locked until, last failure]
        self._all: List[float] = [0, 0.0, 0.0]

    def _live(self, rec: List[float]) -> List[float]:
        if rec[0] and self.clock() - rec[2] > self.FORGET_S:
            rec[0], rec[1] = 0, 0.0
        return rec

    def retry_after(self, client: str) -> float:
        now = self.clock()
        until = max(self._live(self._clients.get(client, [0, 0.0, 0.0]))[1], self._live(self._all)[1])
        return max(0.0, until - now)

    def failure(self, client: str) -> None:
        now = self.clock()
        if client not in self._clients and len(self._clients) >= self.MAX_CLIENTS:
            self._clients.pop(min(self._clients, key=lambda k: self._clients[k][2]))
        for rec, free in ((self._live(self._clients.setdefault(client, [0, 0.0, 0.0])), self.FREE),
                          (self._live(self._all), self.GLOBAL_FREE)):
            rec[0] += 1
            rec[2] = now
            if rec[0] >= free:
                rec[1] = now + min(self.MAX_S, self.BASE_S * 2 ** min(rec[0] - free, 16))

    def success(self, client: str) -> None:
        self._clients.pop(client, None)
        self._all = [0, 0.0, 0.0]


# ---- the account, sessions and keys ---------------------------------------------------
class Access:
    def __init__(self, store: SecretStore, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock
        self.throttle = LoginThrottle()

    def _data(self) -> Dict[str, Any]:
        self.store.refresh()
        self.store.require()
        return self.store.data

    def state(self) -> Dict[str, Any]:
        with self.store.lock:
            self.store.refresh()
            account = None if self.store.error else self.store.data.get("account")
            return {
                "locked": bool(self.store.error),
                "account": bool(account),
                "username": account["username"] if account else None,
            }

    # -- account
    def create_account(self, username: str, password: str, policy: bool = True) -> None:
        username = check_username(username)
        if policy:
            check_password(password)
        elif not password:
            raise AccessError(422, "password must not be empty")
        with self.store.lock:
            data = self._data()
            if data.get("account"):
                raise AccessError(409, "an account exists already")
            data["account"] = {"username": username, "password": hash_password(password), "changed": self.clock()}
            self.store.save()
        log.info("admin account %s created", username)

    def set_password(self, username: Optional[str], password: str) -> str:
        """Create the account or replace its password (the command-line tool).
        Every session ends."""
        check_password(password)
        with self.store.lock:
            data = self._data()
            current = data.get("account") or {}
            username = check_username(username or current.get("username") or "admin")
            data["account"] = {"username": username, "password": hash_password(password), "changed": self.clock()}
            data["sessions"] = []
            self.store.save()
        return username

    def update_account(self, current_password: str, username: Optional[str], new_password: Optional[str],
                       keep_token: Optional[str]) -> str:
        with self.store.lock:
            data = self._data()
            account = data.get("account")
            if not account or not verify_password(current_password or "", account["password"]):
                raise AccessError(403, "the current password is not right")
            if username is not None:
                account["username"] = check_username(username)
            if new_password:
                account["password"] = hash_password(check_password(new_password))
                keep = _digest(keep_token) if keep_token else None
                data["sessions"] = [s for s in data["sessions"] if s["hash"] == keep]
            account["changed"] = self.clock()
            self.store.save()
            return account["username"]

    # -- sessions
    def login(self, username: str, password: str, client: str) -> str:
        wait = self.throttle.retry_after(client)
        if wait > 0:
            raise AccessError(429, "too many failed sign-ins; wait and try again", retry_after=wait)
        with self.store.lock:
            data = self._data()
            account = data.get("account")
            record = account["password"] if account else _DUMMY
            ok = verify_password(password or "", record) and bool(account) and hmac.compare_digest(
                (username or "").strip().encode("utf-8"), account["username"].encode("utf-8"))
            if not ok:
                self.throttle.failure(client)
                raise AccessError(401, "the username or password is not right")
            self.throttle.success(client)
            return self._open_session(data)

    def _open_session(self, data: Dict[str, Any]) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        live = [s for s in data["sessions"] if s["expires"] > now]
        live.sort(key=lambda s: s["seen"])
        data["sessions"] = live[-(MAX_SESSIONS - 1):] if MAX_SESSIONS > 1 else []
        data["sessions"].append({"hash": _digest(token), "created": now, "seen": now, "expires": now + SESSION_TTL_S})
        self.store.save()
        return token

    def setup(self, username: str, password: str) -> str:
        """Create the first account and sign it in."""
        with self.store.lock:
            self.create_account(username, password)
            return self._open_session(self._data())

    def session(self, token: Optional[str]) -> Optional[str]:
        """The username of the session the token opens, or None."""
        if not token:
            return None
        with self.store.lock:
            data = self._data()
            account = data.get("account")
            if not account:
                return None
            digest, now = _digest(token), self.clock()
            for s in data["sessions"]:
                if hmac.compare_digest(s["hash"], digest):
                    if s["expires"] <= now:
                        return None
                    if now - s["seen"] >= SESSION_SLIDE_S:
                        s["seen"], s["expires"] = now, now + SESSION_TTL_S
                        self.store.touch()
                    return account["username"]
            return None

    def logout(self, token: Optional[str], everywhere: bool = False) -> None:
        with self.store.lock:
            data = self._data()
            digest = _digest(token) if token else None
            kept = [] if everywhere else [s for s in data["sessions"] if s["hash"] != digest]
            if len(kept) != len(data["sessions"]):
                data["sessions"] = kept
                self.store.save()

    # -- API keys
    @staticmethod
    def _public(entry: Dict[str, Any]) -> Dict[str, Any]:
        return {k: entry.get(k) for k in ("id", "name", "prefix", "scope", "created", "last_used")}

    def keys(self) -> List[Dict[str, Any]]:
        with self.store.lock:
            return [self._public(k) for k in self._data()["api_keys"]]

    def create_key(self, name: str, scope: str) -> Dict[str, Any]:
        name = " ".join((name or "").split())
        if not 1 <= len(name) <= KEY_NAME_MAX:
            raise AccessError(422, f"name: 1 to {KEY_NAME_MAX} characters")
        if scope not in SCOPES:
            raise AccessError(422, "scope: control or settings")
        with self.store.lock:
            data = self._data()
            if len(data["api_keys"]) >= MAX_KEYS:
                raise AccessError(409, f"at most {MAX_KEYS} keys; revoke one first")
            key = KEY_PREFIX + secrets.token_urlsafe(32)
            entry = {
                "id": secrets.token_hex(6), "name": name, "prefix": key[:KEY_SHOWN], "hash": _digest(key),
                "scope": scope, "created": self.clock(), "last_used": None,
            }
            data["api_keys"].append(entry)
            self.store.save()
        log.info("API key %r created (scope %s)", name, scope)
        return {**self._public(entry), "key": key}

    def revoke_key(self, key_id: str) -> bool:
        with self.store.lock:
            data = self._data()
            kept = [k for k in data["api_keys"] if k["id"] != key_id]
            if len(kept) == len(data["api_keys"]):
                return False
            data["api_keys"] = kept
            self.store.save()
        log.info("API key %s revoked", key_id)
        return True

    def key(self, token: str) -> Optional[Dict[str, Any]]:
        """The key entry a presented token belongs to, or None."""
        if not token or not token.startswith(KEY_PREFIX):
            return None
        with self.store.lock:
            data = self._data()
            digest = _digest(token)
            for k in data["api_keys"]:
                if hmac.compare_digest(k["hash"], digest):
                    k["last_used"] = self.clock()
                    self.store.touch()
                    return self._public(k)
            return None


ACCESS = Access(STORE)


# ---- route dependencies ------------------------------------------------------------------
class Principal(NamedTuple):
    kind: str      # "session" or "key"
    scope: str     # "session", "control" or "settings"
    name: str


def _same_origin(request: Request) -> None:
    """A cookie is sent by the browser whoever wrote the page, so a write that
    carries one must come from this site's own pages."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if origin is not None:
        if urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
            raise HTTPException(status_code=403, detail="cross-origin request refused")
        return
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="cross-origin request refused")


def principal(request: Request) -> Optional[Principal]:
    header = request.headers.get("authorization")
    if header:
        scheme, _, value = header.partition(" ")
        entry = ACCESS.key(value.strip()) if scheme.lower() == "bearer" else None
        if entry is None:
            raise HTTPException(status_code=401, detail="API key not recognized", headers={"WWW-Authenticate": "Bearer"})
        return Principal("key", entry["scope"], entry["name"])
    username = ACCESS.session(request.cookies.get(SESSION_COOKIE))
    if username is None:
        return None
    _same_origin(request)
    return Principal("session", "session", username)


def require(level: str):
    """The dependency of a route in access class `level`."""
    def dependency(request: Request) -> Principal:
        try:
            who = principal(request)
        except StoreLocked:
            raise HTTPException(status_code=503, detail=LOCKED_DETAIL)
        if who is None:
            raise HTTPException(status_code=401, detail="sign in, or send an API key",
                                headers={"WWW-Authenticate": "Bearer"})
        if level == "session" and who.kind != "session":
            raise HTTPException(status_code=403, detail="this needs a signed-in browser session, not an API key")
        if level == "settings" and who.kind == "key" and who.scope != "settings":
            raise HTTPException(status_code=403, detail="this API key may control the display only")
        return who

    dependency.access_class = level   # type: ignore[attr-defined]
    return dependency


# ---- values that used to live in .env ---------------------------------------------------
LEGACY_ENV = (
    ("HANGAR_ADMIN_PASSWORD", "ADMIN_PASSWORD"),
    ("HANGAR_NMS_CLIENT_ID",), ("HANGAR_NMS_CLIENT_SECRET",), ("HANGAR_LLM_API_KEY",),
    *((f"HANGAR_CAM{n}",) for n in range(1, MAX_STREAMS + 1)),
)
CONFIG_API_KEY = "config:data_source.api_key"


def _env(environ, names) -> str:
    return next((environ[n] for n in names if environ.get(n)), "")


def legacy_environment(environ) -> str:
    """The NMS environment a HANGAR_NMS_BASE setting points at (production when unset)."""
    base = (environ.get("HANGAR_NMS_BASE") or "").strip().rstrip("/").lower()
    return next((env for env, host in NMS_HOSTS.items() if base == host), "production")


def import_legacy(store: SecretStore, environ, raw_config: Dict[str, Any]) -> List[str]:
    """Copy each value an earlier version read from the environment (or kept in
    config.yaml) into the store, once. A value imported before is never imported
    again, so clearing it in the store is not undone by a restart."""
    if store.error:
        return []
    done: List[str] = []
    with store.lock:
        store.refresh()
        data, imported = store.data, store.data["imported"]

        def take(name: str) -> bool:
            return name not in imported

        password = _env(environ, LEGACY_ENV[0])
        if password and take("HANGAR_ADMIN_PASSWORD") and not data.get("account"):
            data["account"] = {"username": "admin", "password": hash_password(password), "changed": time.time()}
            done.append("HANGAR_ADMIN_PASSWORD")

        client_id, secret = environ.get("HANGAR_NMS_CLIENT_ID", ""), environ.get("HANGAR_NMS_CLIENT_SECRET", "")
        pairs = data["credentials"].get(NMS) or {}
        environment = legacy_environment(environ)
        if client_id and secret and take("HANGAR_NMS_CLIENT_ID") and environment not in pairs:
            data["credentials"][NMS] = {
                **pairs, environment: {"client_id": client_id, "client_secret": secret, "changed": time.time()},
            }
            done += ["HANGAR_NMS_CLIENT_ID", "HANGAR_NMS_CLIENT_SECRET"]

        key = environ.get("HANGAR_LLM_API_KEY", "")
        if key and take("HANGAR_LLM_API_KEY") and "llm_api_key" not in data["credentials"]:
            data["credentials"]["llm_api_key"] = {"value": key, "changed": time.time()}
            done.append("HANGAR_LLM_API_KEY")

        source = raw_config.get("data_source") if isinstance(raw_config.get("data_source"), dict) else {}
        key = source.get("api_key")
        if isinstance(key, str) and key and take(CONFIG_API_KEY) and "aggregator_api_key" not in data["credentials"]:
            data["credentials"]["aggregator_api_key"] = {"value": key, "changed": time.time()}
            done.append(CONFIG_API_KEY)

        cameras = raw_config.get("cameras") if isinstance(raw_config.get("cameras"), dict) else {}
        captions = {t.get("stream"): t.get("name") for t in cameras.get("tiles") or [] if isinstance(t, dict)}
        taken = {s["id"] for s in data["streams"]}
        for n in range(1, MAX_STREAMS + 1):
            name, stream_id = f"HANGAR_CAM{n}", f"cam{n}"
            url = environ.get(name, "")
            if not url or not take(name) or stream_id in taken:
                continue
            try:
                url = validate_stream_url(url)
            except ValueError as exc:
                log.error("%s not imported: %s", name, exc)
                continue
            data["streams"].append({
                "id": stream_id, "name": captions.get(stream_id) or f"Camera {n}", "url": url, "changed": time.time(),
            })
            done.append(name)
        data["streams"].sort(key=lambda s: (len(s["id"]), s["id"]))

        if done:
            for name in done:
                imported[name] = time.time()
            store.save()
    for name in done:
        log.info("%s imported into the secrets store; it is no longer read from its old place", name)
    return done


def legacy_leftovers(environ) -> List[str]:
    """The retired variables the environment still sets."""
    return [names[0] for names in LEGACY_ENV if _env(environ, names)]


# ---- command line ------------------------------------------------------------------------
def _read_password() -> str:
    if sys.stdin.isatty():
        import getpass
        first = getpass.getpass("New password: ")
        if getpass.getpass("Repeat it: ") != first:
            raise AccessError(422, "the two entries differ")
        return first
    return sys.stdin.readline().rstrip("\r\n")


def main(argv: List[str]) -> int:
    usage = "usage: python -m app.access set-password [--username NAME] | status"
    if not argv or argv[0] not in ("set-password", "status"):
        print(usage, file=sys.stderr)
        return 2
    access = ACCESS
    if argv[0] == "status":
        state = access.state()
        if state["locked"]:
            print("the secrets store cannot be read")
            return 2
        print(f"account: {state['username']}" if state["account"] else "no account")
        return 0 if state["account"] else 1
    username = None
    if len(argv) >= 3 and argv[1] == "--username":
        username = argv[2]
    elif len(argv) != 1:
        print(usage, file=sys.stderr)
        return 2
    try:
        username = access.set_password(username, _read_password())
    except StoreLocked as exc:
        print(f"the secrets store cannot be read: {exc}", file=sys.stderr)
        return 2
    except AccessError as exc:
        print(exc.detail, file=sys.stderr)
        return 1
    print(f"password set for {username}; every session was signed out")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
