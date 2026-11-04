"""
monet/migrate.py
~~~~~~~~~~~~~~~~

Migration utility to convert Excel calibration databases to SQLite.

:authors: Heinrich Grabmayr, 2024
:copyright: Copyright (c) 2024 Jungmann Lab, MPI of Biochemistry
"""

import json
import logging

import pandas as pd
from sqlalchemy.orm import Session

from monet import DATABASE_INDEXLEVELS
from monet.models import Calibration, get_engine

logger = logging.getLogger(__name__)


def _py(value):
    """Coerce a pandas/numpy scalar to a plain Python type (JSON-safe)."""
    return value.item() if hasattr(value, "item") else value


def _unpack_excel_index(idx):
    """Unpack a row's ``DATABASE_INDEXLEVELS`` index into the identity fields.

    Returns ``(name, wavelength, power, date, time)`` with the same coercions
    both migration paths use, so the SQLite and HTTP targets stay consistent.
    Note: ``date``/``time`` are stringified as stored in the Excel file (monet's
    own databases keep them as ``YYYY-MM-DD`` / ``HH:MM`` strings); a hand-made
    Excel with true date-typed cells would stringify differently — but
    identically for both targets.
    """
    if not isinstance(idx, tuple):
        idx = (idx,)
    if len(idx) < len(DATABASE_INDEXLEVELS):
        raise ValueError(
            "expected {} index levels ({}), got {}".format(
                len(DATABASE_INDEXLEVELS), DATABASE_INDEXLEVELS, len(idx)
            )
        )
    return str(idx[0]), float(idx[1]), float(idx[2]), str(idx[3]), str(idx[4])


def migrate_excel_to_sqlite(excel_path, db_path):
    """Read an existing Excel database and insert all records into SQLite.

    Preserves original dates and times from the Excel database.

    Parameters
    ----------
    excel_path : str
        Path to the source Excel database.
    db_path : str
        Path to the destination SQLite database file.
    """
    n_index = len(DATABASE_INDEXLEVELS)
    df = pd.read_excel(excel_path, index_col=list(range(n_index)))

    engine = get_engine(db_path)
    inserted = 0

    with Session(engine) as session:
        for idx, row in df.iterrows():
            name, wavelength, power, date, time = _unpack_excel_index(idx)
            params = {col: row[col] for col in row.index if pd.notna(row[col])}

            cal = Calibration(
                device_name=name,
                wavelength_nm=wavelength,
                laser_power_mw=power,
                calibration_date=date,
                calibration_time=time,
                parameters_json=json.dumps(params),
            )
            session.add(cal)
            inserted += 1

        session.commit()

    print(f"Migrated {inserted} records from {excel_path} to {db_path}")


def migrate_excel_to_server(
    excel_path,
    server_url,
    skip_existing=True,
    progress_callback=None,
    dry_run=False,
):
    """Upload an Excel calibration database to a monet server over HTTP.

    The counterpart to :func:`migrate_excel_to_sqlite` for the case where the
    server's SQLite file is not reachable on the local filesystem (e.g. a rig
    moving its single-computer history to a server on another machine). Posts
    each record to ``/calibrations`` **with its original date/time preserved**
    (the server honours them), using ``PAINT_MONET_TOKEN`` if set.

    Appending is idempotent when ``skip_existing`` (default): the server's
    current records are fetched once and any row already present (same name,
    wavelength, power, date and time) is skipped, so re-running is safe and
    **merging** an Excel DB into a server that already holds data is a clean
    union. If that fetch fails, it warns (to stderr too) that re-running may
    duplicate.

    Records are POSTed one per request — fine for a one-time migration of a
    typical calibration database; a very large DB over a high-latency link will
    be correspondingly slow.

    Parameters
    ----------
    progress_callback : callable, optional
        Called ``progress_callback(done, total)`` after each row (inserted or
        skipped), so a GUI can drive a progress bar.
    dry_run : bool
        When ``True``, perform the dedup check and count what *would* be
        inserted/skipped but POST nothing — for a pre-flight preview.

    Returns ``(inserted, skipped)``.
    """
    import sys
    import requests

    from monet.io import _auth_headers

    server_url = server_url.rstrip("/")
    headers = _auth_headers()
    n_index = len(DATABASE_INDEXLEVELS)
    df = pd.read_excel(excel_path, index_col=list(range(n_index)))

    existing = set()
    if skip_existing:
        try:
            resp = requests.post(
                f"{server_url}/calibrations/query",
                json={"index": {}, "time_idx": "all"},
                timeout=30,
                headers=headers,
            )
            # A fresh server (the init-populate case) has no records yet and the
            # query endpoint answers 404 — that's an empty set, not a failure,
            # so don't warn about possible duplicates.
            if resp.status_code != 404:
                resp.raise_for_status()
                for rec in resp.json().get("records", []):
                    existing.add(
                        (
                            str(rec["device_name"]),
                            float(rec["wavelength_nm"]),
                            float(rec["laser_power_mw"]),
                            str(rec["calibration_date"]),
                            str(rec["calibration_time"]),
                        )
                    )
        except Exception:
            logger.warning(
                "could not fetch existing records from %s; importing all rows",
                server_url,
                exc_info=True,
            )
            print(
                "  warning: could not check the server for existing records — "
                "importing all rows; re-running may create duplicates.",
                file=sys.stderr,
            )

    inserted = 0
    skipped = 0
    total = len(df)
    for i, (idx, row) in enumerate(df.iterrows()):
        name, wavelength, power, date, time = _unpack_excel_index(idx)
        if (name, wavelength, power, date, time) in existing:
            skipped += 1
        else:
            params = {
                col: _py(row[col]) for col in row.index if pd.notna(row[col])
            }
            if not dry_run:
                resp = requests.post(
                    f"{server_url}/calibrations",
                    json={
                        "index": {
                            "name": name,
                            "wavelength [nm]": wavelength,
                            "laser_power [mW]": power,
                        },
                        "parameters": params,
                        "calibration_date": date,
                        "calibration_time": time,
                    },
                    timeout=30,
                    headers=headers,
                )
                resp.raise_for_status()
            existing.add((name, wavelength, power, date, time))
            inserted += 1
        if progress_callback is not None:
            progress_callback(i + 1, total)

    print(
        "{} {} record(s) from {} to {} ({} already present)".format(
            "Would migrate" if dry_run else "Migrated",
            inserted,
            excel_path,
            server_url,
            skipped,
        )
    )
    return inserted, skipped
