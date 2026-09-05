import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import settings
from app.encryption import encrypt, decrypt

logger = logging.getLogger(__name__)


class PyroAuthService:

    def __init__(
        self,
        api_key:    str,
        login_id:   str,
        password:   str,
        secret_key: str,
        base_url:   str = "",
        label:      str = "",   # e.g. "FRC", "FANCYSALE" — used in log messages
    ):
        self.api_key    = api_key
        self.login_id   = login_id
        self.password   = password
        self.secret_key = secret_key
        self.base_url   = base_url or settings.pyro_base_url
        self.label      = label

        self.session_token:     Optional[str]   = None
        self.access_token:      Optional[str]   = None
        self._access_token_exp: Optional[float] = None  # Unix timestamp from JWT
        self._auth_lock  = asyncio.Lock()   # serialises authenticate() (full re-login)
        self._token_lock = asyncio.Lock()   # serialises get_access_token() checks


    def _base_headers(self) -> dict:
        return {"apiKey": self.api_key}
    
    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(settings.pyro_request_timeout_seconds)
        
    def _parse_jwt_exp(self, token: str) -> Optional[float]:
        
        try:
            part = token.split(".")[1]
            part += "=" * (-len(part) % 4)
            return float(json.loads(base64.b64decode(part)).get("exp", 0))
        except Exception:
            return None

    def _is_access_token_valid(self) -> bool:
        if not self.access_token or not self._access_token_exp:
            return False
        return self._access_token_exp > datetime.now(timezone.utc).timestamp() + 60

    def _parse_pyro_response(self, resp: httpx.Response, label: str) -> dict:
        
        raw = resp.text.strip()
        try:
            return resp.json()
        except Exception as json_err:
            logger.debug("[%s] %s: plain JSON parse failed (%s); trying encrypted response",
                         self.label, label, json_err)

        try:
            return json.loads(decrypt(raw, self.secret_key))
        except Exception as dec_err:
            logger.error("[%s] %s: both plain JSON and decrypt parse failed. Raw: %s",
                         self.label, label, raw[:200])
            return {"statusCode": -1, "message": f"Response parse failed: {dec_err}"}

    # ── Public API ─────────────────────────────────────────────────────────────

    async def authenticate(self) -> bool:
        
        async with self._auth_lock:
            body = {"loginId": self.login_id, "password": self.password}
            encrypted_body = encrypt(json.dumps(body), self.secret_key)
            try:
                async with httpx.AsyncClient(verify=True, timeout=self._timeout()) as client:
                    resp = await client.post(
                        f"{self.base_url}/auth-api/authentication",
                        headers={**self._base_headers(), "Content-Type": "application/json"},
                        content=encrypted_body,
                    )
            except httpx.TimeoutException as exc:
                logger.error(
                        "Pyro authentication request timed out after %.1fs: %s",
                        settings.pyro_request_timeout_seconds,
                        type(exc).__name__,
                    )
                return False
            except httpx.RequestError as exc:
                logger.error(
                        "Pyro authentication request failed with error: %s",
                        type(exc).__name__,
                    )
                return False

            data = self._parse_pyro_response(resp, "AUTH")

            if data.get("statusCode") == 2000:
                d = data["data"]
                self.session_token     = d["sessionToken"]
                self.access_token      = d["accessToken"]
                self._access_token_exp = self._parse_jwt_exp(self.access_token)
                logger.info("[%s] Pyro authentication successful — user: %s",
                            self.label, d.get("userName"))
                return True

            logger.error("[%s] Pyro authentication failed: %s — %s",
                        self.label, data.get("statusCode"), data.get("message"))
            return False

    async def refresh_access_token(self) -> bool:
        
        if not self.session_token or not self.access_token:
            logger.warning("refresh_access_token called before authenticate - re-authenticating")
            return await self.authenticate()
        try:
            async with httpx.AsyncClient(verify=True, timeout=self._timeout()) as client:
                resp = await client.get(
                    f"{self.base_url}/auth-api/refresh-access-token",
                    headers={
                        **self._base_headers(),
                        "sessionToken": self.session_token,
                        "accessToken":  self.access_token,
                    },
                )
            data = self._parse_pyro_response(resp, "REFRESH_ACCESS_TOKEN")
        except httpx.TimeoutException as exc:
            logger.error(
                    "Pyro refresh access token request timed out after %.1fs: %s",
                    settings.pyro_request_timeout_seconds,
                    type(exc).__name__,
                )
            return False
        except httpx.RequestError as exc:
            logger.error(
                    "Pyro refresh access token request failed with error: %s",
                    type(exc).__name__,
                )
            return False
        if data.get("statusCode") == 2000:
            self.access_token      = data["data"]["accessToken"]
            self._access_token_exp = self._parse_jwt_exp(self.access_token)
            logger.debug("[%s] Access token refreshed", self.label)
            return True

        logger.error("[%s] Token refresh failed: %s — %s",
                     self.label, data.get("statusCode"), data.get("message"))
        return False

    
    async def get_access_token(self) -> Optional[str]:
       
        async with self._token_lock:
            if not self._is_access_token_valid():
                await self.refresh_access_token()
            return self.access_token