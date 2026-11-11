"""monet admin tooling (the ``[admin]`` extra).

An SSH-driven admin console for the deployed DB-only monet calibration server:
it runs the server-side ``monet-admin`` wrapper over SSH to manage tokens,
check status, tail logs, restart, and upgrade — so monet keeps **no network
admin surface** (authority is your SSH access, per ADR-001 / C18).

It is meant to run from an admin workstation (a laptop), not the rigs or the
server box: ``pip install 'monet[admin]'`` (PyQt6 + paramiko) is the only
install needed there — never ``monet[server]`` / ``monet[hardware]``. It sits
beside the picasso-registry admin console, which targets the same central host.

Layers, mirroring monet's remote-setup wizard:

* :mod:`monet.admin.core` — GUI-free SSH transport + operations (paramiko,
  lazy-imported). The one place that knows how to drive the box.
* :mod:`monet.admin.cli` — headless front-end (``monet-admin-cli``).
* :mod:`monet.admin.qt` — PyQt6 GUI (``monet-admin-gui``).

Both front-ends are thin renderers over the core. Qt and paramiko live in the
``[admin]`` extra; the base package stays free of them.
"""

from __future__ import annotations

from .core import (
    AdminError,
    MonetAdmin,
    ServerStatus,
    TokenEntry,
    parse_minted_token,
    parse_status,
    parse_token_list,
)

__all__ = [
    "MonetAdmin",
    "AdminError",
    "ServerStatus",
    "TokenEntry",
    "parse_status",
    "parse_token_list",
    "parse_minted_token",
]
