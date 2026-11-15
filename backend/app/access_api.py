"""Routes for signing in, the account, API keys and the stored credentials."""
from __future__ import annotations

import math
import os
from typing import Callable, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .access import (
    ACCESS,
    LOCKED_DETAIL,
    PASSWORD_MAX,
    SCOPES,
    SESSION_COOKIE,
    SESSION_TTL_S,
    AccessError,
    Principal,
    _same_origin,
    legacy_leftovers,
    require,
)
from .store import NMS, SIMPLE_CREDENTIALS, STORE, StoreLocked

router = APIRouter()
session_only = require("session")

# Called with a credential's name after it was stored or cleared.
changed: List[Callable[[str], None]] = []


class Credentials(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=PASSWORD_MAX)


class AccountChange(BaseModel):
    current_password: str = Field(max_length=PASSWORD_MAX)
    username: Optional[str] = Field(None, max_length=64)
    new_password: Optional[str] = Field(None, max_length=PASSWORD_MAX)


class NewKey(BaseModel):
    name: str = Field(max_length=200)
    scope: Literal["control", "settings"] = "control"


class NmsPair(BaseModel):
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: str = Field(min_length=1, max_length=200)
    environment: Literal["production", "staging", "fit"]


class SecretValue(BaseModel):
    value: str = Field(min_length=1, max_length=400)


def _refuse(exc: Exception):
    if isinstance(exc, StoreLocked):
        raise HTTPException(status_code=503, detail=LOCKED_DETAIL)
    assert isinstance(exc, AccessError)
    headers = {"Retry-After": str(math.ceil(exc.retry_after))} if exc.retry_after else None
    raise HTTPException(status_code=exc.status, detail=exc.detail, headers=headers)


def _set_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL_S, path="/", httponly=True, samesite="strict",
        secure=request.url.scheme == "https",
    )


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _notify(name: str) -> None:
    for hook in changed:
        hook(name)


# ---- signing in -----------------------------------------------------------------
@router.get("/api/auth/session")
def auth_session(request: Request):
    """Whether this browser is signed in, and whether an account exists yet."""
    state = ACCESS.state()
    username = None
    if not state["locked"]:
        try:
            username = ACCESS.session(request.cookies.get(SESSION_COOKIE))
        except StoreLocked:
            state["locked"] = True
    return {
        "authenticated": username is not None,
        "username": username,
        "setup_required": not state["locked"] and not state["account"],
        "locked": state["locked"],
        "detail": LOCKED_DETAIL if state["locked"] else None,
    }


@router.post("/api/auth/setup")
def auth_setup(body: Credentials, request: Request, response: Response):
    """Create the admin account. Works only while there is none."""
    _same_origin(request)
    try:
        token = ACCESS.setup(body.username, body.password)
    except (AccessError, StoreLocked) as exc:
        _refuse(exc)
    _set_cookie(response, request, token)
    return {"ok": True, "username": body.username.strip()}


@router.post("/api/auth/login")
def auth_login(body: Credentials, request: Request, response: Response):
    _same_origin(request)
    try:
        token = ACCESS.login(body.username, body.password, _client(request))
    except (AccessError, StoreLocked) as exc:
        _refuse(exc)
    _set_cookie(response, request, token)
    return {"ok": True, "username": body.username.strip()}


@router.post("/api/auth/logout", dependencies=[Depends(session_only)])
def auth_logout(request: Request, response: Response):
    ACCESS.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/api/auth/logout-all", dependencies=[Depends(session_only)])
def auth_logout_all(response: Response):
    """End every session, this one included."""
    ACCESS.logout(None, everywhere=True)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.put("/api/auth/account")
def auth_account(body: AccountChange, request: Request, who: Principal = Depends(session_only)):
    """Change the username or the password. A new password ends every other session."""
    try:
        username = ACCESS.update_account(
            body.current_password, body.username, body.new_password, request.cookies.get(SESSION_COOKIE))
    except (AccessError, StoreLocked) as exc:
        _refuse(exc)
    return {"ok": True, "username": username}


# ---- API keys ---------------------------------------------------------------------
@router.get("/api/keys", dependencies=[Depends(session_only)])
def keys_list():
    return {"keys": ACCESS.keys(), "scopes": list(SCOPES)}


@router.post("/api/keys", dependencies=[Depends(session_only)])
def keys_create(body: NewKey):
    """Create a key. Its value is in this answer and nowhere afterwards."""
    try:
        return ACCESS.create_key(body.name, body.scope)
    except (AccessError, StoreLocked) as exc:
        _refuse(exc)


@router.delete("/api/keys/{key_id}", dependencies=[Depends(session_only)])
def keys_revoke(key_id: str):
    if not ACCESS.revoke_key(key_id):
        raise HTTPException(status_code=404, detail="no such key")
    return {"ok": True}


# ---- stored credentials --------------------------------------------------------------
@router.get("/api/secrets", dependencies=[Depends(session_only)])
def secrets_state():
    """Which credentials are stored, never their values, and which retired
    variables the environment still sets."""
    return {
        "secrets": STORE.credential_state(),
        "imported": sorted(STORE.data["imported"]),
        "leftovers": legacy_leftovers(os.environ),
    }


@router.put("/api/secrets/nms", dependencies=[Depends(session_only)])
def secrets_put_nms(body: NmsPair):
    """Store the FAA client ID and secret for the environment they were issued for."""
    STORE.set_nms(body.client_id.strip(), body.client_secret.strip(), body.environment)
    _notify(NMS)
    return {"ok": True, "secrets": STORE.credential_state()}


@router.put("/api/secrets/{name}", dependencies=[Depends(session_only)])
def secrets_put(name: str, body: SecretValue):
    if name not in SIMPLE_CREDENTIALS:
        raise HTTPException(status_code=404, detail="no such credential")
    value = body.value.strip()
    if not value:
        raise HTTPException(status_code=422, detail="the value is empty")
    STORE.set_credential(name, value)
    _notify(name)
    return {"ok": True, "secrets": STORE.credential_state()}


@router.delete("/api/secrets/{name}", dependencies=[Depends(session_only)])
def secrets_delete(name: str):
    if name != NMS and name not in SIMPLE_CREDENTIALS:
        raise HTTPException(status_code=404, detail="no such credential")
    STORE.clear_credential(name)
    _notify(name)
    return {"ok": True, "secrets": STORE.credential_state()}
