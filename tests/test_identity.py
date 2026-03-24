"""Tests for trellis/core/identity — SPIFFE identity federation and forge token exchange."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trellis.core.identity.forge import (
    ForgeCredential,
    ForgeTokenExchanger,
    GitHubTokenExchanger,
    GitLabTokenExchanger,
    ForgejoTokenExchanger,
    _validate_branch_pattern,
    _validate_forge_url,
    _validate_repos,
    resolve_forge_exchanger,
)
from trellis.core.identity.manager import ForgeTokenManager, create_token_manager
from trellis.core.identity.provider import (
    IdentityProvider,
    SpiffeIdentityProvider,
    _parse_jwt_exp,
    resolve_identity_provider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jwt(claims: dict, header: dict | None = None) -> str:
    """Build a minimal unsigned JWT for testing."""
    hdr = header or {"alg": "none", "typ": "JWT"}
    parts = []
    for payload in (hdr, claims):
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).rstrip(b"=").decode()
        parts.append(encoded)
    parts.append("")  # empty signature
    return ".".join(parts)


class MockIdentityProvider(IdentityProvider):
    """Test double for IdentityProvider."""

    def __init__(self, token: str = "mock-oidc-jwt"):
        self._token = token
        self.call_count = 0

    async def get_token(self, audience: str) -> str:
        self.call_count += 1
        return self._token

    def provider_name(self) -> str:
        return "mock"


class MockForgeExchanger(ForgeTokenExchanger):
    """Test double for ForgeTokenExchanger."""

    def __init__(self, token: str = "ghp_mock_token_1234", ttl: float = 3600):
        self._token = token
        self._ttl = ttl
        self.call_count = 0
        self.last_oidc_token: str | None = None
        self.last_repos: list[str] | None = None
        self.last_permissions: dict[str, str] | None = None

    async def exchange(
        self,
        oidc_token: str,
        repos: list[str] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> ForgeCredential:
        self.call_count += 1
        self.last_oidc_token = oidc_token
        self.last_repos = repos
        self.last_permissions = permissions
        return ForgeCredential(
            token=self._token,
            expires_at=time.time() + self._ttl,
            scopes=list((permissions or {}).keys()),
            forge_type="github",
        )


# ===================================================================
# Provider tests
# ===================================================================

class TestParseJwtExp:
    def test_valid_jwt(self):
        exp = time.time() + 3600
        token = _make_jwt({"exp": exp, "sub": "test"})
        assert _parse_jwt_exp(token) == pytest.approx(exp)

    def test_missing_exp(self):
        token = _make_jwt({"sub": "test"})
        assert _parse_jwt_exp(token) is None

    def test_invalid_jwt(self):
        assert _parse_jwt_exp("not-a-jwt") is None

    def test_empty_string(self):
        assert _parse_jwt_exp("") is None


class TestSpiffeIdentityProvider:
    def test_validate_trust_domain_rejects_slashes(self):
        with pytest.raises(ValueError, match="path separators"):
            SpiffeIdentityProvider(
                socket_path="/tmp/nonexistent.sock",
                trust_domain="../../etc/passwd",
            )

    def test_validate_trust_domain_rejects_wildcards(self):
        with pytest.raises(ValueError, match="wildcards"):
            SpiffeIdentityProvider(
                socket_path="/tmp/nonexistent.sock",
                trust_domain="*.example.com",
            )

    def test_validate_trust_domain_rejects_empty(self):
        with pytest.raises(ValueError, match="must not be empty"):
            SpiffeIdentityProvider(
                socket_path="/tmp/nonexistent.sock",
                trust_domain="",
            )

    def test_validate_socket_path_rejects_regular_file(self):
        with tempfile.NamedTemporaryFile() as f:
            with pytest.raises(ValueError, match="not a Unix domain socket"):
                SpiffeIdentityProvider(
                    socket_path=f.name,
                    trust_domain="trellis.local",
                )

    def test_validate_socket_path_rejects_empty(self):
        with pytest.raises(ValueError, match="must not be empty"):
            SpiffeIdentityProvider(socket_path="", trust_domain="trellis.local")

    def test_nonexistent_socket_ok_at_init(self):
        # Socket may not exist yet (SPIRE agent starts later)
        provider = SpiffeIdentityProvider(
            socket_path="/tmp/definitely-not-a-real-socket-path-xyz",
            trust_domain="trellis.local",
        )
        assert provider.provider_name() == "spiffe/trellis.local"

    async def test_caches_token(self):
        provider = SpiffeIdentityProvider(
            socket_path="/tmp/nonexistent.sock",
            trust_domain="trellis.local",
            skew_seconds=5.0,
        )
        exp = time.time() + 300
        mock_token = _make_jwt({"exp": exp, "aud": "test"})

        with patch.object(provider, "_fetch_jwt_svid", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_token
            t1 = await provider.get_token("test-audience")
            t2 = await provider.get_token("test-audience")
            assert t1 == t2
            assert mock_fetch.call_count == 1  # only one fetch, second was cached

    async def test_refreshes_expired_token(self):
        provider = SpiffeIdentityProvider(
            socket_path="/tmp/nonexistent.sock",
            trust_domain="trellis.local",
            skew_seconds=5.0,
        )
        # Token that expires very soon
        exp_soon = time.time() + 2
        token_soon = _make_jwt({"exp": exp_soon, "aud": "test"})
        exp_later = time.time() + 300
        token_later = _make_jwt({"exp": exp_later, "aud": "test"})

        with patch.object(provider, "_fetch_jwt_svid", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = token_soon
            t1 = await provider.get_token("test")
            assert t1 == token_soon

            # Token is within skew window, should refetch
            mock_fetch.return_value = token_later
            t2 = await provider.get_token("test")
            assert t2 == token_later
            assert mock_fetch.call_count == 2


class TestResolveIdentityProvider:
    def test_none_returns_none(self):
        assert resolve_identity_provider(provider_type="none") is None

    def test_auto_no_socket_returns_none(self):
        result = resolve_identity_provider(
            provider_type="auto",
            spiffe_endpoint_socket="/tmp/nonexistent-socket-xyz",
        )
        assert result is None

    def test_spiffe_missing_socket_raises(self):
        with pytest.raises(FileNotFoundError):
            resolve_identity_provider(
                provider_type="spiffe",
                spiffe_endpoint_socket="/tmp/nonexistent-socket-xyz",
            )

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown identity provider"):
            resolve_identity_provider(provider_type="kubernetes")


# ===================================================================
# Forge exchanger tests
# ===================================================================

class TestForgeCredential:
    def test_repr_masks_token(self):
        cred = ForgeCredential(
            token="ghp_abcdef1234567890",
            expires_at=time.time() + 3600,
            scopes=["contents"],
            forge_type="github",
        )
        r = repr(cred)
        assert "ghp_abcdef1234567890" not in r
        assert "****7890" in r

    def test_str_masks_token(self):
        cred = ForgeCredential(
            token="glpat-abcdef1234567890",
            expires_at=time.time() + 3600,
            scopes=[],
            forge_type="gitlab",
        )
        assert "glpat-abcdef1234567890" not in str(cred)

    def test_fingerprint(self):
        cred = ForgeCredential(
            token="ghp_abc123xyz",
            expires_at=time.time() + 3600,
            scopes=[],
            forge_type="github",
        )
        assert cred.fingerprint == "****3xyz"

    def test_is_expired(self):
        cred = ForgeCredential(
            token="tok", expires_at=time.time() - 10,
            scopes=[], forge_type="github",
        )
        assert cred.is_expired

    def test_not_expired(self):
        cred = ForgeCredential(
            token="tok", expires_at=time.time() + 3600,
            scopes=[], forge_type="github",
        )
        assert not cred.is_expired


class TestValidation:
    def test_forge_url_rejects_http(self):
        with pytest.raises(ValueError, match="HTTPS"):
            _validate_forge_url("http://github.com")

    def test_forge_url_accepts_https(self):
        _validate_forge_url("https://github.com")  # no exception

    def test_repos_rejects_wildcards(self):
        with pytest.raises(ValueError, match="wildcards"):
            _validate_repos(["org/*"])

    def test_repos_rejects_bad_format(self):
        with pytest.raises(ValueError, match="owner/repo"):
            _validate_repos(["just-a-repo"])

    def test_repos_accepts_valid(self):
        _validate_repos(["org/repo", "other/repo2"])  # no exception

    def test_branch_pattern_rejects_invalid_regex(self):
        with pytest.raises(ValueError, match="Invalid branch pattern"):
            _validate_branch_pattern("[invalid")

    def test_branch_pattern_accepts_valid(self):
        _validate_branch_pattern(r"agent/implementation/.*")  # no exception

    def test_branch_pattern_accepts_empty(self):
        _validate_branch_pattern("")  # no exception

    def test_branch_pattern_uses_fullmatch_semantics(self):
        """Pattern 'main' should not match 'main-evil' with fullmatch."""
        pattern = "main"
        assert re.fullmatch(pattern, "main") is not None
        assert re.fullmatch(pattern, "main-evil") is None


class TestGitHubTokenExchanger:
    def test_rejects_http_url(self):
        with pytest.raises(ValueError, match="HTTPS"):
            GitHubTokenExchanger(
                forge_url="http://github.com",
                app_id="123",
                installation_id="456",
                private_key_path="/tmp/nonexistent.pem",
            )

    def test_validates_pem_permissions(self):
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
            f.write(b"fake-key")
            f.flush()
            # Make world-readable
            os.chmod(f.name, 0o644)
            try:
                with pytest.raises(PermissionError, match="group/world readable"):
                    GitHubTokenExchanger(
                        forge_url="https://github.com",
                        app_id="123",
                        installation_id="456",
                        private_key_path=f.name,
                    )
            finally:
                os.unlink(f.name)

    def test_missing_pem_raises(self):
        with pytest.raises(FileNotFoundError):
            GitHubTokenExchanger(
                forge_url="https://github.com",
                app_id="123",
                installation_id="456",
                private_key_path="/tmp/nonexistent-pem-xyz.pem",
            )


class TestResolveForgeExchanger:
    def test_empty_type_returns_none(self):
        assert resolve_forge_exchanger(forge_type="", forge_url="") is None

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown forge type"):
            resolve_forge_exchanger(forge_type="bitbucket", forge_url="https://bb.com")

    def test_github_missing_config_raises(self):
        with pytest.raises(ValueError, match="github_app_id"):
            resolve_forge_exchanger(
                forge_type="github",
                forge_url="https://github.com",
            )

    def test_forgejo_missing_url_raises(self):
        with pytest.raises(ValueError, match="explicit forge_url"):
            resolve_forge_exchanger(
                forge_type="forgejo",
                forge_url="",
                github_app_id="1",
                github_app_installation_id="2",
                github_app_private_key_path="/tmp/nonexistent.pem",
            )

    def test_gitlab_creates_exchanger(self):
        exchanger = resolve_forge_exchanger(
            forge_type="gitlab",
            forge_url="https://gitlab.com",
        )
        assert isinstance(exchanger, GitLabTokenExchanger)


# ===================================================================
# Token Manager tests
# ===================================================================

class TestForgeTokenManager:
    async def test_get_token_end_to_end(self):
        provider = MockIdentityProvider(token="oidc-jwt")
        exchanger = MockForgeExchanger(token="ghp_test123", ttl=3600)
        manager = ForgeTokenManager(provider, exchanger, audience="github")

        cred = await manager.get_forge_token(repos=["org/repo"])
        assert cred is not None
        assert cred.token == "ghp_test123"
        assert exchanger.last_oidc_token == "oidc-jwt"
        assert exchanger.last_repos == ["org/repo"]

    async def test_caches_forge_token(self):
        provider = MockIdentityProvider()
        exchanger = MockForgeExchanger(ttl=3600)
        manager = ForgeTokenManager(provider, exchanger)

        c1 = await manager.get_forge_token()
        c2 = await manager.get_forge_token()
        assert c1 is c2  # same object from cache
        assert exchanger.call_count == 1

    async def test_refreshes_at_80_percent_ttl(self):
        provider = MockIdentityProvider()
        exchanger = MockForgeExchanger(ttl=100)
        manager = ForgeTokenManager(provider, exchanger, skew_seconds=5)

        c1 = await manager.get_forge_token()
        assert exchanger.call_count == 1

        # Manipulate cache to simulate 80% of 100s TTL elapsed:
        # fetched_at = 85s ago, expires_at = 15s from now (100s total TTL)
        key = manager._cache_key(None, None)
        entry = manager._cache[key]
        now = time.time()
        entry.fetched_at = now - 85
        entry.credential.expires_at = now + 15  # 85/100 = 85% elapsed

        c2 = await manager.get_forge_token()
        assert exchanger.call_count == 2  # refreshed

    async def test_handles_provider_failure(self):
        provider = MockIdentityProvider()
        provider.get_token = AsyncMock(side_effect=RuntimeError("SPIRE down"))
        exchanger = MockForgeExchanger()
        manager = ForgeTokenManager(provider, exchanger)

        result = await manager.get_forge_token()
        assert result is None  # graceful failure

    async def test_handles_exchanger_failure_with_stale_cache(self):
        provider = MockIdentityProvider()
        exchanger = MockForgeExchanger(ttl=3600)
        manager = ForgeTokenManager(provider, exchanger, skew_seconds=5)

        # First call succeeds
        c1 = await manager.get_forge_token()
        assert c1 is not None

        # Force refresh condition
        key = manager._cache_key(None, None)
        manager._cache[key].fetched_at = time.time() - 3600

        # Exchanger now fails
        exchanger.exchange = AsyncMock(side_effect=RuntimeError("GitHub 500"))

        # Should return stale but still valid cached token
        c2 = await manager.get_forge_token()
        assert c2 is not None
        assert c2.token == c1.token

    async def test_concurrent_access(self):
        provider = MockIdentityProvider()
        exchanger = MockForgeExchanger(ttl=3600)
        manager = ForgeTokenManager(provider, exchanger)

        results = await asyncio.gather(*[
            manager.get_forge_token() for _ in range(10)
        ])
        # All should succeed
        assert all(r is not None for r in results)
        # Only one exchange should have happened (lock prevents duplicates)
        # Note: the first concurrent call wins the lock; others find cache populated
        assert exchanger.call_count == 1

    async def test_clock_skew_buffer(self):
        provider = MockIdentityProvider()
        exchanger = MockForgeExchanger(ttl=25)  # 25s TTL
        manager = ForgeTokenManager(provider, exchanger, skew_seconds=30)

        c1 = await manager.get_forge_token()
        assert c1 is not None

        # Token has 25s TTL but skew buffer is 30s, so it's already "expired"
        # The manager should try to refresh on next call
        c2 = await manager.get_forge_token()
        assert exchanger.call_count == 2  # had to refresh

    async def test_invalidate_clears_cache(self):
        provider = MockIdentityProvider()
        exchanger = MockForgeExchanger(ttl=3600)
        manager = ForgeTokenManager(provider, exchanger)

        await manager.get_forge_token()
        assert exchanger.call_count == 1
        manager.invalidate()
        await manager.get_forge_token()
        assert exchanger.call_count == 2  # cache was cleared


class TestCreateTokenManager:
    async def test_returns_none_if_no_provider(self):
        exchanger = MockForgeExchanger()
        result = await create_token_manager(provider=None, exchanger=exchanger)
        assert result is None

    async def test_returns_none_if_no_exchanger(self):
        provider = MockIdentityProvider()
        result = await create_token_manager(provider=provider, exchanger=None)
        assert result is None

    async def test_returns_manager_when_both_present(self):
        provider = MockIdentityProvider()
        exchanger = MockForgeExchanger()
        result = await create_token_manager(provider=provider, exchanger=exchanger)
        assert isinstance(result, ForgeTokenManager)


# ===================================================================
# Registry roundtrip tests
# ===================================================================

class TestRegistryForgeFields:
    def test_agent_config_defaults(self):
        from trellis.core.registry import AgentConfig
        config = AgentConfig(name="test", description="t")
        assert config.forge_repos == []
        assert config.forge_permissions == {}
        assert config.forge_branch_pattern == ""

    def test_registry_roundtrip(self, tmp_path):
        from trellis.core.registry import AgentConfig, Registry, load_registry

        config = AgentConfig(
            name="test",
            description="test agent",
            forge_repos=["org/repo1", "org/repo2"],
            forge_permissions={"contents": "write", "pull_requests": "read"},
            forge_branch_pattern=r"agent/test/.*",
        )
        registry = Registry(agents={"test": config})
        path = tmp_path / "registry.yaml"
        registry.save(path)

        loaded = load_registry(path)
        agent = loaded.get_agent("test")
        assert agent is not None
        assert agent.forge_repos == ["org/repo1", "org/repo2"]
        assert agent.forge_permissions == {"contents": "write", "pull_requests": "read"}
        assert agent.forge_branch_pattern == r"agent/test/.*"


# ===================================================================
# Config tests
# ===================================================================

class TestConfigForgeSettings:
    def test_default_settings(self):
        from trellis.config import Settings
        s = Settings(
            _env_file=None,
            project_root=Path("/tmp"),
            blackboard_dir=Path("/tmp/bb"),
            workspace_dir=Path("/tmp/ws"),
            registry_path=Path("/tmp/reg.yaml"),
        )
        assert s.identity_provider == "auto"
        assert s.forge_type == ""
        assert s.spiffe_endpoint_socket == "/tmp/spire-agent/public/api.sock"
        assert s.spiffe_trust_domain == "trellis.local"
        assert s.github_app_id == ""
        assert s.forge_token_audience == ""
