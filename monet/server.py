"""
monet/server.py
~~~~~~~~~~~~~~~

FastAPI server for the calibration database **and** the target-power API.

Two surfaces share one app and one auth model:

* the calibration/factor **database** (create + query + prune), and
* the **target-power API** (WP-12a): an external recommender sets a per-laser
  target power (reusing the closed-loop PI setter) and reads back the measured
  power, so PycroFlow can log target + measured to the registry.

Authentication (A9 / ADR-001, C18) is the **shared** ``picasso_registry.auth``
helper, imported via :mod:`monet.serviceauth` (see that module). Every data
route is scoped: ``write`` on anything that mutates the DB or actuates the
laser, ``read`` on queries and power read-back; ``/health`` is public. With no
tokens configured the service is unauthenticated — allowed only on a loopback
bind (the fail-closed host guard in :mod:`monet.__main__` and the request-time
net in ``require_scope`` enforce that).

A monet ``write`` actuates laser hardware, so ``POST /power/set`` additionally
clamps the request to a hard per-laser **safety ceiling** (C34) *before* the
instrument is touched — enforced in code, not advisory.

:authors: Heinrich Grabmayr, 2024
:copyright: Copyright (c) 2024 Jungmann Lab, MPI of Biochemistry
"""

import json
import logging
import os
import shutil
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from monet.models import Calibration, Factor, get_engine
from monet.schemas import (
    CalibrationCreate,
    CalibrationDeleteQuery,
    CalibrationDeleteResponse,
    CalibrationQuery,
    CalibrationRecord,
    DatabaseResponse,
    FactorCreate,
    FactorListResponse,
    FactorQuery,
    FactorRecord,
    PowerReadResponse,
    PowerSetRequest,
    PowerSetResponse,
    RestartResponse,
)
from monet.serviceauth import AuthConfig, require_scope

logger = logging.getLogger(__name__)

# Module-level engine/session factory, set during lifespan
_engine = None
_SessionLocal = None

# Route-level auth dependencies (ADR 001 / C18): `write` guards DB mutations and
# laser actuation, `read` guards queries / power read-back. Attached via
# `dependencies=[...]` so they guard the route (and land the HTTPBearer scheme in
# the OpenAPI spec) without touching handler signatures. A no-op until tokens are
# configured — the loopback dev path and the in-process TestClient stay
# zero-config.
_READ = [Depends(require_scope("read"))]
_WRITE = [Depends(require_scope("write"))]


def _get_session():
    return _SessionLocal()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine, _SessionLocal
    db_path = os.environ.get("MONET_DB_PATH", "calibrations.db")
    _engine = get_engine(db_path)
    _SessionLocal = sessionmaker(bind=_engine)
    yield
    if _engine:
        _engine.dispose()


def _record_from_row(row: Calibration) -> CalibrationRecord:
    return CalibrationRecord(
        device_name=row.device_name,
        wavelength_nm=row.wavelength_nm,
        laser_power_mw=row.laser_power_mw,
        calibration_date=row.calibration_date,
        calibration_time=row.calibration_time,
        parameters=json.loads(row.parameters_json),
    )


