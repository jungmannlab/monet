"""
monet/authcheck.py
~~~~~~~~~~~~~~~~~~

Client-side ``monet auth test``: verify a rig can authenticate to a monet
calibration/power server, and show the ``(scope, label)`` the server knows the
token by.

Deliberately client-only — it does NOT import the server auth stack
(``picasso_registry.auth`` / the ``[server]`` extra), so it runs on a bare rig
that only has the client installed. The actual probe lives in
:func:`monet.io.check_server_auth` (which reuses the same
``PAINT_MONET_TOKEN`` / ``PAINT_MONET_AUTH`` logic as the DB client).
"""

import argparse


def _resolve_url(name, url):
    """Return the server base URL from ``--url`` or a microscope's config."""
    if url:
        return url.rstrip("/")
    if name:
        from monet import CONFIGS

        try:
            cfg = CONFIGS[name]
        except (KeyError, TypeError):
            raise SystemExit(
                "unknown microscope {!r}; pass --url, or use a name present "
                "in the config.".format(name)
            )
        db = cfg.get("database") if isinstance(cfg, dict) else None
        if not (
            isinstance(db, str) and db.startswith(("http://", "https://"))
        ):
            raise SystemExit(
                "microscope {!r} has no server URL in its 'database' entry "
                "(got {!r}); pass --url instead.".format(name, db)
            )
        return db.rstrip("/")
    raise SystemExit("provide a microscope name or --url <server>.")


def _print_report(r):
    reachable = (
        "yes (health {})".format(r["health_status"])
        if r["reachable"]
        else "NO"
    )
    print("Server:        {}".format(r["server_url"]))
    print("Reachable:     {}".format(reachable))
    print("Token present: {}".format("yes" if r["token_present"] else "no"))
    if r["auth_enabled"] is not None:
        print(
            "Server auth:   {}".format(
                "enabled" if r["auth_enabled"] else "disabled"
            )
        )
    ok = r["authenticated"] or r["auth_enabled"] is False
    print("Authenticated: {} — {}".format("YES" if ok else "NO", r["detail"]))
    if r["label"]:
        print("Token label:   {}".format(r["label"]))
        print("Token scope:   {}".format(r["scope"]))


def _auth_test(name, url):
    from monet import io

    result = io.check_server_auth(_resolve_url(name, url))
    _print_report(result)
    # Exit 0 when the server accepted us (or auth is disabled); else 1.
    ok = result["authenticated"] or result["auth_enabled"] is False
    return 0 if ok else 1


def auth_cli(argv):
    """Entry point for ``monet auth`` (argv = args after ``auth``)."""
    parser = argparse.ArgumentParser(
        prog="monet auth",
        description="Test authentication against a monet server.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    pt = sub.add_parser(
        "test", help="check reachability + auth against a monet server"
    )
    pt.add_argument(
        "name",
        nargs="?",
        default=None,
        help="microscope name — reads its server URL from the config "
        "'database' entry",
    )
    pt.add_argument(
        "--url",
        default=None,
        help="server base URL (overrides the config's database URL)",
    )
    args = parser.parse_args(argv)
    if args.cmd == "test":
        return _auth_test(args.name, args.url)
    return 1
