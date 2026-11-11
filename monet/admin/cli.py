"""``monet-admin-cli`` — headless front-end over :mod:`.core`.

SSHes to the deployed monet server and runs the server-side ``monet-admin``
wrapper. The same operations the GUI exposes, scriptable from a terminal:

    monet-admin-cli --host mibweather status
    monet-admin-cli --host mibweather token list
    monet-admin-cli --host mibweather token add \\
        --scope write --label microscope-mercury
    monet-admin-cli --host mibweather --sudo upgrade \\
        ghcr.io/jungmannlab/monet:0.7.0

Connection flags (``--host``/``--user``/``--port``/``--identity``/``--ask-pass``
/``--sudo``) are shared across every subcommand.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from .core import AdminError, MonetAdmin


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group("connection")
    g.add_argument("--host", required=True, help="server hostname / IP")
    g.add_argument(
        "--user",
        default=getpass.getuser(),
        help="SSH user (default: current user)",
    )
    g.add_argument("--port", type=int, default=22, help="SSH port (22)")
    g.add_argument(
        "--identity", metavar="KEYFILE", help="SSH private key file"
    )
    g.add_argument(
        "--ask-pass",
        action="store_true",
        help="prompt for the SSH password (also used for sudo)",
    )
    g.add_argument(
        "--sudo",
        action="store_true",
        help="run the wrapper via sudo (if the SSH user needs it for docker)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monet-admin-cli",
        description="SSH admin console for a deployed monet server.",
    )
    _add_connection_args(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="container state + health + version")

    pt = sub.add_parser("token", help="manage bearer tokens")
    tsub = pt.add_subparsers(dest="tcmd", required=True)
    tsub.add_parser("list", help="list scopes + labels (never values)")
    pa = tsub.add_parser("add", help="mint a new token")
    pa.add_argument("--scope", choices=["read", "write"], required=True)
    pa.add_argument("--label", required=True)
    pr = tsub.add_parser("rotate", help="replace a label's token")
    pr.add_argument("--label", required=True)
    prv = tsub.add_parser("revoke", help="remove a label's token")
    prv.add_argument("--label", required=True)

    pl = sub.add_parser("logs", help="tail the container logs")
    pl.add_argument("-n", "--lines", type=int, default=200)
    pl.add_argument(
        "-f", "--follow", action="store_true", help="stream live (Ctrl-C)"
    )

    sub.add_parser("restart", help="restart the container")

    pu = sub.add_parser("upgrade", help="pull a new image + recreate")
    pu.add_argument("image_ref", help="e.g. ghcr.io/.../monet:0.7.0")

    return parser


def _make_admin(args, *, stream: bool) -> MonetAdmin:
    password = getpass.getpass("SSH password: ") if args.ask_pass else None
    on_output = (lambda s: print(s, end="", flush=True)) if stream else None
    return MonetAdmin(
        args.host,
        args.user,
        password=password,
        key_filename=args.identity,
        port=args.port,
        use_sudo=args.sudo,
        on_output=on_output,
    )


def _run_status(admin: MonetAdmin) -> int:
    with admin:
        s = admin.status()
    mark = "running" if s.running else "DOWN"
    health = "ok" if s.health_ok else "unreachable"
    print(f"state:   {mark}")
    print(f"health:  {health}")
    if s.version:
        print(f"version: {s.version}")
    if s.image:
        print(f"image:   {s.image}")
    if s.ports:
        print(f"ports:   {s.ports}")
    return 0 if s.running else 1


def _run_token(admin: MonetAdmin, args) -> int:
    if args.tcmd == "list":
        with admin:
            entries = admin.token_list()
        if not entries:
            print("no tokens configured")
            return 0
        print(f"{'SCOPE':6}  LABEL")
        for e in entries:
            print(f"{e.scope:6}  {e.label}")
        return 0
    with admin:
        if args.tcmd == "add":
            out = admin.token_add(args.scope, args.label)
        elif args.tcmd == "rotate":
            out = admin.token_rotate(args.label)
        else:  # revoke
            out = admin.token_revoke(args.label)
    print(out.rstrip())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # Streaming subcommands print live via on_output; the rest render results.
    stream = args.cmd == "upgrade" or (args.cmd == "logs" and args.follow)
    try:
        admin = _make_admin(args, stream=stream)
        if args.cmd == "status":
            return _run_status(admin)
        if args.cmd == "token":
            return _run_token(admin, args)
        if args.cmd == "logs":
            with admin:
                out = admin.logs(args.lines, follow=args.follow)
            if not stream:
                print(out.rstrip())
            return 0
        if args.cmd == "restart":
            with admin:
                print(admin.restart().rstrip())
            return 0
        if args.cmd == "upgrade":
            with admin:
                admin.upgrade(args.image_ref)
            return 0
    except AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