def create_app(
    auth: AuthConfig | None = None,
    instrument=None,
    powermeter=None,
    config=None,
) -> FastAPI:
    """Build a fresh app instance (used by the service, tests, and export).

    ``auth`` is the token map the ``require_scope`` dependencies enforce; it
    defaults to :func:`monet.serviceauth.auth_from_env` (``PAINT_MONET_TOKENS``),
    so an unset variable yields a disabled config and the loopback dev path /
    in-process TestClient stay zero-config.

    ``instrument`` / ``powermeter`` / ``config`` power the target-power API. When
    ``instrument`` is None (the DB-only deployment — ``monet serve`` with no
    microscope name) the ``/power`` routes still exist (so the auth scheme and
    the contract are visible) but return **503**. Build them with
    :func:`build_app_for_microscope`.
    """
    app = FastAPI(title="Monet Calibration Server", lifespan=lifespan)
    app.state.auth = (
        auth if auth is not None else AuthConfig.from_env("PAINT_MONET_TOKENS")
    )
    app.state.instrument = instrument
    app.state.powermeter = powermeter
    app.state.config = (
        config if config is not None else getattr(instrument, "config", None)
    )

    # ── calibration database ─────────────────────────────────────────────────

    @app.post(
        "/calibrations",
        response_model=CalibrationRecord,
        dependencies=_WRITE,
    )
    def save_calibration(data: CalibrationCreate):
        """Save a calibration record. Date/time are auto-generated."""
        now = datetime.now()
        index = data.index
        row = Calibration(
            device_name=str(index.get("name", "")),
            wavelength_nm=float(index.get("wavelength [nm]", 0)),
            laser_power_mw=float(index.get("laser_power [mW]", 0)),
            calibration_date=now.strftime("%Y-%m-%d"),
            calibration_time=now.strftime("%H:%M"),
            parameters_json=json.dumps(data.parameters),
        )
        with _get_session() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return _record_from_row(row)

    @app.post(
        "/calibrations/query",
        response_model=DatabaseResponse,
        dependencies=_READ,
    )
    def query_calibrations(query: CalibrationQuery):
        """Query calibration records.

        Index values of None act as wildcards (match all).
        time_idx modes: 'latest', 'last date', 'last combinations', 'all',
        or a list of [date] or [date, time].
        """
        index = query.index
        time_idx = query.time_idx

        with _get_session() as session:
            stmt = select(Calibration)

            # Apply index filters (None = wildcard)
            device = index.get("name")
            if device is not None:
                stmt = stmt.where(Calibration.device_name == str(device))

            wavelength = index.get("wavelength [nm]")
            if wavelength is not None:
                stmt = stmt.where(
                    Calibration.wavelength_nm == float(wavelength)
                )

            laser_power = index.get("laser_power [mW]")
            if laser_power is not None:
                stmt = stmt.where(
                    Calibration.laser_power_mw == float(laser_power)
                )

            # Handle time_idx as list: [date] or [date, time]
            if isinstance(time_idx, (list, tuple)):
                if len(time_idx) >= 1:
                    stmt = stmt.where(
                        Calibration.calibration_date == str(time_idx[0])
                    )
                if len(time_idx) >= 2:
                    stmt = stmt.where(
                        Calibration.calibration_time == str(time_idx[1])
                    )
                # With explicit date/time, just return sorted
                stmt = stmt.order_by(
                    Calibration.device_name,
                    Calibration.wavelength_nm,
                    Calibration.laser_power_mw,
                    Calibration.calibration_date,
                    Calibration.calibration_time,
                )
                rows = session.execute(stmt).scalars().all()
                return DatabaseResponse(
                    records=[_record_from_row(r) for r in rows]
                )

            # Apply date/time filter from index if present
            date_val = index.get("date")
            if date_val is not None:
                stmt = stmt.where(
                    Calibration.calibration_date == str(date_val)
                )

            time_val = index.get("time")
            if time_val is not None:
                stmt = stmt.where(
                    Calibration.calibration_time == str(time_val)
                )

            # Sort by all index columns + date/time
            stmt = stmt.order_by(
                Calibration.device_name,
                Calibration.wavelength_nm,
                Calibration.laser_power_mw,
                Calibration.calibration_date,
                Calibration.calibration_time,
            )
            rows = session.execute(stmt).scalars().all()

            if not rows:
                raise HTTPException(
                    status_code=404, detail="No matching calibrations found."
                )

            if time_idx is None or time_idx == "latest":
                # Return only the last record overall
                return DatabaseResponse(records=[_record_from_row(rows[-1])])

            elif time_idx == "last date":
                # Find the latest date, return all records from that date
                last_date = max(r.calibration_date for r in rows)
                filtered = [r for r in rows if r.calibration_date == last_date]
                return DatabaseResponse(
                    records=[_record_from_row(r) for r in filtered]
                )

            elif time_idx == "last combinations":
                # For each (device, wavelength, power) combo, keep only the
                # last entry
                seen = {}
                for r in rows:
                    key = (r.device_name, r.wavelength_nm, r.laser_power_mw)
                    seen[key] = r  # later entries overwrite earlier (sorted)
                result = list(seen.values())
                return DatabaseResponse(
                    records=[_record_from_row(r) for r in result]
                )

            elif time_idx == "all":
                return DatabaseResponse(
                    records=[_record_from_row(r) for r in rows]
                )

            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown time_idx mode: {time_idx}",
                )

    @app.post(
        "/calibrations/delete",
        response_model=CalibrationDeleteResponse,
        dependencies=_WRITE,
    )
    def delete_calibrations(query: CalibrationDeleteQuery):
        """Delete calibration records matching the query.

        None values act as wildcards.
        """
        with _get_session() as session:
            stmt = select(Calibration)
            if query.device_name is not None:
                stmt = stmt.where(Calibration.device_name == query.device_name)
            if query.wavelength_nm is not None:
                stmt = stmt.where(
                    Calibration.wavelength_nm == query.wavelength_nm
                )
            if query.laser_power_mw is not None:
                stmt = stmt.where(
                    Calibration.laser_power_mw == query.laser_power_mw
                )
            if query.calibration_date is not None:
                stmt = stmt.where(
                    Calibration.calibration_date == query.calibration_date
                )
            if query.calibration_time is not None:
                stmt = stmt.where(
                    Calibration.calibration_time == query.calibration_time
                )
            rows = session.execute(stmt).scalars().all()
            for row in rows:
                session.delete(row)
            session.commit()
        return CalibrationDeleteResponse(deleted_count=len(rows))

    @app.post(
        "/database/restart",
        response_model=RestartResponse,
        dependencies=_WRITE,
    )
    def restart_database():
        """Backup the current database and prune to only the latest entries."""
        db_path = os.environ.get("MONET_DB_PATH", "calibrations.db")
        today = datetime.now().strftime("%Y-%m-%d")
        root, ext = os.path.splitext(db_path)
        backup_path = f"{root}_{today}{ext}"

        if os.path.exists(backup_path):
            raise HTTPException(
                status_code=409,
                detail=f"Backup file already exists: {backup_path}",
            )

        # Copy current DB as backup
        shutil.copy2(db_path, backup_path)

        # Keep only last combination per (device, wavelength, power)
        with _get_session() as session:
            all_rows = (
                session.execute(
                    select(Calibration).order_by(
                        Calibration.device_name,
                        Calibration.wavelength_nm,
                        Calibration.laser_power_mw,
                        Calibration.calibration_date,
                        Calibration.calibration_time,
                    )
                )
                .scalars()
                .all()
            )

            # Find rows to keep
            keep = {}
            for r in all_rows:
                key = (r.device_name, r.wavelength_nm, r.laser_power_mw)
                keep[key] = r.id

            keep_ids = set(keep.values())

            # Delete rows not in keep set
            for r in all_rows:
                if r.id not in keep_ids:
                    session.delete(r)
            session.commit()

            remaining = session.execute(select(Calibration)).scalars().all()
            return RestartResponse(
                backup_path=backup_path,
                remaining_records=len(remaining),
            )

    @app.post("/factors", response_model=FactorRecord, dependencies=_WRITE)
    def save_factor(data: FactorCreate):
        """Save or update a transmission_objective factor record."""
        with _get_session() as session:
            # Replace existing record for same (device, wavelength, date)
            stmt = select(Factor).where(
                Factor.device_name == data.device_name,
                Factor.wavelength_nm == data.wavelength_nm,
                Factor.calibration_date == data.calibration_date,
            )
            existing = session.execute(stmt).scalar_one_or_none()
            if existing:
                existing.transmission_objective_mean = (
                    data.transmission_objective_mean
                )
                existing.transmission_objective_std = (
                    data.transmission_objective_std
                )
                existing.n_points = data.n_points
                session.commit()
                session.refresh(existing)
                row = existing
            else:
                row = Factor(
                    device_name=data.device_name,
                    wavelength_nm=data.wavelength_nm,
                    calibration_date=data.calibration_date,
                    transmission_objective_mean=(
                        data.transmission_objective_mean
                    ),
                    transmission_objective_std=data.transmission_objective_std,
                    n_points=data.n_points,
                )
                session.add(row)
                session.commit()
                session.refresh(row)
            return FactorRecord(
                device_name=row.device_name,
                wavelength_nm=row.wavelength_nm,
                calibration_date=row.calibration_date,
                transmission_objective_mean=row.transmission_objective_mean,
                transmission_objective_std=row.transmission_objective_std,
                n_points=row.n_points,
            )

    @app.post(
        "/factors/query",
        response_model=FactorListResponse,
        dependencies=_READ,
    )
    def query_factors(query: FactorQuery):
        """Query transmission_objective factor records."""
        with _get_session() as session:
            stmt = select(Factor).order_by(
                Factor.device_name,
                Factor.wavelength_nm,
                Factor.calibration_date,
            )
            if query.device_name is not None:
                stmt = stmt.where(Factor.device_name == query.device_name)
            if query.wavelength_nm is not None:
                stmt = stmt.where(Factor.wavelength_nm == query.wavelength_nm)
            if query.date_from is not None:
                stmt = stmt.where(Factor.calibration_date >= query.date_from)
            if query.date_to is not None:
                stmt = stmt.where(Factor.calibration_date <= query.date_to)
            rows = session.execute(stmt).scalars().all()
        return FactorListResponse(
            records=[
                FactorRecord(
                    device_name=r.device_name,
                    wavelength_nm=r.wavelength_nm,
                    calibration_date=r.calibration_date,
                    transmission_objective_mean=r.transmission_objective_mean,
                    transmission_objective_std=r.transmission_objective_std,
                    n_points=r.n_points,
                )
                for r in rows
            ]
        )

    # ── target-power API (WP-12a) ────────────────────────────────────────────

    @app.post("/power/set", response_model=PowerSetResponse)
    def set_power(
        req: PowerSetRequest,
        request: Request,
        token=Depends(require_scope("write")),
    ):
        """Drive a per-laser target power and return the measured power.

        Reuses the closed-loop PI setter (:func:`monet.control.run_power_feedback`)
        when a power meter is attached, else an open-loop calibration set. The
        request is **clamped to the hard per-laser safety ceiling** (C34) before
        the instrument is touched — an unbounded actuation request cannot reach
        the hardware. ``write`` scope is required: unauthenticated laser
        actuation is refused before any hardware call.
        """
        from monet.control import run_power_feedback

        instrument = request.app.state.instrument
        powermeter = request.app.state.powermeter
        config = request.app.state.config
        if config is None:
            config = getattr(instrument, "config", None)

        if instrument is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "no instrument configured; start with "
                    "`monet serve <MicroscopeName>` to enable the power API"
                ),
            )

        laser = req.laser
        if laser not in instrument.laser:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"laser {laser} not available; "
                    f"configured lasers: {instrument.laser}"
                ),
            )
        requested = float(req.target_power_mw)
        if requested < 0:
            raise HTTPException(
                status_code=422, detail="target power must be non-negative"
            )

        # ── SAFETY INTERLOCK (C34) ──────────────────────────────────────────
        # The hard per-laser ceiling is enforced in the control layer, below
        # every actuation path (control.clamp_to_max_power, applied by the power
        # setter / set_power_fixed_* / run_power_feedback), so the GUI and CLI
        # are bounded too — not just this route. Here we resolve the ceiling and
        # the delivered target for the response so the caller sees when its
        # request was clamped down (fail-safe: the hardware is never driven above
        # the ceiling).
        ceiling = instrument.max_power(laser)
        target, clamped = instrument.clamp_to_max_power(requested, laser)

        # Select + enable the laser (this is an actuation route).
        instrument.laser = laser
        instrument.laser_enabled = True

        has_meter = powermeter is not None
        mode = req.mode or ("fixed_laser" if has_meter else "combined")

        if has_meter and mode in ("fixed_laser", "fixed_attenuator"):
            fb = config.get("feedback", {}) if isinstance(config, dict) else {}
            tol = (
                req.tolerance_pct
                if req.tolerance_pct is not None
                else float(fb.get("tol_pct", 1.0))
            )
            try:
                result = run_power_feedback(
                    instrument,
                    powermeter,
                    target,
                    laser,
                    mode,
                    kp=float(fb.get("kp", 0.85)),
                    ki=float(fb.get("ki", 0.15)),
                    max_dev_pct=tol,
                    max_iter=int(fb.get("max_iter", 20)),
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            measured = float(result["measured"])
            converged = bool(result["converged"])
            iterations = int(result["iterations"])
        else:
            # Open-loop: set from the calibration, then read the meter if any.
            try:
                instrument.power = target
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            if has_meter:
                raw = powermeter.read()
                measured = float(instrument.to_sample_plane(raw, laser))
            else:
                measured = float(instrument.power)
            converged = True
            iterations = 0
            mode = "combined"

        return PowerSetResponse(
            laser=laser,
            requested_power_mw=requested,
            target_power_mw=target,
            measured_power_mw=measured,
            clamped=clamped,
            max_power_mw=ceiling,
            converged=converged,
            iterations=iterations,
            mode=mode,
            label=getattr(token, "label", None),
        )

    @app.get("/power", response_model=PowerReadResponse, dependencies=_READ)
    def read_power(request: Request, laser: int | None = None):
        """Read back the current power (measured if a meter is attached).

        Truly read-only: it does not switch lines, change any set-point, or
        enable a laser. It reports the **currently active** laser — the only
        one for which a live meter reading is physically meaningful (the meter
        sees whatever is emitting). If ``laser`` is supplied and is not the
        active laser, the request is rejected (409); select it with
        ``POST /power/set`` first.
        """
        instrument = request.app.state.instrument
        powermeter = request.app.state.powermeter
        if instrument is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "no instrument configured; start with "
                    "`monet serve <MicroscopeName>` to enable the power API"
                ),
            )
        current = instrument.curr_laser
        if laser is not None and laser != current:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"laser {laser} is not the active laser ({current}); "
                    "select it via POST /power/set before reading back"
                ),
            )
        laser = current

        predicted = None
        try:
            predicted = float(instrument.power)
        except Exception:
            predicted = None
        measured = None
        if powermeter is not None:
            try:
                raw = powermeter.read()
                measured = float(instrument.to_sample_plane(raw, laser))
            except Exception:
                measured = None
        return PowerReadResponse(
            laser=laser,
            measured_power_mw=measured,
            predicted_power_mw=predicted,
            has_powermeter=powermeter is not None,
        )

    # /health is deliberately unauthenticated: liveness/readiness probes and the
    # reverse proxy can't carry a bearer token, and it exposes nothing sensitive.
    @app.get("/health")
    def health():
        """Health check endpoint."""
        return {"status": "ok"}

    # Dashboard UI — imported here to avoid circular-import issues. Browser
    # access is guarded at the reverse proxy (HTTP Basic / lab SSO per ADR-001),
    # not by bearer tokens, so the router is mounted as-is.
    from monet import dashboard as _dashboard_module

    app.include_router(_dashboard_module.router)

    return app


