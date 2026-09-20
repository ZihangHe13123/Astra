"""Astra-owned, single-account Codex OAuth credentials. Never reads other apps' tokens."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from .instance_lock import InstanceAlreadyRunning, InstanceLock
from .paths import state_path
from .network import active_proxy_for_url

ISSUER = "https://auth.openai.com"
BASE_URL = "https://chatgpt.com/backend-api/codex"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # Public device-flow client identifier.
DEVICE_URL = ISSUER + "/codex/device"
TOKEN_URL = ISSUER + "/oauth/token"


class CodexAuthError(ValueError):
    """An actionable, credential-free authentication error."""


def auth_path() -> Path:
    return state_path("auth", "codex.json")


def _read() -> dict:
    try:
        value = json.loads(auth_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(value: dict) -> None:
    path = auth_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".codex-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@asynccontextmanager
async def _locked():
    lock = InstanceLock(auth_path().with_suffix(".lock"))
    async with asyncio.timeout(45):
        while True:
            try:
                lock.acquire()
                break
            except InstanceAlreadyRunning:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            lock.release()


def _claims(token: str) -> dict:
    # Only hints from a token delivered by the TLS-authenticated issuer, not a
    # signature verification or an authorization decision.
    try:
        part = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError):
        return {}


def _credentials(payload: dict, previous: dict | None = None) -> dict:
    previous = previous or {}
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise CodexAuthError("OpenAI returned no access token. Run astra auth login again.")
    claims = _claims(access)
    identity = _claims(str(payload.get("id_token") or ""))
    account = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
    account = account or (identity.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
    account = account or previous.get("account_id")
    if not isinstance(account, str) or not account or any(ord(c) < 32 for c in account):
        raise CodexAuthError("OpenAI returned no ChatGPT account identity. Run astra auth login again.")
    expires = claims.get("exp") or time.time() + float(payload.get("expires_in") or 3600)
    return {"version": 1, "access_token": access,
            "refresh_token": payload.get("refresh_token") or previous.get("refresh_token", ""),
            "account_id": account, "expires_at": float(expires)}


def credential_hint() -> str:
    """Non-secret account-scoped catalog/cache identity; never an access token."""
    data = _read()
    if data.get("reauth_required") or not data.get("access_token") or not data.get("account_id"):
        return ""
    return "codex-oauth:" + hashlib.sha256(str(data["account_id"]).encode()).hexdigest()[:20]


def auth_status() -> dict:
    data = _read()
    return {"connected": bool(credential_hint()), "reauth_required": bool(data.get("reauth_required")),
            "expires_at": data.get("expires_at"), "path": str(auth_path())}


async def logout() -> None:
    async with _locked():
        auth_path().unlink(missing_ok=True)


async def credentials(*, client: httpx.AsyncClient | None = None, rejected_token: str = "") -> dict:
    """Serialize rotating refresh tokens, rereading after the cross-process lock."""
    if client is None:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False,
                                     proxy=active_proxy_for_url(ISSUER)) as owned:
            return await credentials(client=owned, rejected_token=rejected_token)
    async with _locked():
        data = _read()
        if data.get("reauth_required") or not data.get("access_token"):
            raise CodexAuthError("ChatGPT is not signed in. Run astra auth login or use /connect.")
        if (float(data.get("expires_at") or 0) > time.time() + 60
                and (not rejected_token or data["access_token"] != rejected_token)):
            return data
        if not data.get("refresh_token"):
            raise CodexAuthError("ChatGPT login expired. Run astra auth login again.")
        response = await client.post(TOKEN_URL, data={"grant_type": "refresh_token",
            "refresh_token": data["refresh_token"], "client_id": CLIENT_ID})
        if response.status_code in {400, 401, 403}:
            # Do not retry a consumed/revoked rotating token on the next request.
            _write({"version": 1, "reauth_required": True})
            raise CodexAuthError("ChatGPT login was revoked or expired. Run astra auth login again.")
        if response.status_code != 200:
            raise CodexAuthError(f"ChatGPT token refresh failed (HTTP {response.status_code}); try again later.")
        try:
            fresh = _credentials(response.json(), data)
        except (TypeError, ValueError) as exc:
            raise CodexAuthError("Invalid OpenAI token response; run astra auth login again.") from exc
        _write(fresh)
        return fresh


def headers(data: dict) -> dict[str, str]:
    return {"Authorization": "Bearer " + data["access_token"],
            "ChatGPT-Account-ID": data["account_id"],
            "User-Agent": "Astra/0.2.0", "originator": "astra"}


async def device_login(on_progress, *, client: httpx.AsyncClient | None = None,
                       timeout: float = 900) -> None:
    """Show the issuer's one-time code; cancellation never saves partial credentials."""
    if client is None:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False,
                                     proxy=active_proxy_for_url(ISSUER)) as owned:
            await device_login(on_progress, client=owned, timeout=timeout)
        return
    async with asyncio.timeout(timeout):
        response = await client.post(ISSUER + "/api/accounts/deviceauth/usercode", json={"client_id": CLIENT_ID})
        if response.status_code != 200:
            raise CodexAuthError(f"Device login unavailable (HTTP {response.status_code}). Check ChatGPT device-code login settings.")
        challenge = response.json()
        if not challenge.get("device_auth_id") or not challenge.get("user_code"):
            raise CodexAuthError("Invalid OpenAI device authorization response.")
        await on_progress({"verification_uri": DEVICE_URL, "user_code": challenge["user_code"],
                           "expires_in": timeout})
        interval = max(3.0, float(challenge.get("interval") or 5))
        while True:
            await asyncio.sleep(interval)
            response = await client.post(ISSUER + "/api/accounts/deviceauth/token", json={
                "device_auth_id": challenge["device_auth_id"], "user_code": challenge["user_code"]})
            if response.status_code in {403, 404}:
                continue
            if response.status_code != 200:
                raise CodexAuthError(f"Device authorization ended (HTTP {response.status_code}). Run astra auth login again.")
            approval = response.json()
            if not approval.get("authorization_code") or not approval.get("code_verifier"):
                raise CodexAuthError("Invalid OpenAI device approval response.")
            response = await client.post(TOKEN_URL, data={"grant_type": "authorization_code",
                "code": approval["authorization_code"], "code_verifier": approval["code_verifier"],
                "client_id": CLIENT_ID, "redirect_uri": ISSUER + "/deviceauth/callback"})
            if response.status_code != 200:
                raise CodexAuthError(f"OpenAI token exchange failed (HTTP {response.status_code}). Run astra auth login again.")
            fresh = _credentials(response.json())
            async with _locked():
                _write(fresh)
            return


