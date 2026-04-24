"""Secretless identity federation for Git forge access.

Provides OIDC token sourcing (via SPIFFE/SPIRE) and exchange with
Git forges (GitHub, GitLab, Forgejo) for short-lived access tokens.
"""

from trellis.core.identity.forge import (
    ForgeCredential,
    ForgeTokenExchanger,
    ForgejoTokenExchanger,
    GitHubTokenExchanger,
    GitLabTokenExchanger,
    resolve_forge_exchanger,
)
from trellis.core.identity.manager import ForgeTokenManager, create_token_manager
from trellis.core.identity.provider import (
    IdentityProvider,
    SpiffeIdentityProvider,
    resolve_identity_provider,
)

__all__ = [
    "ForgeCredential",
    "ForgeTokenExchanger",
    "ForgeTokenManager",
    "ForgejoTokenExchanger",
    "GitHubTokenExchanger",
    "GitLabTokenExchanger",
    "IdentityProvider",
    "SpiffeIdentityProvider",
    "create_token_manager",
    "resolve_forge_exchanger",
    "resolve_identity_provider",
]
