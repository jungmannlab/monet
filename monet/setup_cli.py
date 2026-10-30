"""
monet/setup_cli.py
~~~~~~~~~~~~~~~~~~~

``monet setup`` — an interactive terminal wizard that onboards an acquisition
machine: point it at (or stand up) a calibration server, wire up the central
config/protocol folder, author a microscope entry, verify the hardware, and
write it all into the per-machine ``.env``.

It is a thin front-end over :mod:`monet.setup_core` (all logic lives there, so
the GUI wizard behaves identically). Dispatched from ``monet/__main__.py`` before
the flat parser, alongside ``token`` and ``auth``.

:authors: Heinrich Grabmayr, 2026
:copyright: Copyright (c) 2026 Jungmann Lab, MPI of Biochemistry
"""

import argparse
import os

from monet import setup_core as sc


# ──────────────────────────────────────────────────────────────────────────
# Tiny prompt helpers (all input()/print() so tests can drive them)
# ──────────────────────────────────────────────────────────────────────────
def _ask(text, default=None):
    suffix = " [{}]".format(default) if default not in (None, "") else ""
    ans = input("{}{}: ".format(text, suffix)).strip()
    if ans:
        return ans
    return default if default is not None else ""


def _ask_required(text):
    while True:
        ans = input("{}: ".format(text)).strip()
        if ans:
            return ans
        print("  (required)")


def _ask_bool(text, default=True):
    hint = "Y/n" if default else "y/N"
    ans = input("{} [{}]: ".format(text, hint)).strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def _choose(text, options):
    """Pick from ``options`` = list of ``(key, label)``; returns the key."""
    print(text)
    for i, (_, label) in enumerate(options, 1):
        print("  {}. {}".format(i, label))
    while True:
        ans = input("select [1]: ").strip() or "1"
        try:
            idx = int(ans)
        except ValueError:
            idx = 0
        if 1 <= idx <= len(options):
            return options[idx - 1][0]
        print("  please enter a number 1-{}".format(len(options)))


def _coerce(raw, typ):
    return sc.coerce_value(raw, typ)


def _ask_fields(driver):
    """Collect a driver's ``init_kwargs`` from the user, with detected hints."""
    kwargs = {}
    for f in driver.fields:
        if f.choices_from:
            found = sc.enumerate_choices(f.choices_from)
            if found:
                print("  detected: {}".format(", ".join(map(str, found))))
        hint = " ({})".format(f.help) if f.help else ""
        label = "  {}{}".format(f.name, hint)
        while True:
            if f.required:
                raw = _ask_required(label)
            else:
                default = "" if f.default is None else f.default
                raw = _ask(label, default)
            # Omit an empty optional whose default is None (e.g. smoothing).
            if raw == "" and not f.required and f.default is None:
                break
            try:
                kwargs[f.name] = _coerce(raw, f.type)
                break
            except Exception:
                print("  could not parse {!r} as {}".format(raw, f.type))
    return kwargs


def _pick_driver(category, text):
    """Prompt the user to choose a driver in ``category``; return its name."""
    options = [(name, drv.label) for name, drv in sc.DRIVERS[category].items()]
    return _choose(text, options)


def _author_section(category, text):
    name = _pick_driver(category, text)
    kwargs = _ask_fields(sc.DRIVERS[category][name])
    return {"class": name, "init_kwargs": kwargs}


# Beam-path drivers whose protocol value is a boolean (open/close) rather than a
# Micro-Manager preset string.
_SHUTTER_CLASSES = sc.SHUTTER_BEAMPATH_CLASSES


def _parse_numbers(raw):
    """Parse a comma-separated list of numbers into ints (or floats)."""
    return sc.parse_number_list(raw)


