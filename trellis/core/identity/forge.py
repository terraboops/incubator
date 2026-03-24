"""Forge token exchangers — trade OIDC JWTs for Git forge access tokens.

Each exchanger implements the OIDC-to-forge token exchange flow for a
specific forge type (GitHub, GitLab, Forgejo).
"""

from __future__ import annotations

import logging
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Token fingerprint: last 4 chars for audit logging without exposing secrets
_TOKEN_MASK_LEN = 4

# Retry configuration
_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0  # seconds
_MAX_BACKOFF = 16.0


@dataclass
class ForgeCredential:
    """A short-lived forge access token with metadata."""

    token: str
    expires_at: float  # Unix timestamp
    scopes: list[str]
    forge_type: str

    @property
    def fingerprint(self) -> str:
        """Last few chars of the token, safe for logging."""
        if len(self.token) > _TOKEN_MASK_LEN:
            return f"****{self.token[-_TOKEN_MASK_LEN:]}"
        return "****"

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def __repr__(self) -> str:
        return (
            f"ForgeCredential(token={self.fingerprint!r}, "
            f"expires_at={self.expires_at}, "
            f"scopes={self.scopes!r}, forge_type={self.forge_type!r})"
        )

    def __str__(self) -> str:
        return repr(self)

    def __del__(self) -> None:
        # Best-effort memory cleanup — not a security guarantee
        try:
            if hasattr(self, "token") and isinstance(self.token, str):
                # Can't truly zero a Python str, but replace the reference
                object.__setattr__(self, "token", "x" * len(self.token))
        except Exception:
            pass


def _validate_forge_url(url: str) -> None:
    """Enforce HTTPS-only forge URLs."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(
            f"Forge URL must use HTTPS, got {parsed.scheme!r}: {url}"
        )


def _validate_repos(repos: list[str]) -> None:
    """Validate repo entries are owner/repo format, no wildcards."""
    for repo in repos:
        if "*" in repo or "?" in repo:
            raise ValueError(f"Forge repo must not contain wildcards: {repo!r}")
        if "/" not in repo or repo.count("/") != 1:
            raise ValueError(
                f"Forge repo must be in owner/repo format: {repo!r}"
            )


def _validate_branch_pattern(pattern: str) -> None:
    """Validate that branch pattern is a valid regex."""
    if not pattern:
        return
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Invalid branch pattern regex {pattern!r}: {e}") from e


class ForgeTokenExchanger(ABC):
    """Abstract base for forge-specific OIDC token exchange."""

    @abstractmethod
    async def exchange(
        self,
        oidc_token: str,
        repos: list[str] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> ForgeCredential:
        """Exchange an OIDC token for a forge access token."""


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    """HTTP request with exponential backoff on 429/5xx."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < _MAX_RETRIES:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            delay = _BASE_BACKOFF * (2**attempt)
                    else:
                        delay = _BASE_BACKOFF * (2**attempt)
                    delay = min(delay, _MAX_BACKOFF)
                    # Add jitter
                    delay *= 0.5 + random.random()
                    logger.warning(
                        "Forge API returned %d, retrying in %.1fs (attempt %d/%d)",
                        resp.status_code, delay, attempt + 1, _MAX_RETRIES,
                    )
                    import asyncio
                    await asyncio.sleep(delay)
                    continue
            return resp
        except httpx.TransportError as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                delay = _BASE_BACKOFF * (2**attempt) * (0.5 + random.random())
                logger.warning(
                    "Forge API transport error: %s, retrying in %.1fs", e, delay,
                )
                import asyncio
                await asyncio.sleep(delay)
    raise last_exc or RuntimeError("Request failed after retries")


def _build_httpx_client(forge_url: str) -> httpx.AsyncClient:
    """Build an httpx client with security-hardened defaults."""
    return httpx.AsyncClient(
        base_url=forge_url,
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        follow_redirects=False,
        verify=True,
    )


