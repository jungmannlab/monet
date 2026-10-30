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
            # idx is a tuple matching DATABASE_INDEXLEVELS
            if not isinstance(idx, tuple):
                idx = (idx,)

            params = {col: row[col] for col in row.index if pd.notna(row[col])}

            cal = Calibration(
                device_name=str(idx[0]),
                wavelength_nm=float(idx[1]),
                laser_power_mw=float(idx[2]),
                calibration_date=str(idx[3]),
                calibration_time=str(idx[4]),
                parameters_json=json.dumps(params),
            )
            session.add(cal)
            inserted += 1

        session.commit()

    print(f"Migrated {inserted} records from {excel_path} to {db_path}")


def migrate_excel_to_server(excel_path, server_url, skip_existing=True):
    """Upload an Excel calibration database to a monet server over HTTP.

    The counterpart to :func:`migrate_excel_to_sqlite` for the case where the
    server's SQLite file is not reachable on the local filesystem (e.g. a rig
    moving its single-computer history to a server on another machine). Posts
    each record to ``/calibrations`` **with its original date/time preserved**
    (the server honours them), using ``PAINT_MONET_TOKEN`` if set.

    Appending is idempotent when ``skip_existing`` (default): the server's
    current records are fetched once and any row already present (same name,
    wavelength, power, date and time) is skipped, so re-running is safe.

    Returns ``(inserted, skipped)``.
    """
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

    inserted = 0
    skipped = 0
    for idx, row in df.iterrows():
        if not isinstance(idx, tuple):
            idx = (idx,)
        name = str(idx[0])
        wavelength = float(idx[1])
        power = float(idx[2])
        date = str(idx[3])
        time = str(idx[4])
        if (name, wavelength, power, date, time) in existing:
            skipped += 1
            continue
        params = {
            col: _py(row[col]) for col in row.index if pd.notna(row[col])
        }
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

    print(
        "Migrated {} record(s) from {} to {} ({} already present)".format(
            inserted, excel_path, server_url, skipped
        )
    )
    return inserted, skipped