# ──────────────────────────────────────────────────────────────────────────
# Wizard steps
# ──────────────────────────────────────────────────────────────────────────
def _step_server(updates):
    """Resolve the calibration database target into ``updates``.

    Returns the server URL to use as the microscope's ``database`` default, or
    ``None`` for the single-computer (local file, no server) case.
    """
    choice = _choose(
        "How will this machine reach its calibration database?",
        [
            (
                "existing",
                "Connect to an existing monet server (other machine)",
            ),
            ("local", "Single computer — local database file, no server"),
            ("new", "Set up a NEW monet server on another machine"),
        ],
    )
    if choice == "local":
        print(
            "  Single-computer mode: the microscope will use a local database "
            "file on this machine (no server, no token)."
        )
        return None
    if choice == "new":
        return _step_new_server(updates)

    while True:
        url = _ask_required("Server URL (e.g. http://host:8000)").rstrip("/")
        verdict = sc.check_server(url)
        print(
            "  reachable: {}".format("yes" if verdict["reachable"] else "NO")
        )
        if not verdict["reachable"]:
            print("  {}".format(verdict["detail"]))
            if _ask_bool("  try a different URL?", default=True):
                continue
            return None
        if verdict["ok"]:
            print("  authenticated: {}".format(verdict["detail"]))
            return url
        # Reachable but needs a token.
        print("  this server requires a token.")
        token = _ask_required("  paste the PAINT_MONET_TOKEN for this rig")
        os.environ[sc.ENV_CLIENT_TOKEN] = token
        verdict = sc.check_server(url)
        if verdict["ok"]:
            print("  authenticated: {}".format(verdict["detail"]))
            updates[sc.ENV_CLIENT_TOKEN] = token
            updates[sc.ENV_AUTH] = "on"
            return url
        print("  token rejected: {}".format(verdict["detail"]))
        if not _ask_bool("  try again?", default=True):
            # Don't leave a rejected token in the live env, and don't persist
            # it: warn that the rig has no working credential rather than
            # writing a config that will silently 401 later.
            os.environ.pop(sc.ENV_CLIENT_TOKEN, None)
            print(
                "  WARNING: no valid token saved — this rig will fail to "
                "authenticate against {}. Re-run `monet setup` or add "
                "PAINT_MONET_TOKEN to the .env once you have a working "
                "token.".format(url)
            )
            return url


def _step_new_server(updates):
    """Stand up a server (automated SSH, else guided manual) → URL+token."""
    host = _ask_required("Linux server hostname/IP")
    port = int(_ask("Server port", default="8000") or "8000")
    rig = _ask(
        "A label for this rig (for the token)", default="acquisition-rig"
    )

    if _ask_bool(
        "Install the server automatically over SSH now?", default=False
    ):
        user = _ask_required("  SSH username")
        key = _ask("  SSH private-key file (blank = use agent/password)", "")
        password = ""
        if not key:
            import getpass

            password = getpass.getpass("  SSH password (blank if key/agent): ")
        try:
            result = sc.remote_install_server(
                host,
                user,
                password=password or None,
                key_filename=key or None,
                port=port,
                rig=rig,
                on_output=lambda s: print(s, end=""),
            )
        except Exception as exc:
            print("  automated install failed: {}".format(exc))
            print("  falling back to manual instructions.\n")
        else:
            updates[sc.ENV_CLIENT_TOKEN] = result["token"]
            updates[sc.ENV_AUTH] = "on"
            print("  server ready at {}".format(result["url"]))
            return result["url"]

    # Guided manual fallback.
    print("\n" + sc.guided_server_instructions(host=host, port=port, rig=rig))
    if _ask_bool("Have you run those steps and have a token?", default=False):
        token = _ask_required("  paste the PAINT_MONET_TOKEN")
        updates[sc.ENV_CLIENT_TOKEN] = token
        updates[sc.ENV_AUTH] = "on"
    return "http://{}:{}".format(host, port)


def _step_central_folder(updates):
    """Resolve paths into ``updates``; return (configs_path, protocols_path)."""
    folder = _ask_required(
        "Folder for configs.yaml / protocols.yaml (central share or local)"
    )
    folder = os.path.expanduser(folder)
    configs_path = os.path.join(folder, "configs.yaml")
    protocols_path = os.path.join(folder, "protocols.yaml")
    if _ask_bool("Copy the example templates there to start from?", False):
        try:
            paths = sc.scaffold_central_folder(folder)
        except FileNotFoundError as exc:
            print("  could not copy templates: {}".format(exc))
            print("  continuing with empty config paths to author by hand.")
        else:
            configs_path, protocols_path = paths["configs"], paths["protocols"]
            print(
                "  scaffolded {} and {}".format(configs_path, protocols_path)
            )
    updates[sc.ENV_CONFIG_PATHS] = sc.paths_to_env([configs_path])
    updates[sc.ENV_PROTOCOL_PATHS] = sc.paths_to_env([protocols_path])
    return configs_path, protocols_path


def _author_protocol(name, answers, protocols_path):
    """Author a multi-laser calibration sweep (protocols.yaml) for ``name``.

    Uses the lasers/beam-path objects just authored; if a Micro-Manager ``.cfg``
    is supplied, beam-path presets are chosen from its scraped config groups so
    they can't drift from what Micro-Manager actually has.
    """
    lasers = answers.get("lasers") or {}
    if not lasers:
        return
    if not _ask_bool(
        "\nAuthor a calibration protocol (multi-laser sweep) now?",
        default=True,
    ):
        return

    laser_sequence = sorted(lasers)
    laser_powers = {}
    for wl in laser_sequence:
        while True:
            raw = _ask(
                "  {} nm power set-points [mW], comma-separated".format(wl),
                "100,200,500,1000",
            )
            try:
                laser_powers[wl] = _parse_numbers(raw)
                break
            except ValueError:
                print("  enter numbers separated by commas")

    beampath = _author_protocol_beampath(answers, laser_sequence)
    proto = sc.build_protocol_entry(
        {
            "laser_sequence": laser_sequence,
            "laser_powers": laser_powers,
            "beampath": beampath,
        }
    )
    if name in sc.existing_entry_names(protocols_path) and not _ask_bool(
        "  protocol '{}' already exists in {} — overwrite it?".format(
            name, protocols_path
        ),
        default=False,
    ):
        print("  kept the existing protocol; not overwritten.")
        return
    sc.write_protocol_entry(name, proto, protocols_path)
    print("  wrote protocol for {} to {}".format(name, protocols_path))


