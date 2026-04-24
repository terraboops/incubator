"""Token lifecycle manager — orchestrates identity providers and forge exchangers.

Combines an IdentityProvider (SPIFFE) with a ForgeTokenExchanger (GitHub/GitLab/
Forgejo) to manage the full OIDC-to-forge token lifecycle including caching,
proactive refresh, and concurrent access safety.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from trellis.core.identity.forge import (
    ForgeCredential,
    ForgeTokenExchanger,
    _validate_branch_pattern,
)
from trellis.core.identity.provider import IdentityProvider

logger = logging.getLogger(__name__)

# Refresh when 80% of TTL has elapsed
_REFRESH_THRESHOLD = 0.80

# Minimum seconds of validity to consider a cached token usable
_MIN_VALIDITY_SECONDS = 30.0


@dataclass
class _CacheEntry:
    credential: ForgeCredential
    fetched_at: float


class ForgeTokenManager:
    """Manages forge token acquisition, caching, and refresh.

    Thread-safe via asyncio.Lock — multiple concurrent agents can share
    a single manager without duplicating token exchange requests.
    """

    def __init__(
        self,
        provider: IdentityProvider,
        exchanger: ForgeTokenExchanger,
        *,
        audience: str = "",
        skew_seconds: float = 30.0,
    ) -> None:
        self._provider = provider
        self._exchanger = exchanger
        self._audience = audience
        self._skew_seconds = skew_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    def _cache_key(self, repos: list[str] | None, permissions: dict[str, str] | None) -> str:
        """Build a deterministic cache key from request parameters."""
        repo_part = ",".join(sorted(repos or []))
        perm_part = ",".join(f"{k}={v}" for k, v in sorted((permissions or {}).items()))
        return f"{repo_part}|{perm_part}"

    def _is_usable(self, entry: _CacheEntry) -> bool:
        """Check if a cached credential is still valid (with skew buffer)."""
        remaining = entry.credential.expires_at - time.time()
        return remaining > self._skew_seconds

    def _should_refresh(self, entry: _CacheEntry) -> bool:
        """Check if a cached credential should be proactively refreshed."""
        total_ttl = entry.credential.expires_at - entry.fetched_at
        if total_ttl <= 0:
            return True
        elapsed = time.time() - entry.fetched_at
        return (elapsed / total_ttl) >= _REFRESH_THRESHOLD

    async def get_forge_token(
        self,
        repos: list[str] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> ForgeCredential | None:
        """Obtain a forge credential, using cache when possible.

        Returns None if the identity provider or forge exchanger fails.
        Logs errors but does not raise — callers should handle None
        gracefully (agent continues without forge access).
        """
        key = self._cache_key(repos, permissions)

        # Fast path: check cache without lock
        entry = self._cache.get(key)
        if entry and self._is_usable(entry) and not self._should_refresh(entry):
            return entry.credential

        # Slow path: acquire lock and refresh
        async with self._lock:
            # Double-check after acquiring lock
            entry = self._cache.get(key)
            if entry and self._is_usable(entry) and not self._should_refresh(entry):
                return entry.credential

            try:
                cred = await self._exchange(repos, permissions)
                self._cache[key] = _CacheEntry(
                    credential=cred,
                    fetched_at=time.time(),
                )
                return cred
            except Exception:
                logger.exception("Failed to obtain forge token")
                # Return stale-but-valid cached token if available
                if entry and self._is_usable(entry):
                    logger.warning(
                        "Using stale cached token (fingerprint=%s)",
                        entry.credential.fingerprint,
                    )
                    return entry.credential
                return None

    async def _exchange(
        self,
        repos: list[str] | None,
        permissions: dict[str, str] | None,
    ) -> ForgeCredential:
        """Execute the full OIDC → forge exchange flow."""
        logger.info(
            "Exchanging OIDC token via %s for forge access (repos=%s)",
            self._provider.provider_name(),
            repos,
        )
        oidc_token = await self._provider.get_token(self._audience)
        cred = await self._exchanger.exchange(oidc_token, repos, permissions)
        logger.info(
            "Forge token obtained: fingerprint=%s, expires_in=%.0fs",
            cred.fingerprint,
            cred.expires_at - time.time(),
        )
        return cred

    def invalidate(self) -> None:
        """Clear all cached tokens (e.g. on permission change)."""
        self._cache.clear()


async def create_token_manager(
    *,
    provider: IdentityProvider | None,
    exchanger: ForgeTokenExchanger | None,
    audience: str = "",
    skew_seconds: float = 30.0,
) -> ForgeTokenManager | None:
    """Create a ForgeTokenManager if both provider and exchanger are available."""
    if provider is None or exchanger is None:
        return None
    return ForgeTokenManager(
        provider=provider,
        exchanger=exchanger,
        audience=audience,
        skew_seconds=skew_seconds,
    )
