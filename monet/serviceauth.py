"""
monet/serviceauth.py
~~~~~~~~~~~~~~~~~~~~~

monet's binding to the **shared** service-auth helper (WP-3b / ADR-001, C18).

monet deliberately does **not** reimplement authentication. It imports the one
audited helper that lives in picasso-registry (the ``picasso-registry[auth]``
extra) so both FastAPI services in the DNA-PAINT stack share a single
implementation instead of two drifting copies. This module only pins monet's own
token-store env var so the two services never share tokens:

* picasso-registry reads ``PAINT_REGISTRY_TOKENS`` (its ``DEFAULT_TOKENS_ENV``);
* monet reads ``PAINT_MONET_TOKENS`` (:data:`MONET_TOKENS_ENV`).

Each variable holds a comma/semicolon/newline-separated ``token:scope:label``
map, where ``scope`` is ``read`` or ``write`` and ``label`` is the human-readable
holder (a machine role like ``microscope-mercury``, or the ``cluster``). A
``write`` token also satisfies ``read`` (capability separation, not per-user
RBAC). See ``picasso-registry/docs/adr/001-service-authentication.md``.

Why this matters here more than in the registry: a monet ``write`` **actuates
laser hardware** (``POST /power/set``), so an unauthenticated networked bind is a
*safety* issue, not just data pollution — hence the fail-closed host guard in
:mod:`monet.__main__` and the request-time net inside :func:`require_scope`.
"""

from __future__ import annotations

try:
    # Home: the picasso-registry [auth] extra (a single fastapi dependency).
    from picasso_registry.auth import (  # noqa: F401
        AuthConfig,
        TokenInfo,
        is_loopback_host,
        is_remote_client,
        parse_tokens,
        require_scope,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - install-time guard
    raise ModuleNotFoundError(
        "monet's serve API needs the shared auth helper. Install the server "
        "extra, which pulls picasso-registry[auth]:  pip install "
        "'monet[server]'  (see README 'Authentication')."
    ) from exc

# monet's own token-store env var (kept distinct from the registry's so the two
# services never share a token store — see the module docstring).
MONET_TOKENS_ENV = "PAINT_MONET_TOKENS"


def auth_from_env(var: str = MONET_TOKENS_ENV) -> AuthConfig:
    """Build monet's :class:`AuthConfig` from ``PAINT_MONET_TOKENS``.

    An unset/empty variable yields a disabled (unauthenticated) config — the
    zero-config loopback dev path and the in-memory test client. The fail-closed
    host guard is what keeps that state off a networked bind.
    """
    return AuthConfig.from_env(var)