def build_app_for_microscope(name, configs_file=None):
    """Build a power-API app around the instrument for microscope ``name``.

    Loads the microscope config (from ``configs_file`` if given, else the
    configured ``CONFIGS``), builds the illumination control and (optionally) the
    power meter, and returns an app with the ``/power`` routes live. Auth is read
    from ``PAINT_MONET_TOKENS`` as usual.

    The instrument is built with ``auto_enable_lasers=False`` so that loading the
    calibration never starts emission; ``POST /power/set`` enables the laser
    explicitly when it actuates.
    """
    import yaml as _yaml

    from monet import CONFIGS
    from monet.control import IlluminationLaserControl
    from monet.util import load_class

    configs = CONFIGS
    if configs_file:
        with open(configs_file, "r") as cf:
            configs = _yaml.full_load(cf)
    try:
        config = configs[name]
    except KeyError as exc:
        raise KeyError(
            f"Microscope {name!r} not found in configurations."
        ) from exc

    instrument = IlluminationLaserControl(config, auto_enable_lasers=False)
    try:
        instrument.load_calibration_database()
    except Exception as exc:
        logger.warning(
            "Could not load calibration for %s: %s. Power API will refuse "
            "to set power until the microscope is calibrated.",
            name,
            exc,
        )

    powermeter = None
    try:
        pwrconfig = config["powermeter"]
        powermeter = load_class(
            pwrconfig["classpath"], pwrconfig["init_kwargs"]
        )
    except Exception as exc:
        logger.warning(
            "No power meter for %s (%s). Power API falls back to open-loop "
            "calibration setting.",
            name,
            exc,
        )

    return create_app(
        instrument=instrument, powermeter=powermeter, config=config
    )


# Default module-level app: DB-only, auth from PAINT_MONET_TOKENS. Kept so
# `from monet.server import app` and `monet serve` (no microscope name) work
# unchanged. Use build_app_for_microscope() to enable the power API.
app = create_app()