async def fetch_models(*, client: httpx.AsyncClient | None = None) -> list[dict]:
    if client is None:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False,
                                     proxy=active_proxy_for_url(BASE_URL)) as owned:
            return await fetch_models(client=owned)
    data = await credentials(client=client)
    url = BASE_URL + "/models?client_version=0.0.0"
    response = await client.get(url, headers=headers(data))
    if response.status_code == 401:
        data = await credentials(client=client, rejected_token=data["access_token"])
        response = await client.get(url, headers=headers(data))
    if response.status_code != 200:
        raise CodexAuthError(f"ChatGPT model listing failed (HTTP {response.status_code}). Check your account access or sign in again.")
    payload = response.json()
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise CodexAuthError("ChatGPT returned an invalid model catalog.")
    items = []
    for model in models:
        if not isinstance(model, dict) or model.get("visibility") in {"hide", "hidden"}:
            continue
        name = model.get("slug") or model.get("id")
        if not isinstance(name, str) or not name or len(name) > 512 or any(ord(c) < 32 for c in name):
            continue
        levels = [v.get("effort") if isinstance(v, dict) else v
                  for v in model.get("supported_reasoning_levels", [])]
        items.append({"id": name, "context_length": model.get("context_window"),
                      "supported_parameters": ["tools", "reasoning"],
                      "reasoning_levels": [v for v in levels if isinstance(v, str)],
                      "input_modalities": model.get("input_modalities", ["text"]),
                      "default_reasoning_level": model.get("default_reasoning_level")})
    if not items:
        raise CodexAuthError("No Codex models are available for this ChatGPT account.")
    return items