def _author_protocol_beampath(answers, laser_sequence):
    """Build the protocol ``beampath`` block (per laser + ``end``), or None."""
    bp_objs = answers.get("beampath") or {}
    if not bp_objs:
        return None

    cfg = None
    cfg_path = _ask(
        "  Micro-Manager .cfg path (optional — enables preset pick-lists)", ""
    )
    if cfg_path:
        try:
            cfg = sc.parse_mm_config(os.path.expanduser(cfg_path))
            print("    found config groups: {}".format(", ".join(cfg.groups)))
            if cfg.core_shutter:
                print("    core shutter: {}".format(cfg.core_shutter))
        except OSError as exc:
            print(
                "    could not read .cfg ({}); entering presets by hand".format(
                    exc
                )
            )

    # For each preset-driven object (filter/nosepiece), pick which scraped MM
    # group drives it once; only groups that actually have presets are offered.
    usable = sc.groups_with_presets(cfg)
    obj_group = {}
    if usable:
        for obj, spec in bp_objs.items():
            if sc.is_shutter_beampath(spec):
                continue
            obj_group[obj] = _choose(
                "  which MM config group drives '{}'?".format(obj),
                [(g, g) for g in usable],
            )

    def _label(step):
        return "end" if step == "end" else "{} nm".format(step)

    def preset_for(step, obj):
        if obj in obj_group:
            return _choose(
                "    {}: preset for '{}'".format(_label(step), obj),
                [(p, p) for p in cfg.groups[obj_group[obj]]],
            )
        return _ask("    {}: preset for '{}'".format(_label(step), obj), "")

    def shutter_open_for(step, obj):
        if step == "end":
            return False  # park + close at the end of the run
        return _ask_bool("    {} nm: open '{}'?".format(step, obj), True)

    return sc.assemble_protocol_beampath(
        bp_objs, laser_sequence, preset_for, shutter_open_for
    )


def _pick_entry_name(prompt, path):
    """Prompt for an entry name, showing what's already in ``path`` and
    confirming before an existing same-named entry would be overwritten."""
    existing = sc.existing_entry_names(path)
    if existing:
        print("  {} already has: {}".format(path, ", ".join(existing)))
    while True:
        name = _ask_required(prompt)
        if name in existing and not _ask_bool(
            "  '{}' already exists — overwrite it?".format(name), default=False
        ):
            continue
        return name


def _step_author_microscope(configs_path, protocols_path, server_url):
    """Author + verify + persist a microscope entry. Returns its name or None."""
    if not _ask_bool("Author a microscope entry now?", default=True):
        return None
    name = _pick_entry_name(
        "Microscope name (the <Name> you pass on the CLI)", configs_path
    )
    db_default = server_url or "./{}_power_database.xlsx".format(name)
    database = _ask(
        "Calibration database (server URL or .xlsx path)", db_default
    )
    wavelength = int(_ask("Index wavelength [nm]", "488") or "488")
    power = int(_ask("Index laser_power [mW]", "100") or "100")

    answers = {
        "name": name,
        "database": database,
        "wavelength": wavelength,
        "laser_power": power,
        "powermeter": _author_section("powermeter", "\nPower meter:"),
        "attenuation": _author_section("attenuation", "\nAttenuator:"),
        "analysis": _author_section("analysis", "\nAttenuation-curve model:"),
    }
    if _ask_bool("\nConfigure lasers (for multi-laser protocols)?", False):
        answers["lasers"] = {}
        while True:
            wl = _ask("  laser wavelength [nm] (blank to finish)", "")
            if not wl:
                break
            answers["lasers"][int(wl)] = _author_section(
                "laser", "  driver for {} nm:".format(wl)
            )
    if _ask_bool("Configure beam-path devices (Micro-Manager)?", False):
        answers["beampath"] = {}
        while True:
            obj = _ask("  device id, e.g. DC/shutter (blank to finish)", "")
            if not obj:
                break
            answers["beampath"][obj] = _author_section(
                "beampath", "  driver for {}:".format(obj)
            )

    entry = sc.build_microscope_entry(answers)

    if _ask_bool("\nTest hardware connection now?", default=True):
        for r in sc.test_microscope(entry):
            print(
                "  [{}] {}: {}".format(
                    "OK" if r.ok else "!!", r.device, r.detail
                )
            )

    sc.write_microscope_config(name, entry, configs_path)
    print("  wrote {} to {}".format(name, configs_path))

    _author_protocol(name, answers, protocols_path)
    return name


