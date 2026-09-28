"""
monet/tokens.py
~~~~~~~~~~~~~~~

``monet token`` — manage the server's bearer tokens without hand-editing files.

Generates high-entropy tokens and maintains the ``PAINT_MONET_TOKENS`` map
(``token:scope:label,…``) in a ``.env`` file, so an operator on the server box
never has to invent random strings or edit the map by hand. This is the
server-side admin tool; it deliberately has no HTTP surface (whoever runs it
already has shell access to the box), which is why the dashboard is *not* a
token-minting endpoint — see ``docs/staging/ROLLOUT.md`` and ADR-001 / C18.

    monet token add --scope write --label microscope-mercury
    monet token list
    monet token revoke --label microscope-mercury
    monet token rotate --label microscope-mercury

Tokens are stored in **plaintext** in the ``.env`` (the ratified C18 model), so
the value is recoverable from that file by anyone with box access; this tool
prints it once on add/rotate for convenience. A running ``monet serve`` reads the
map at startup, so **restart serve to apply** a change.

:authors: Heinrich Grabmayr, 2024
:copyright: Copyright (c) 2024 Jungmann Lab, MPI of Biochemistry
"""

import argparse
import os
import secrets
import sys

from monet import _PKG_ROOT

_TOKENS_KEY = "PAINT_MONET_TOKENS"
_TOKEN_NBYTES = 32  # -> ~43-char url-safe string


def _default_env_file():
    """The server's .env (package root) — the same file monet loads at import."""
    return os.path.join(_PKG_ROOT, ".env")


def _read_map(env_file):
    """Return the current {token: TokenInfo} from the .env file (or live env)."""
    from monet.serviceauth import parse_tokens

    raw = None
    if os.path.exists(env_file):
        from dotenv import dotenv_values

        raw = dotenv_values(env_file).get(_TOKENS_KEY)
    if raw is None:
        raw = os.environ.get(_TOKENS_KEY)
    return parse_tokens(raw)


def _write_map(env_file, tokens):
    """Serialize {token: TokenInfo} back to the .env and the live process env."""
    from dotenv import set_key

    serialized = ",".join(
        "{}:{}:{}".format(tok, info.scope, info.label)
        for tok, info in tokens.items()
    )
    # Ensure the file exists (set_key needs it) then keep it secret-only.
    parent = os.path.dirname(os.path.abspath(env_file))
    if parent:
        os.makedirs(parent, exist_ok=True)
    open(env_file, "a").close()
    set_key(env_file, _TOKENS_KEY, serialized, quote_mode="never")
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass
    # Reflect into this process too (a running server still needs a restart).
    os.environ[_TOKENS_KEY] = serialized


def _new_token():
    return secrets.token_urlsafe(_TOKEN_NBYTES)


def _print_new(env_file, value, scope, label):
    print(
        "Created a {} token for {!r}.\n".format(scope, label)
        + "Stored in {} (PAINT_MONET_TOKENS). This tool won't print it "
        "again —\ncopy it now (it is also readable from that file).\n\n"
        "  On the server:  restart `monet serve` to apply.\n"
        "  On the client:  add this line to that machine's .env:\n\n"
        "    PAINT_MONET_TOKEN={}\n".format(env_file, value)
    )


def _add(env_file, scope, label):
    tokens = _read_map(env_file)
    if any(info.label == label for info in tokens.values()):
        print(
            "error: a token labelled {!r} already exists (use "
            "`monet token rotate`).".format(label),
            file=sys.stderr,
        )
        return 2
    from monet.serviceauth import TokenInfo

    value = _new_token()
    tokens[value] = TokenInfo(scope=scope, label=label)
    _write_map(env_file, tokens)
    _print_new(env_file, value, scope, label)
    return 0


def _list(env_file):
    tokens = _read_map(env_file)
    if not tokens:
        print("no tokens configured in {}".format(env_file))
        return 0
    print("{:6}  LABEL".format("SCOPE"))
    for info in sorted(tokens.values(), key=lambda i: (i.scope, i.label)):
        # never print the token value itself
        print("{:6}  {}".format(info.scope, info.label))
    return 0


def _revoke(env_file, label):
    tokens = _read_map(env_file)
    remaining = {t: i for t, i in tokens.items() if i.label != label}
    removed = len(tokens) - len(remaining)
    if removed == 0:
        print(
            "no token labelled {!r} in {}".format(label, env_file),
            file=sys.stderr,
        )
        return 2
    _write_map(env_file, remaining)
    print(
        "revoked {} token(s) labelled {!r} — restart `monet serve` to "
        "apply.".format(removed, label)
    )
    return 0


def _rotate(env_file, label):
    tokens = _read_map(env_file)
    matches = [i for i in tokens.values() if i.label == label]
    if not matches:
        print(
            "no token labelled {!r} in {}".format(label, env_file),
            file=sys.stderr,
        )
        return 2
    from monet.serviceauth import TokenInfo

    scope = matches[0].scope
    tokens = {t: i for t, i in tokens.items() if i.label != label}
    value = _new_token()
    tokens[value] = TokenInfo(scope=scope, label=label)
    _write_map(env_file, tokens)
    _print_new(env_file, value, scope, label)
    return 0


def token_cli(argv):
    """Entry point for ``monet token`` (argv = args after ``token``)."""
    parser = argparse.ArgumentParser(
        prog="monet token",
        description=(
            "Manage the server's bearer tokens (PAINT_MONET_TOKENS) in a .env "
            "file."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _env_arg(p):
        p.add_argument(
            "--env-file",
            default=None,
            help="path to the .env holding PAINT_MONET_TOKENS "
            "(default: the package-root .env the server loads).",
        )

    pa = sub.add_parser("add", help="generate and register a new token")
    pa.add_argument("--scope", choices=["read", "write"], required=True)
    pa.add_argument(
        "--label",
        required=True,
        help="holder name, e.g. microscope-mercury or dashboards",
    )
    _env_arg(pa)

    pl = sub.add_parser("list", help="list token scopes + labels (no values)")
    _env_arg(pl)

    pr = sub.add_parser("revoke", help="remove the token(s) for a label")
    pr.add_argument("--label", required=True)
    _env_arg(pr)

    prot = sub.add_parser(
        "rotate", help="replace a label's token with a fresh value"
    )
    prot.add_argument("--label", required=True)
    _env_arg(prot)

    args = parser.parse_args(argv)
    env_file = args.env_file or _default_env_file()

    if args.cmd == "add":
        return _add(env_file, args.scope, args.label)
    if args.cmd == "list":
        return _list(env_file)
    if args.cmd == "revoke":
        return _revoke(env_file, args.label)
    if args.cmd == "rotate":
        return _rotate(env_file, args.label)
    return 1
