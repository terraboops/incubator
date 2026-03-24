"""Identity providers for OIDC token sourcing.

Provides an abstract IdentityProvider interface and a concrete SPIFFE/SPIRE
implementation that fetches JWT-SVIDs from the SPIRE Agent Workload API.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class IdentityProvider(ABC):
    """Abstract base for OIDC identity token sources."""

    @abstractmethod
    async def get_token(self, audience: str) -> str:
        """Return a JWT suitable for OIDC federation with the given audience."""

    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable name for logging and diagnostics."""


def _parse_jwt_exp(token: str) -> float | None:
    """Extract the ``exp`` claim from a JWT without verifying the signature.

    We only need to know when the token expires so we can refresh proactively.
    The relying party (Git forge) is responsible for full verification.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        # JWT base64url padding
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return float(claims["exp"])
    except Exception:
        return None


@dataclass
class _CachedToken:
    token: str
    exp: float  # Unix timestamp


class SpiffeIdentityProvider(IdentityProvider):
    """Fetches JWT-SVIDs from the SPIRE Agent Workload API.

    Connects via Unix domain socket to the local SPIRE agent and requests
    a JWT-SVID for the given audience.  Tokens are cached until they approach
    expiry (controlled by ``skew_seconds``).
    """

    def __init__(
        self,
        socket_path: str = "/tmp/spire-agent/public/api.sock",
        trust_domain: str = "trellis.local",
        spiffe_id_prefix: str | None = None,
        skew_seconds: float = 30.0,
    ) -> None:
        self._socket_path = socket_path
        self._trust_domain = trust_domain
        self._spiffe_id_prefix = spiffe_id_prefix
        self._skew_seconds = skew_seconds
        self._cache: dict[str, _CachedToken] = {}
        self._validate_socket_path(socket_path)
        self._validate_trust_domain(trust_domain)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_socket_path(path: str) -> None:
        """Reject paths that clearly are not Unix domain sockets."""
        if not path:
            raise ValueError("SPIFFE endpoint socket path must not be empty")
        # We only validate at construction time if the file exists.
        # At runtime the socket may appear later (SPIRE agent restart).
        if os.path.exists(path):
            mode = os.stat(path).st_mode
            if not stat.S_ISSOCK(mode):
                raise ValueError(
                    f"SPIFFE endpoint path is not a Unix domain socket: {path}"
                )

    @staticmethod
    def _validate_trust_domain(domain: str) -> None:
        if not domain:
            raise ValueError("SPIFFE trust domain must not be empty")
        if "/" in domain or "\\" in domain:
            raise ValueError(
                f"SPIFFE trust domain must not contain path separators: {domain!r}"
            )
        if "*" in domain or "?" in domain:
            raise ValueError(
                f"SPIFFE trust domain must not contain wildcards: {domain!r}"
            )

    # ------------------------------------------------------------------
    # IdentityProvider interface
    # ------------------------------------------------------------------

    def provider_name(self) -> str:
        return f"spiffe/{self._trust_domain}"

    async def get_token(self, audience: str) -> str:
        """Return a cached or freshly-fetched JWT-SVID for *audience*."""
        cached = self._cache.get(audience)
        now = time.time()
        if cached and (cached.exp - self._skew_seconds) > now:
            return cached.token

        token = await self._fetch_jwt_svid(audience)
        exp = _parse_jwt_exp(token)
        if exp is not None:
            self._cache[audience] = _CachedToken(token=token, exp=exp)
        return token

    # ------------------------------------------------------------------
    # SPIRE Workload API
    # ------------------------------------------------------------------

    async def _fetch_jwt_svid(self, audience: str) -> str:
        """Call the SPIRE Agent Workload API to mint a JWT-SVID.

        Uses pyspiffe if available; otherwise falls back to a minimal
        gRPC-free HTTP implementation against the Workload API.
        """
        try:
            return await self._fetch_via_pyspiffe(audience)
        except ImportError:
            logger.debug("pyspiffe not installed, using direct Workload API call")
            return await self._fetch_via_http(audience)

    async def _fetch_via_pyspiffe(self, audience: str) -> str:
        """Use the pyspiffe library for Workload API access."""
        from pyspiffe.spiffe_id.spiffe_id import SpiffeId
        from pyspiffe.workloadapi.default_jwt_source import DefaultJwtSource
        from pyspiffe.workloadapi.workload_api_client import WorkloadApiClient

        import anyio

        def _sync_fetch() -> str:
            with WorkloadApiClient(
                spiffe_endpoint_socket=f"unix://{self._socket_path}"
            ) as client:
                svid_set = client.fetch_jwt_svids(
                    audiences=[audience],
                    hint=self._spiffe_id_prefix or "",
                )
                if not svid_set:
                    raise RuntimeError("SPIRE returned no JWT-SVIDs")
                return svid_set[0].token

        return await anyio.to_thread.run_sync(_sync_fetch)

    async def _fetch_via_http(self, audience: str) -> str:
        """Minimal direct call to the SPIRE Workload API over Unix socket.

        The SPIRE Workload API exposes an HTTP endpoint on a Unix domain
        socket.  We issue a POST to /v1/auth/jwt-svids with the audience
        list.  This avoids requiring pyspiffe/gRPC as a hard dependency.
        """
        import httpx

        transport = httpx.AsyncHTTPTransport(uds=self._socket_path)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        ) as client:
            resp = await client.post(
                "/v1/auth/jwt-svids",
                json={"audience": [audience]},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            svids = data.get("svids", [])
            if not svids:
                raise RuntimeError("SPIRE returned no JWT-SVIDs")
            return svids[0]["svid"]


def resolve_identity_provider(
    *,
    provider_type: str = "auto",
    spiffe_endpoint_socket: str = "/tmp/spire-agent/public/api.sock",
    spiffe_trust_domain: str = "trellis.local",
    spiffe_id_prefix: str | None = None,
    skew_seconds: float = 30.0,
) -> IdentityProvider | None:
    """Resolve the identity provider based on configuration and environment.

    Returns None if no provider is available or configured.
    """
    if provider_type == "none":
        return None

    if provider_type in ("auto", "spiffe"):
        if os.path.exists(spiffe_endpoint_socket):
            try:
                return SpiffeIdentityProvider(
                    socket_path=spiffe_endpoint_socket,
                    trust_domain=spiffe_trust_domain,
                    spiffe_id_prefix=spiffe_id_prefix,
                    skew_seconds=skew_seconds,
                )
            except ValueError as e:
                logger.warning("SPIFFE provider unavailable: %s", e)
                if provider_type == "spiffe":
                    raise
                return None
        elif provider_type == "spiffe":
            raise FileNotFoundError(
                f"SPIFFE endpoint socket not found: {spiffe_endpoint_socket}"
            )

    if provider_type not in ("auto", "none"):
        raise ValueError(f"Unknown identity provider type: {provider_type!r}")

    logger.info("No identity provider available (auto-detect found nothing)")
    return None