# ──────────────────────────────────────────────────────────────────────────
# Upgrade: move a single-computer lab's local database(s) to a server
# ──────────────────────────────────────────────────────────────────────────
def _run_upgrade(local_scopes, env_file):
    """Move existing local-file microscopes to a server (migrate + re-point)."""
    import monet

    configs_path = getattr(monet, "CONFIGS_PATH", "") or ""
    print(
        "\nUpgrading {} microscope(s) from a local database file to a "
        "server:".format(len(local_scopes))
    )
    updates = {}
    server_url = _step_server(updates)
    if not server_url:
        print("  No server configured — upgrade aborted; nothing changed.")
        return 1

    for name, cfg in local_scopes.items():
        db = cfg.get("database")
        xlsx = sc.resolve_local_db_path(db, configs_path)
        if xlsx:
            try:
                inserted, skipped = sc.migrate_local_db_to_server(
                    xlsx, server_url
                )
                print(
                    "  {}: migrated {} record(s) ({} already present)".format(
                        name, inserted, skipped
                    )
                )
            except Exception as exc:
                print(
                    "  {}: migration FAILED ({}); leaving it on the local "
                    "file, not re-pointing.".format(name, exc)
                )
                continue
        else:
            # Don't silently re-point a rig whose history we couldn't migrate —
            # that would leave its config pointing at an empty server with the
            # old calibrations stranded in an unreferenced file.
            print(
                "  {}: local database {!r} not found — leaving it as-is (not "
                "migrated or re-pointed). Fix the path and re-run `monet "
                "setup`, or re-point it by hand once migrated.".format(
                    name, db
                )
            )
            continue
        sc.repoint_microscope_to_server(name, server_url, configs_path)
        print("  {}: database -> {}".format(name, server_url))

    sc.write_env(updates, env_path=env_file)
    print(
        "\nUpgrade complete. Restart monet so the new config + .env take "
        "effect."
    )

    if _ask_bool(
        "\nSet up the new (additional) microscope now?", default=True
    ):
        protocols_path = getattr(monet, "PROTOCOLS_PATH", "") or os.path.join(
            os.path.dirname(configs_path or "."), "protocols.yaml"
        )
        _step_author_microscope(configs_path, protocols_path, server_url)
    return 0


# ──────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────
def run(env_file=None):
    """Run the interactive wizard. Returns a process exit code."""
    env_file = env_file or sc.default_env_path()
    state = sc.detect_state(probe_server=False)
    print("monet setup — guided onboarding\n")
    print("Writing per-machine settings to: {}".format(env_file))
    if state.configured:
        print(
            "This machine already has config at {!r}; the wizard will "
            "update it.".format(state.configs_path or env_file)
        )
    print()

    # Upgrade path: a single-computer lab (local DB files) growing to a server.
    local_scopes = sc.local_db_microscopes()
    if local_scopes and _ask_bool(
        "Detected microscope(s) on a local database file ({}). Move them to a "
        "server now?".format(", ".join(local_scopes)),
        default=False,
    ):
        return _run_upgrade(local_scopes, env_file)

    updates = {}
    server_url = _step_server(updates)
    configs_path, protocols_path = _step_central_folder(updates)
    sc.write_env(updates, env_path=env_file)
    print("\nSaved server/path settings to {}".format(env_file))

    name = _step_author_microscope(configs_path, protocols_path, server_url)

    print("\nSetup complete.")
    if name:
        print("  Try it:  python -m monet gui {}".format(name))
    else:
        print(
            "  Add a microscope entry to {} (see ONBOARDING Part 3).".format(
                configs_path
            )
        )
    # monet reads config/.env at import, so a running process won't see these
    # changes until it is restarted.
    print(
        "\n  Restart monet (and close any running GUI) for the new "
        "configuration to take effect."
    )
    return 0


def setup_cli(argv):
    """Entry point for ``monet setup`` (argv = args after ``setup``)."""
    parser = argparse.ArgumentParser(
        prog="monet setup",
        description="Interactive onboarding wizard for an acquisition machine.",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="path to the .env to write (default: the package-root .env monet "
        "loads at import).",
    )
    args = parser.parse_args(argv)
    try:
        return run(env_file=args.env_file)
    except (KeyboardInterrupt, EOFError):
        print("\naborted.")
        return 1
