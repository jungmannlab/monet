"""GUI-free SSH core for the monet admin console.

Drives the server-side ``monet-admin`` wrapper over SSH. The design mirrors
monet's own ``setup_core`` (and picasso-registry's admin core): paramiko is
lazy-imported (so importing this module is cheap and needs no admin extra), the
run primitive returns combined output or *raises* on a non-zero exit, and a
single ``on_output`` callback is the only live-streaming seam (the CLI prints
it, the GUI emits it as a Qt signal).

Nothing here imports Qt, the monet server, or any hardware SDK — so the laptop
that administers the central host needs only ``pip install 'monet[admin]'``
(PyQt6 + paramiko), never ``monet[server]`` / ``monet[hardware]``. Parsing
helpers are module-level and pure, so they are unit-testable without a server.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Callable

#: Name of the server-side wrapper on the box's PATH (installed by
#: deploy/install-docker.sh into /usr/local/bin).
REMOTE_WRAPPER = "monet-admin"
DEFAULT_SSH_PORT = 22

OnOutput = Callable[[str], None]


class AdminError(RuntimeError):
    """A remote command failed, or the connection could not be made."""


@dataclass
class TokenEntry:
    """One ``(scope, label)`` row from ``token list`` (never the value)."""

    scope: str
    label: str


@dataclass
class ServerStatus:
    """Parsed ``monet-admin status`` output."""

    running: bool
    health_ok: bool
    version: str | None = None
    image: str | None = None
    ports: str | None = None
    raw: dict[str, str] = field(default_factory=dict)


# --- parsing helpers (pure; unit-testable without SSH) -----------------------


def parse_token_list(text: str) -> list[TokenEntry]:
    """Parse the ``SCOPE  LABEL`` table printed by ``monet token list``.

    Skips the header and the "no tokens configured" line; each remaining row is
    ``<scope> <whitespace> <label>``. Unparseable lines are ignored.
    """
    entries: list[TokenEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("SCOPE"):
            continue
        if line.lower().startswith("no tokens"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        scope, label = parts[0], parts[1].strip()
        if scope in ("read", "write"):
            entries.append(TokenEntry(scope=scope, label=label))
    return entries


#: ``monet token add``/``rotate`` print the value once as a
#: ``PAINT_MONET_TOKEN=<value>`` line (the client .env line to copy) — pull it
#: back out for a copy button. The value is a ``secrets.token_urlsafe`` string,
#: whose alphabet is exactly ``[A-Za-z0-9_-]``; matching only those chars stops
#: the capture at the end of the token instead of swallowing a trailing period
#: if the surrounding message is ever reworded.
_MINTED_RE = re.compile(r"PAINT_MONET_TOKEN=(?P<value>[A-Za-z0-9_\-]+)")


def parse_minted_token(text: str) -> str | None:
    """Return the freshly minted token value from add/rotate output, if found."""
    m = _MINTED_RE.search(text)
    return m.group("value") if m else None


def parse_status(text: str) -> ServerStatus:
    """Parse the ``key: value`` lines printed by ``status``."""
    raw: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            raw[key.strip().lower()] = val.strip()
    state = raw.get("state", "").lower()
    # `state` is authoritative when present; only fall back to the `status`
    # line (`Up …`) when the wrapper didn't report a state. Avoids a stale
    # `Up … (unhealthy)` line reading as running while state says exited.
    if state:
        running = state == "running"
    else:
        running = raw.get("status", "").startswith("Up")
    health_ok = raw.get("health", "").upper().startswith("OK")
    return ServerStatus(
        running=running,
        health_ok=health_ok,
        version=raw.get("version") or None,
        image=raw.get("image") or None,
        ports=raw.get("ports") or None,
        raw=raw,
    )


# --- SSH transport -----------------------------------------------------------


def _connect(
    host: str,
    user: str,
    *,
    password: str | None,
    key_filename: str | None,
    port: int,
    sudo_password: str | None,
    on_output: OnOutput | None,
):
    """Open one paramiko connection; return ``(client, run)``.

    ``run(cmd, sudo=False) -> str`` returns combined stdout+stderr and raises
    :class:`AdminError` on a non-zero exit. The caller owns ``client`` and must
    ``close()`` it (always in a ``finally``). Mirrors monet's setup primitive.
    """
    try:
        import paramiko
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise AdminError(
            "SSH needs paramiko — install the admin stack: "
            "pip install 'monet[admin]'"
        ) from exc

    client = paramiko.SSHClient()
    # Trust-on-first-use: an interactive admin tool, not an unattended service.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict = dict(
        hostname=host,
        port=port,
        username=user,
        password=password,
        key_filename=key_filename,
        # Bound the handshake so an unreachable/filtered host fails fast
        # instead of hanging the CLI/GUI worker indefinitely.
        timeout=15,
        banner_timeout=15,
    )
    # With a typed password and no key, stop paramiko trying agent/default keys
    # first and exhausting MaxAuthTries before it ever tries the password.
    if password and not key_filename:
        connect_kwargs["look_for_keys"] = False
        connect_kwargs["allow_agent"] = False
    try:
        client.connect(**connect_kwargs)
    except Exception as exc:  # paramiko raises many types; normalize
        client.close()  # don't leak the half-open transport/socket
        raise AdminError(f"SSH connection to {user}@{host} failed: {exc}")

    def run(cmd: str, sudo: bool = False) -> str:
        if sudo:
            cmd = "sudo -S -p '' " + cmd
        if on_output:
            on_output(f"$ {cmd}\n")
        chan = client.get_transport().open_session()
        chan.set_combine_stderr(True)
        chan.exec_command(cmd)
        if sudo:
            # Always close stdin for `sudo -S`: with a password, send it first;
            # WITHOUT one, the EOF makes sudo fail fast ("no password") instead
            # of blocking forever on a prompt nobody will answer.
            if sudo_password:
                chan.sendall((sudo_password + "\n").encode())
            chan.shutdown_write()
        chunks: list[str] = []
        while True:
            data = chan.recv(4096)
            if not data:
                break
            text = data.decode("utf-8", "replace")
            chunks.append(text)
            if on_output:
                on_output(text)
        code = chan.recv_exit_status()
        chan.close()
        out = "".join(chunks)
        if code != 0:
            raise AdminError(
                f"remote command failed ({code}): {cmd}\n{out}".rstrip()
            )
        return out

    return client, run


class MonetAdmin:
    """A connection to a deployed monet server's admin surface.

    Connects per use (``with MonetAdmin(...) as a: a.token_list()``) so no
    paramiko client is shared across threads — the GUI runs each operation in
    its own worker. ``on_output`` streams every command + chunk live.
    """

    def __init__(
        self,
        host: str,
        user: str,
        *,
        password: str | None = None,
        key_filename: str | None = None,
        port: int = DEFAULT_SSH_PORT,
        use_sudo: bool = False,
        sudo_password: str | None = None,
        wrapper: str = REMOTE_WRAPPER,
        on_output: OnOutput | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self._password = password
        self._key_filename = key_filename
        self._port = port
        self._use_sudo = use_sudo
        # Default the sudo password to the login password (monet convention).
        self._sudo_password = sudo_password or password
        self._wrapper = wrapper
        self._on_output = on_output
        self._client = None
        self._run = None

    # -- connection lifecycle --------------------------------------------
    def connect(self) -> "MonetAdmin":
        self._client, self._run = _connect(
            self.host,
            self.user,
            password=self._password,
            key_filename=self._key_filename,
            port=self._port,
            sudo_password=self._sudo_password,
            on_output=self._on_output,
        )
        return self

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                # Closing a half-dead transport can raise; never let that mask
                # the real error from the `with` body.
                pass
            self._client = None
            self._run = None

    def __enter__(self) -> "MonetAdmin":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the one command builder -----------------------------------------
    def _admin(self, *args: str, sudo: bool | None = None) -> str:
        if self._run is None:
            raise AdminError("not connected — call connect() first")
        use_sudo = self._use_sudo if sudo is None else sudo
        cmd = " ".join(shlex.quote(a) for a in (self._wrapper, *args))
        return self._run(cmd, sudo=use_sudo)

    # -- operations (thin wrappers over the server-side wrapper) ---------
    def status(self) -> ServerStatus:
        return parse_status(self._admin("status"))

    def token_list(self) -> list[TokenEntry]:
        return parse_token_list(self._admin("token", "list"))

    def token_add(self, scope: str, label: str) -> str:
        """Mint a token; returns the raw output (holds the one-time value)."""
        return self._admin("token", "add", "--scope", scope, "--label", label)

    def token_rotate(self, label: str) -> str:
        return self._admin("token", "rotate", "--label", label)

    def token_revoke(self, label: str) -> str:
        return self._admin("token", "revoke", "--label", label)

    def logs(self, lines: int = 200, follow: bool = False) -> str:
        args = ["logs", "-n", str(lines)]
        if follow:
            args.append("-f")
        return self._admin(*args)

    def restart(self) -> str:
        return self._admin("restart")

    def upgrade(self, image_ref: str) -> str:
        """Pull a new image + recreate the container (needs root on the box)."""
        return self._admin("upgrade", image_ref, sudo=True)