class GitHubTokenExchanger(ForgeTokenExchanger):
    """Exchange OIDC tokens for GitHub App installation access tokens.

    Flow:
    1. Sign a JWT assertion using the GitHub App private key
    2. POST /app/installations/{id}/access_tokens with the assertion
    3. Receive a scoped installation token
    """

    def __init__(
        self,
        forge_url: str,
        app_id: str,
        installation_id: str,
        private_key_path: str,
    ) -> None:
        _validate_forge_url(forge_url)
        self._forge_url = forge_url
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key_path = private_key_path
        self._validate_private_key_permissions()
        self._api_url = forge_url.replace("https://github.com", "https://api.github.com")

    def _validate_private_key_permissions(self) -> None:
        """Ensure the PEM file isn't world or group readable."""
        import os
        import stat as stat_mod
        if not os.path.exists(self._private_key_path):
            raise FileNotFoundError(
                f"GitHub App private key not found: {self._private_key_path}"
            )
        mode = os.stat(self._private_key_path).st_mode
        if mode & (stat_mod.S_IRGRP | stat_mod.S_IROTH):
            raise PermissionError(
                f"GitHub App private key is group/world readable "
                f"(mode {oct(mode)}): {self._private_key_path}. "
                f"Run: chmod 600 {self._private_key_path}"
            )

    def _create_jwt_assertion(self) -> str:
        """Create a GitHub App JWT signed with the private key."""
        import json
        import base64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        import struct

        with open(self._private_key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)

        now = int(time.time())
        # GitHub allows up to 10 minute expiry for App JWTs
        payload = {
            "iat": now - 60,  # issued 60s ago to account for clock skew
            "exp": now + 600,  # 10 minute expiry
            "iss": self._app_id,
        }

        # Build JWT manually to avoid PyJWT dependency
        header = {"alg": "RS256", "typ": "JWT"}
        segments = []
        for part in (header, payload):
            encoded = base64.urlsafe_b64encode(
                json.dumps(part, separators=(",", ":")).encode()
            ).rstrip(b"=")
            segments.append(encoded)

        signing_input = b".".join(segments)
        signature = private_key.sign(signing_input, asym_padding.PKCS1v15(), hashes.SHA256())
        segments.append(base64.urlsafe_b64encode(signature).rstrip(b"="))
        return b".".join(segments).decode()

    async def exchange(
        self,
        oidc_token: str,
        repos: list[str] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> ForgeCredential:
        if repos:
            _validate_repos(repos)

        jwt_assertion = self._create_jwt_assertion()

        body: dict = {}
        if repos:
            # GitHub expects just the repo names, not owner/repo
            body["repositories"] = [r.split("/", 1)[1] for r in repos]
        if permissions:
            body["permissions"] = permissions

        async with _build_httpx_client(self._api_url) as client:
            resp = await _request_with_retry(
                client,
                "POST",
                f"/app/installations/{self._installation_id}/access_tokens",
                json=body if body else None,
                headers={
                    "Authorization": f"Bearer {jwt_assertion}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        token = data["token"]
        # Parse expiry — GitHub returns ISO 8601
        expires_str = data.get("expires_at", "")
        if expires_str:
            expires_at = datetime.fromisoformat(
                expires_str.replace("Z", "+00:00")
            ).timestamp()
        else:
            # Default 1 hour if not provided
            expires_at = time.time() + 3600

        scopes = list((data.get("permissions") or {}).keys())

        cred = ForgeCredential(
            token=token,
            expires_at=expires_at,
            scopes=scopes,
            forge_type="github",
        )
        logger.info(
            "GitHub token exchanged: %s, expires=%s, scopes=%s",
            cred.fingerprint,
            datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
            scopes,
        )
        return cred


class GitLabTokenExchanger(ForgeTokenExchanger):
    """Exchange OIDC tokens for GitLab access via token exchange.

    Uses the RFC 8693 token exchange flow:
    POST /oauth/token with grant_type=urn:ietf:params:oauth:grant-type:token-exchange
    """

    def __init__(self, forge_url: str, token_exchange_url: str = "") -> None:
        _validate_forge_url(forge_url)
        self._forge_url = forge_url
        self._token_exchange_url = token_exchange_url or f"{forge_url}/oauth/token"

    async def exchange(
        self,
        oidc_token: str,
        repos: list[str] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> ForgeCredential:
        if repos:
            _validate_repos(repos)

        body = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": oidc_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        }

        async with _build_httpx_client(self._forge_url) as client:
            resp = await _request_with_retry(
                client,
                "POST",
                self._token_exchange_url,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()

        token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        expires_at = time.time() + expires_in

        cred = ForgeCredential(
            token=token,
            expires_at=expires_at,
            scopes=data.get("scope", "").split() if data.get("scope") else [],
            forge_type="gitlab",
        )
        logger.info(
            "GitLab token exchanged: %s, expires_in=%ds",
            cred.fingerprint, expires_in,
        )
        return cred


class ForgejoTokenExchanger(ForgeTokenExchanger):
    """Exchange OIDC tokens for Forgejo access tokens.

    Forgejo mirrors the GitHub App installation token API, so the flow
    is identical to GitHubTokenExchanger.
    """

    def __init__(
        self,
        forge_url: str,
        app_id: str,
        installation_id: str,
        private_key_path: str,
    ) -> None:
        _validate_forge_url(forge_url)
        self._inner = GitHubTokenExchanger(
            forge_url=forge_url,
            app_id=app_id,
            installation_id=installation_id,
            private_key_path=private_key_path,
        )
        # Forgejo API is at the same base URL (not a separate api. subdomain)
        self._inner._api_url = forge_url

    async def exchange(
        self,
        oidc_token: str,
        repos: list[str] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> ForgeCredential:
        cred = await self._inner.exchange(oidc_token, repos, permissions)
        # Re-tag as forgejo
        cred.forge_type = "forgejo"
        return cred


def resolve_forge_exchanger(
    *,
    forge_type: str,
    forge_url: str,
    github_app_id: str = "",
    github_app_installation_id: str = "",
    github_app_private_key_path: str = "",
    gitlab_token_exchange_url: str = "",
) -> ForgeTokenExchanger | None:
    """Create the appropriate forge exchanger based on configuration."""
    if not forge_type:
        return None

    if forge_type == "github":
        if not all([github_app_id, github_app_installation_id, github_app_private_key_path]):
            raise ValueError(
                "GitHub forge requires github_app_id, "
                "github_app_installation_id, and github_app_private_key_path"
            )
        return GitHubTokenExchanger(
            forge_url=forge_url or "https://github.com",
            app_id=github_app_id,
            installation_id=github_app_installation_id,
            private_key_path=github_app_private_key_path,
        )

    if forge_type == "gitlab":
        return GitLabTokenExchanger(
            forge_url=forge_url or "https://gitlab.com",
            token_exchange_url=gitlab_token_exchange_url,
        )

    if forge_type == "forgejo":
        if not all([github_app_id, github_app_installation_id, github_app_private_key_path]):
            raise ValueError(
                "Forgejo forge requires github_app_id, "
                "github_app_installation_id, and github_app_private_key_path"
            )
        if not forge_url:
            raise ValueError("Forgejo forge requires an explicit forge_url")
        return ForgejoTokenExchanger(
            forge_url=forge_url,
            app_id=github_app_id,
            installation_id=github_app_installation_id,
            private_key_path=github_app_private_key_path,
        )

    raise ValueError(f"Unknown forge type: {forge_type!r}")
