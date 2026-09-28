#!/usr/bin/env python
"""
docs/staging/seed_calibration.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Seed a small linear calibration for the ``stg`` simulated microscope so the
staging power API has something to load (an uncalibrated instrument returns 422
on power-set). Writes an Excel calibration DB in the current directory (the path
the staging config's ``database:`` points at).

Run it from the directory you will ``monet serve`` from:

    python /path/to/monet/docs/staging/seed_calibration.py

No hardware or SDKs required — it only writes a spreadsheet.
"""

from datetime import datetime

import pandas as pd

from monet import DATABASE_INDEXLEVELS

DB_PATH = "staging_power_database.xlsx"


def main():
    now = datetime.now()
    datim = [now.strftime("%Y-%m-%d"), now.strftime("%H:%M")]
    # Linear calibration (LinearCurveAnalyzer): output = amp * attenuator_pos.
    # Two laser-power levels per laser so both fixed_laser and fixed_attenuator
    # modes have enough points.
    db = pd.DataFrame(
        index=pd.MultiIndex.from_product(
            [
                ["stg"],
                ["488", "561"],
                [50, 100],
                [datim[0]],
                [datim[1]],
            ],
            names=tuple(DATABASE_INDEXLEVELS),
        ),
        data={"bkg": [0, 0, 0, 0], "amp": [1.0, 2.0, 0.8, 1.6]},
    )
    db.to_excel(DB_PATH)
    print("wrote calibration ->", DB_PATH)


if __name__ == "__main__":
    main()
