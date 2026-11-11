"""Unit tests for the admin console's pure parsing + command building.

These cover the risk surface (parsing the server wrapper's output and building
the right remote commands) without needing paramiko, PyQt6, or an SSH server —
the SSH transport itself is a thin, separately-exercised shim.
"""

from __future__ import annotations

from monet.admin.core import (
    MonetAdmin,
    parse_minted_token,
    parse_status,
    parse_token_list,
)


def test_parse_token_list_skips_header_and_empty():
    text = "SCOPE   LABEL\nwrite   microscope-mercury\nread    lab-dashboard\n"
    entries = parse_token_list(text)
    assert [(e.scope, e.label) for e in entries] == [
        ("write", "microscope-mercury"),
        ("read", "lab-dashboard"),
    ]


def test_parse_token_list_handles_no_tokens():
    assert parse_token_list("no tokens configured in /x/monet.env") == []


def test_parse_token_list_ignores_junk_rows():
    # a stray non-scope line must not become a bogus entry
    text = "SCOPE   LABEL\nwrite   a\n(some noise line)\nread   b"
    labels = [(e.scope, e.label) for e in parse_token_list(text)]
    assert labels == [("write", "a"), ("read", "b")]


def test_parse_minted_token_extracts_value():
    # monet's `token add` prints the client .env line to copy.
    out = (
        "Created a write token for 'mercury'.\n"
        "  On the client:  add this line to that machine's .env:\n\n"
        "    PAINT_MONET_TOKEN=abc-123_XYZ\n\n"
    )
    assert parse_minted_token(out) == "abc-123_XYZ"


def test_parse_minted_token_absent():
    assert parse_minted_token("no token here") is None


def test_parse_status_running_and_health():
    text = (
        "container: monet\n"
        "state:     running\n"
        "status:    Up 3 minutes\n"
        "image:     ghcr.io/x/monet:0.7.0\n"
        "ports:     0.0.0.0:8002->8000/tcp\n"
        "health:    OK (http://127.0.0.1:8002/health)\n"
        "version:   0.7.0\n"
    )
    s = parse_status(text)
    assert s.running is True
    assert s.health_ok is True
    assert s.version == "0.7.0"
    assert s.image == "ghcr.io/x/monet:0.7.0"
    assert "8002->8000" in (s.ports or "")


def test_parse_status_down():
    s = parse_status("container: monet\nstate:     exited\n")
    assert s.running is False
    assert s.health_ok is False
    assert s.version is None


def test_admin_builds_quoted_commands():
    # Inject a fake run to capture the exact remote command strings, without
    # opening any SSH connection.
    calls: list[tuple[str, bool]] = []

    admin = MonetAdmin("host", "user", use_sudo=False)
    admin._run = lambda cmd, sudo=False: calls.append((cmd, sudo)) or "ok"

    admin.token_add("write", "micro-mercury")
    admin.restart()
    admin.upgrade("ghcr.io/x/monet:0.7.0")

    assert calls[0] == (
        "monet-admin token add --scope write --label micro-mercury",
        False,
    )
    assert calls[1] == ("monet-admin restart", False)
    # upgrade forces sudo regardless of use_sudo
    assert calls[2][1] is True
    assert calls[2][0].startswith("monet-admin upgrade ")


def test_admin_use_sudo_prefixes_all_ops():
    calls: list[tuple[str, bool]] = []
    admin = MonetAdmin("host", "user", use_sudo=True)
    admin._run = lambda cmd, sudo=False: calls.append((cmd, sudo)) or ""
    admin.token_list()
    assert calls[0][1] is True  # sudo applied


def test_admin_label_with_shell_metachars_is_quoted():
    # labels are charset-validated server-side, but the core must still quote.
    calls: list[tuple[str, bool]] = []
    admin = MonetAdmin("host", "user")
    admin._run = lambda cmd, sudo=False: calls.append((cmd, sudo)) or ""
    admin.token_revoke("a b")  # space -> must be quoted as one arg
    assert "'a b'" in calls[0][0]


def test_parse_status_state_wins_over_stale_status_line():
    # `state: exited` must beat a leftover `Up … (unhealthy)` status line.
    s = parse_status("state:  exited\nstatus: Up 2 minutes (unhealthy)\n")
    assert s.running is False


def test_admin_logs_follow_builds_tail_and_follow():
    calls: list[str] = []
    admin = MonetAdmin("h", "u")
    admin._run = lambda cmd, sudo=False: calls.append(cmd) or ""
    admin.logs(50, follow=True)
    admin.logs(100)
    assert calls[0] == "monet-admin logs -n 50 -f"
    assert calls[1] == "monet-admin logs -n 100"


def test_load_profile_coerces_types(tmp_path, monkeypatch):
    from monet.admin import profile as prof

    f = tmp_path / "profile.json"
    f.write_text('{"host":"h","port":"2222","use_sudo":true,"bogus":1}')
    monkeypatch.setenv("MONET_ADMIN_PROFILE", str(f))
    loaded = prof.load_profile()
    assert loaded.host == "h"
    assert loaded.port == 2222  # coerced str -> int, not left as "2222"
    assert loaded.use_sudo is True
    # unknown key ignored, no crash, defaults preserved
    assert loaded.user == ""


def test_load_profile_bad_json_returns_defaults(tmp_path, monkeypatch):
    f = tmp_path / "profile.json"
    f.write_text("{not valid json")
    monkeypatch.setenv("MONET_ADMIN_PROFILE", str(f))
    from monet.admin import profile as prof

    assert prof.load_profile().host == ""
