"""
monet/schemas.py
~~~~~~~~~~~~~~~~

Pydantic schemas for the FastAPI server.

:authors: Heinrich Grabmayr, 2024
:copyright: Copyright (c) 2024 Jungmann Lab, MPI of Biochemistry
"""

from typing import List, Optional, Union

from pydantic import BaseModel


class CalibrationCreate(BaseModel):
    index: dict
    parameters: dict


class CalibrationQuery(BaseModel):
    index: dict
    time_idx: Union[str, List, None] = "last combinations"


class CalibrationRecord(BaseModel):
    device_name: str
    wavelength_nm: float
    laser_power_mw: float
    calibration_date: str
    calibration_time: str
    parameters: dict


class DatabaseResponse(BaseModel):
    records: List[CalibrationRecord]


class RestartResponse(BaseModel):
    backup_path: str
    remaining_records: int


class CalibrationDeleteQuery(BaseModel):
    device_name: Optional[str] = None
    wavelength_nm: Optional[float] = None
    laser_power_mw: Optional[float] = None
    calibration_date: Optional[str] = None
    calibration_time: Optional[str] = None


class CalibrationDeleteResponse(BaseModel):
    deleted_count: int


class FactorCreate(BaseModel):
    device_name: str
    wavelength_nm: float
    calibration_date: str
    transmission_objective_mean: float
    transmission_objective_std: float
    n_points: int


class FactorRecord(BaseModel):
    device_name: str
    wavelength_nm: float
    calibration_date: str
    transmission_objective_mean: float
    transmission_objective_std: float
    n_points: int


class FactorQuery(BaseModel):
    device_name: Optional[str] = None
    wavelength_nm: Optional[float] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None


class FactorListResponse(BaseModel):
    records: List[FactorRecord]


# ── Target-power API (WP-12a) ────────────────────────────────────────────────
# The recommender/PycroFlow sets a per-laser target power and reads back the
# measured power (so target + measured are logged to the registry). A power-set
# ACTUATES laser hardware, so this route is `write`-scoped and clamped to a hard
# per-laser safety ceiling (C34) before anything touches the instrument.


class PowerSetRequest(BaseModel):
    laser: int
    target_power_mw: float
    # 'fixed_laser' (PI on the attenuator) / 'fixed_attenuator' (scale laser
    # power) use the closed-loop meter feedback; 'combined' is open-loop from the
    # calibration. Omit to let the server choose (closed-loop when a meter is
    # present, else open-loop).
    mode: Optional[str] = None
    # Convergence tolerance for closed-loop modes, in percent of the target.
    tolerance_pct: Optional[float] = None


class PowerSetResponse(BaseModel):
    laser: int
    requested_power_mw: float  # what the caller asked for
    target_power_mw: float  # what was actually driven (clamped to the ceiling)
    measured_power_mw: float  # sample-plane power measured / predicted
    clamped: bool  # True if the request exceeded the safety ceiling
    max_power_mw: Optional[float]  # the per-laser ceiling in force, if any
    converged: bool
    iterations: int
    mode: str
    label: Optional[str] = None  # attributable token holder (auth on)


class PowerReadResponse(BaseModel):
    laser: int
    measured_power_mw: Optional[float]  # from the meter, if one is attached
    predicted_power_mw: Optional[float]  # from the calibration model
    has_powermeter: bool
