#!/usr/bin/env python
"""
monet/analysis.py
~~~~~~~~~~~~~~~~~

Analysis of attenuation curves.

:authors: Heinrich Grabmayr, 2022
:copyright: Copyright (c) 2022 Jungmann Lab, MPI of Biochemistry
"""

import abc
import logging
import re
from collections.abc import Iterable

import lmfit
import matplotlib.pyplot as plt
import numpy as np
from icecream import ic
from numpy.polynomial import Polynomial as _Polynomial

logger = logging.getLogger(__name__)
ic.configureOutput(outputFunction=logger.debug)


class AbstractAttenuationCurveAnalyzer(abc.ABC):
    """An abstract class for analyzing attenuation curves.

    Attenuation curves (Power vs control parameter) are expected to have
    specific shapes (e.g. sinusoidal for Polarization-Rotation attenuation)
    or can be fit with a spline.
    An AttenuationCurveAnalyzer fits a model to calibration data, and then
    uses that to return a control parameter for a desired power output.
    """

    def __init__(self, analysis_parameters):
        """Initialize the analyzer.

        Parameters
        ----------
        analysis_parameters : dict
            Configuration with keys ``min`` (float, minimum control
            parameter such as angle) and ``max`` (float, maximum control
            parameter).
        """
        self.model = lmfit.Model(self._model_function)
        self.analysis_parameters = analysis_parameters

    @abc.abstractmethod
    def _model_function(x, **pars):
        """Evaluate the fitting model function."""
        pass

    @abc.abstractmethod
    def _model_function_inv(y, **pars):
        """Evaluate the inverse of the model function."""
        pass

    @abc.abstractmethod
    def _model_function_estinit(x, y):
        """Estimate initial parameters for the model function."""
        pass

    @abc.abstractmethod
    def output_range(self):
        pass

    def fit(self, x, y):
        """Fit a model from calibration data.

        Parameters
        ----------
        x : numeric, same shape as y
            The control parameters (e.g. angle) corresponding to y.
        y : scalar or 1d array
            Desired power output.
        """
        init_pars = self._model_function_estinit(y, x)
        self.fit_result = self.model.fit(y, x=x, verbose=False, **init_pars)
        self.curr_params = self.get_model()

    def estimate(self, y):
        """Estimate control parameter needed to reach a given power.

        Parameters
        ----------
        y : scalar or 1d array
            Desired power output.

        Returns
        -------
        x : numeric, same shape as y
            The control parameters (e.g. angle) corresponding to y
            using the current model.
        """
        pars = self.curr_params
        minimax = {
            "mini": self.analysis_parameters["min"],
            "maxi": self.analysis_parameters["max"],
        }
        return self._model_function_inv(y, **pars, **minimax)

    def estimate_power(self, x):
        """Estimate power for a given control parameter.

        Parameters
        ----------
        x : scalar or 1d array
            Angular value.

        Returns
        -------
        y : numeric, same shape as x
            Estimated power output.
        """
        if isinstance(self.curr_params, dict):
            return self.model.eval(**self.curr_params, x=x)
        else:
            return self.model.eval(self.curr_params, x=x)

    def get_model(self):
        """Return current model parameters.

        Returns
        -------
        model_parameters : dict
            The model parameters.
        """
        if hasattr(self, "fit_result"):
            return self.fit_result.params.valuesdict()
        else:
            return self.curr_params

    def load_model(self, model_parameters):
        """Load a model from parameters.

        Parameters
        ----------
        model_parameters : dict
            The model parameters.
        """
        self.model.make_params(**model_parameters)
        self.curr_params = model_parameters

    def plot(self, fname, xlabel=None, ylabel=None, title=None):
        """Plot the outcome of the analysis.

        Parameters
        ----------
        fname : str
            The file name to save the plot at.
        xlabel, ylabel, title : str, optional
            Axis labels and plot title.
        """
        plt.switch_backend("agg")
        fig = self.fit_result.plot(
            show_init=False, xlabel=xlabel, ylabel=ylabel, title=title
        )
        # switch on gridlines
        for ax in fig.get_axes():
            ax.xaxis.grid(True)
            ax.yaxis.grid(True)
        fig.savefig(fname)
        plt.close(fig)


class SinusAttenuationCurveAnalyzer(AbstractAttenuationCurveAnalyzer):
    """Sinusoidal attenuation model for half-wave-plate polarization.

    The polarization angle is twice the wave-plate angle::

        P(alpha) = bkg + amp * sin(2 * (alpha + phi))**2

    Notes
    -----
    ``P`` is the power and ``alpha`` the angle of the half-wave plate.
    Model parameters: ``bkg`` (background power), ``amp`` (amplitude) and
    ``phi`` (offset rotation of the half-wave plate).
    """

    def __init__(self, analysis_parameters):
        """Initialize the analyzer.

        Parameters
        ----------
        analysis_parameters : dict
            Configuration with keys ``min`` (float, minimum control
            parameter such as angle) and ``max`` (float, maximum control
            parameter).
        """
        super().__init__(analysis_parameters)

    def _model_function(self, x, bkg, amp, phi):
        """Evaluate a squared-sine with twice the angle, background, offset.

        With ``sin**2(alpha) = (1 + sin(2 * alpha)) / 2``::

            P(alpha) = bkg + amp * sin(2 * (alpha + phi))**2
                     = bkg + amp * (1 + sin(4 * pi / 180 * (alpha + phi))) / 2

        Parameters
        ----------
        x : float or array
            Input angle, in deg.
        bkg : float
            Background.
        amp : float
            Amplitude.
        phi : float
            Angular offset in deg.

        Returns
        -------
        result : float or array
            The squared sine etc.
        """
        return bkg + amp * (1 + np.sin(4 * np.pi / 180 * (x + phi))) / 2

    def _model_function_inv(self, y, bkg, amp, phi, mini, maxi):
        """Calculate the inverse of the squared sine.

        Notes
        -----
        ``alpha = np.arcsin((out - bkg) / amp * 2 - 1) / 2 * 180 / np.pi
        - phi``
        """
        logger.debug("inverting squared sinus model function")
        logger.debug(
            f"y={str(y)}, bkg={str(bkg)}, amp={str(amp)}, phi={str(phi)}"
        )
        if np.any(y < bkg) or np.any(y > bkg + amp):
            raise ValueError(
                "Desired value y={:s} out of range. ".format(str(y))
                + "Should be between bkg={:s} and amp+bkg={:s}".format(
                    str(bkg), str(bkg + amp)
                )
            )
        alpha = np.arcsin((y - bkg) / amp * 2 - 1) / 4 * 180 / np.pi - phi
        if isinstance(alpha, np.ndarray):
            alpha[alpha < mini] = alpha[alpha < mini] + 90
            alpha[alpha > maxi] = alpha[alpha > maxi] - 90
        else:
            for i in range(5):
                if alpha < mini:
                    alpha += 90
                elif alpha > maxi:
                    alpha -= 90
                else:
                    break
        return alpha

    def _model_function_estinit(self, y, x):
        """Estimate initial parameters for a squared-sinusoidal fit.

        Parameters
        ----------
        y : array (N)
            The result data.
        x : array (N)
            The angular data.

        Returns
        -------
        pars : dict
            Keys: bkg, amp, phi.
        """
        pars = {
            "bkg": np.min(y),
            "amp": np.max(y) - np.min(y),
            "phi": x[np.argmax(y)] + 90 / 4,
        }
        self.model.make_params(pars)
        self.model.set_param_hint("bkg", min=0)
        self.model.set_param_hint("amp", min=0)
        self.model.set_param_hint("phi", min=0)
        return pars

    def output_range(self):
        """Calculate the power output range within the polarizer angle range.

        Returns
        -------
        output_range : list, len 2
            ``[min power, max power]``.
        """
        params = self.get_model()
        # the next two lines do not seem to make sense at all
        # phi_max = 180/8  # =22,5°; period 90°
        # phi_min = 3/8*180
        phi_period = 90
        phi_max = params["phi"] - 180 / 8
        phi_min = params["phi"] + 180 / 8
        phi_range = [
            self.analysis_parameters["min"],
            self.analysis_parameters["max"],
        ]
        ic(phi_min)
        ic(phi_max)
        ic(phi_range)
        # check whether maximum is between the angle range
        next_maxphi_from_min = (
            ((phi_range[0] - phi_max) // phi_period) + 1
        ) * phi_period + phi_max
        next_minphi_from_min = (
            ((phi_range[0] - phi_min) // phi_period) + 1
        ) * phi_period + phi_min
        ic(next_minphi_from_min)
        ic(next_maxphi_from_min)
        output_range = [0, 0]
        if next_maxphi_from_min < phi_range[1]:
            output_range[1] = params["bkg"] + params["amp"]
        else:
            output_range[1] = max(
                [
                    self.estimate_power(phi_range[0]),
                    self.estimate_power(phi_range[1]),
                ]
            )
        if next_minphi_from_min < phi_range[1]:
            output_range[0] = params["bkg"]
        else:
            output_range[0] = min(
                [
                    self.estimate_power(phi_range[0]),
                    self.estimate_power(phi_range[1]),
                ]
            )
        return output_range

    def plot(self, fname, xlabel=None, ylabel=None, title=None):
        """Plot the outcome of the analysis.

        Parameters
        ----------
        fname : str
            The file name to save the plot at.
        xlabel, ylabel, title : str, optional
            Axis labels and plot title.
        """
        if xlabel is None:
            xlabel = "angle [deg]"
        # print('plotting with', xlabel, ylabel, title)
        super().plot(fname, xlabel, ylabel, title)


class LinearCurveAnalyzer(AbstractAttenuationCurveAnalyzer):
    """Linear attenuation model.

    The model is ``P(x) = bkg + amp * x``.

    Notes
    -----
    ``P`` is the power and ``x`` the set point. Model parameters: ``bkg``
    (background power) and ``amp`` (amplitude).
    """

    def __init__(self, analysis_parameters):
        """Initialize the analyzer.

        Parameters
        ----------
        analysis_parameters : dict
            Configuration with keys ``min`` (float, minimum control
            parameter) and ``max`` (float, maximum control parameter).
        """
        super().__init__(analysis_parameters)

    def _model_function(self, x, bkg, amp):
        """Evaluate a linear function with background and offset.

        The model is ``P(x) = bkg + amp * x``.

        Parameters
        ----------
        x : float or array
            Input value.
        bkg : float
            Background.
        amp : float
            Amplitude.

        Returns
        -------
        result : float or array
            The output value.
        """
        return bkg + amp * x

    def _model_function_inv(self, y, bkg, amp, mini, maxi):
        """Calculate the inverse."""
        if np.any(y < bkg + amp * mini) or np.any(y > bkg + amp * maxi):
            raise ValueError(
                "Desired value y={:s} out of range. ".format(str(y))
                + "Should be between {:s} and {:s}".format(
                    str(bkg + amp * mini), str(bkg + amp * maxi)
                )
            )
        x = (y - bkg) / amp
        return x

    def _model_function_estinit(self, y, x):
        """Estimate initial parameters for a linear fit.

        Parameters
        ----------
        y : array (N)
            The result data.
        x : array (N)
            The input data.

        Returns
        -------
        pars : dict
            Keys: bkg, amp.
        """
        pars = {
            "bkg": np.min(y),
            "amp": np.max(y) - np.min(y),
        }
        self.model.make_params(pars)
        self.model.set_param_hint("bkg", min=0)
        self.model.set_param_hint("amp", min=0)
        return pars

    def output_range(self):
        p = self.curr_params
        v0 = p["bkg"] + p["amp"] * self.analysis_parameters["min"]
        v1 = p["bkg"] + p["amp"] * self.analysis_parameters["max"]
        return sorted([v0, v1])

    def plot(self, fname, xlabel=None, ylabel=None, title=None):
        """Plot the outcome of the analysis.

        Parameters
        ----------
        fname : str
            The file name to save the plot at.
        xlabel, ylabel, title : str, optional
            Axis labels and plot title.
        """
        if xlabel is None:
            xlabel = "x"
        print("plotting with", xlabel, ylabel, title)
        super().plot(fname, xlabel, ylabel, title)


class PointCurveAnalyzer(AbstractAttenuationCurveAnalyzer):
    """Point analyzer returning a single calibrated value.

    Use this when no attenuator is connected; it always returns the one
    calibrated value. The model is ``P(x) = amp``.
    """

    def __init__(self, analysis_parameters):
        analysis_parameters["min"] = np.nan
        analysis_parameters["min"] = np.nan
        super().__init__(analysis_parameters)

    def fit(self, x, y):
        """Fit a model from calibration data.

        Parameters
        ----------
        x : numeric, same shape as y
            For a PointCurveAnalyzer the x value does not make sense and
            is ignored. It is only kept for consistency of use of the
            different analyzers.
        y : scalar or 1d array
            Desired power output.
        """
        if isinstance(y, Iterable):
            y = np.mean(y)
        self.curr_params = {"amp": y}

    def output_range(self):
        return [self.curr_params["amp"], self.curr_params["amp"]]

    def estimate(self, y):
        """Estimate control parameter needed to reach a given power.

        For the PointCurveAnalyzer there is no relevant control
        parameter, so return zeros.

        Parameters
        ----------
        y : scalar or 1d array
            Desired power output.

        Returns
        -------
        x : numeric, same shape as y
            The control parameters (e.g. angle) corresponding to y
            using the current model.
        """
        if isinstance(y, Iterable):
            x = np.zeros_like(y)
        else:
            x = 0
        return x

    def estimate_power(self, x):
        """Estimate power for a given control parameter.

        Parameters
        ----------
        x : scalar or 1d array
            Angular value.

        Returns
        -------
        y : numeric, same shape as x
            Estimated power output.
        """
        if isinstance(x, Iterable):
            return self.curr_params["amp"] * np.ones_like(x)
        else:
            return self.curr_params["amp"]

    def plot(self, fname, xlabel=None, ylabel=None, title=None):
        logger.debug("PointCurveAnalyzer does not plot.")

    def _model_function(self, x, bkg, amp):
        """Evaluate a constant function.

        The model is ``P(x) = amp``.

        Parameters
        ----------
        x : float or array
            Input value.
        amp : float
            Amplitude.

        Returns
        -------
        result : float or array
            The output value.
        """
        if isinstance(x, Iterable):
            return amp + np.ones_like(x)
        else:
            return amp

    def _model_function_inv(self, y, amp):
        """Calculate the inverse."""
        return y

    def _model_function_estinit(self, y, x):
        """Estimate initial parameters for a constant fit.

        Parameters
        ----------
        y : array (N)
            The result data.
        x : array (N)
            The input data.

        Returns
        -------
        pars : dict
            Keys: bkg, amp.
        """
        pars = {
            "amp": np.mean(y),
        }
        return pars


class PolynomAttenuationCurveAnalyzer(AbstractAttenuationCurveAnalyzer):
    """Polynomial attenuation model.

    The model is
    ``P(ap) = pn[0] * ap**deg + pn[1] * ap**(deg-1) + ... + pn[deg]``.

    Notes
    -----
    ``P`` is the power and ``ap`` / ``x`` the power setting of the AOTF
    (input value). Model parameters: ``pn`` (polynomial fit parameters,
    array of length ``deg + 1``) and ``deg`` (degree of polynomial fit).
    """

    def __init__(self, analysis_parameters):
        """Initialize the analyzer.

        Parameters
        ----------
        analysis_parameters : dict
            Configuration with keys ``min`` (float, minimum control
            parameter such as angle), ``max`` (float, maximum control
            parameter) and ``deg`` (int, degree of polynomial fitting).
        """
        self.poly = None
        self.polinv = None
        if "polydegree" not in analysis_parameters:
            analysis_parameters["polydegree"] = 10
        super().__init__(analysis_parameters)

    def _model_function(self, x, pn):
        """Evaluate the polynomial model at x.

        Parameters
        ----------
        x : float or array
            Input variable, AOTF power [dB].
        pn : array of float
            Polynomial parameters.

        Returns
        -------
        result : float or array
            The evaluation of the polynomial at x.
        """
        if self.poly is None:
            self.poly = _Polynomial(pn)
        y = self.poly(x)
        return y

    def _model_function_inv(self, y, pn, mini, maxi):
        """Calculate the inverse."""
        if self.polinv is not None:
            x = self.polinv(y)
        else:
            raise ValueError(
                "At this point the inverse Polznomial should be defined."
            )
            x = _Polynomial(pn)(y)
        if x < mini:
            x = mini
        elif x > maxi:
            x = maxi
        return x

    def _model_function_estinit(self, y, x):
        """Estimate initial parameters for a polynomial fit.

        Parameters
        ----------
        y : array (N)
            The result data.
        x : array (N)
            The angular data.

        Returns
        -------
        pars : dict
            Keys: bkg, amp, phi.

        Raises
        ------
        NotImplementedError
            Always; estimation is not implemented for this analyzer.
        """
        raise NotImplementedError()

    def output_range(self):
        """Calculate the power output range within the input parameter range.

        Returns
        -------
        output_range : list, len 2
            ``[min power, max power]``.
        """
        end_vals = [
            np.real(self.poly(self.analysis_parameters["min"])),
            np.real(self.poly(self.analysis_parameters["max"])),
        ]
        extremes = self.poly.deriv().roots()
        extremes = [
            e
            for e in extremes
            if (
                np.isreal(e)
                and e > self.analysis_parameters["min"]
                and e < self.analysis_parameters["max"]
            )
        ]
        extreme_vals = [np.real(self.poly(e)) for e in extremes]
        output_range = [
            min(end_vals + extreme_vals),
            max(end_vals + extreme_vals),
        ]
        return output_range

    def fit(self, x, y):
        """Fit a model from calibration data.

        Crop to the x-range and between maxima to make the relationship
        bijective.

        Parameters
        ----------
        x : numeric, same shape as y
            The control parameters (e.g. angle) corresponding to y
            using the current model.
        y : scalar or 1d array
            Desired power output.
        """
        inxrange = np.argwhere(
            (x >= self.analysis_parameters["min"])
            & (x <= self.analysis_parameters["max"])
        )
        x = x[inxrange].flatten()
        y = y[inxrange].flatten()
        self.fitvals_forward = {"x": x, "y": y}
        win_x = [min(x), max(x)]
        self.poly = _Polynomial.fit(
            x,
            y,
            self.analysis_parameters["polydegree"],
            window=win_x,
            domain=win_x,
        )
        idxyextremepos = np.argmin(y), np.argmax(y)
        if idxyextremepos[0] < idxyextremepos[1]:
            idx = range(idxyextremepos[0], idxyextremepos[1])
        else:
            idx = range(idxyextremepos[1], idxyextremepos[0])
        x = x[idx]
        y = y[idx]

        self.fitvals_backward = {"x": x, "y": y}
        win_y = [min(y), max(y)]
        self.polinv = _Polynomial.fit(
            y,
            x,
            int(1.5 * self.analysis_parameters["polydegree"]),
            window=win_y,
            domain=win_y,
        )
        self.curr_params = self.coef2params(self.poly.coef, self.polinv.coef)

    def estimate(self, y):
        """Estimate control parameter needed to reach a given power.

        Parameters
        ----------
        y : scalar or 1d array
            Desired power output.

        Returns
        -------
        x : numeric, same shape as y
            The control parameters (e.g. angle) corresponding to y
            using the current model.
        """
        coef_fw, coef_bw = self.params2coef(self.curr_params)
        minimax = {
            "mini": self.analysis_parameters["min"],
            "maxi": self.analysis_parameters["max"],
        }
        return self._model_function_inv(y, coef_bw, **minimax)

    def estimate_power(self, x):
        """Estimate power for a given control parameter.

        Parameters
        ----------
        x : scalar or 1d array
            Angular value.

        Returns
        -------
        y : numeric, same shape as x
            Estimated power output.
        """
        if self.poly is not None and x is not None:
            return self.poly(x)
        else:
            return 0

    def get_model(self):
        """Return current model parameters.

        Returns
        -------
        model_parameters : dict
            The model parameters.
        """
        if self.poly is not None:
            return self.coef2params(self.poly.coef, self.polinv.coef)
        else:
            return self.curr_params

    def coef2params(self, coef, coef_inv):
        """Convert the polynomial coefficients to a parameter set.

        Parameters
        ----------
        coef : np array
            The polynomial coefficients.
        coef_inv : np array
            The coefficients of the inverse polynomial.

        Returns
        -------
        params : dict
            The coefficients as a dict, with keys p0, p1, ...
        """
        params = {"p{:d}".format(i): c for i, c in enumerate(coef)}
        for i, c in enumerate(coef_inv):
            params["i{:d}".format(i)] = c
        return params

    def params2coef(self, params):
        """Convert a parameter set to the polynomial coefficients.

        Parameters
        ----------
        params : dict
            The coefficients as a dict, with keys p0, p1, ...

        Returns
        -------
        coef_fw : np array
            The polynomial coefficients.
        coef_bw : np array
            The coefficients of the inverse polynomial.
        """
        # Select exactly the polynomial coefficient keys p0,p1,... / i0,i1,...
        # (a substring test would wrongly match e.g. 'amp'/'phi' from a
        # sinusoidal-model row, then KeyError on 'p0'). Ordered by index.
        p_keys = sorted(
            (k for k in params if re.fullmatch(r"p\d+", str(k))),
            key=lambda k: int(str(k)[1:]),
        )
        i_keys = sorted(
            (k for k in params if re.fullmatch(r"i\d+", str(k))),
            key=lambda k: int(str(k)[1:]),
        )
        if not p_keys:
            raise ValueError(
                "polynomial model parameters (p0, p1, ...) not found in "
                "{}. This calibration was likely made with a different "
                "analysis model (e.g. sinusoidal); recalibrate with the "
                "current model.".format(sorted(map(str, params.keys())))
            )
        coef_fw = np.array([params[k] for k in p_keys], dtype=float)
        coef_fw[np.isnan(coef_fw)] = 0
        coef_bw = np.array([params[k] for k in i_keys], dtype=float)
        coef_bw[np.isnan(coef_bw)] = 0
        return coef_fw, coef_bw

    def load_model(self, model_parameters):
        """Load a model from parameters.

        Parameters
        ----------
        model_parameters : dict
            The model parameters, with keys 'p0', 'p1', ...
        """
        coef_fw, coef_bw = self.params2coef(model_parameters)
        self.poly = _Polynomial(coef_fw)
        self.polinv = _Polynomial(coef_bw)
        self.curr_params = model_parameters

    def plot(self, fname, xlabel=None, ylabel=None, title=None):
        """Plot the outcome of the analysis.

        Parameters
        ----------
        fname : str
            The file name to save the plot at.
        xlabel, ylabel, title : str, optional
            Axis labels and plot title.
        """
        if xlabel is None:
            xlabel = "input variable (e.g. power[dBm])"
        if ylabel is None:
            ylabel = "Beam Power [mW]"
        plt.switch_backend("agg")
        fig, ax = plt.subplots()
        xmock = np.linspace(
            self.analysis_parameters["min"],
            self.analysis_parameters["max"],
            num=50,
        )
        if hasattr(self, "fitvals_forward"):
            ax.plot(
                self.fitvals_forward["x"],
                self.fitvals_forward["y"],
                color="b",
                linestyle="None",
                marker="+",
                label="data used forward",
            )
            ax.plot(
                self.fitvals_backward["x"],
                self.fitvals_backward["y"],
                color="r",
                linestyle="None",
                marker="+",
                label="data used inv",
            )
            ymock = np.linspace(
                np.min(self.fitvals_backward["y"]),
                np.max(self.fitvals_backward["y"]),
                num=50,
            )
        else:
            ymock = np.linspace(
                self.poly(np.min(self.analysis_parameters["min"])),
                self.poly(np.max(self.analysis_parameters["min"])),
                num=50,
            )
        ax.plot(
            xmock,
            self.poly(xmock),
            color="b",
            linestyle="-",
            label="fit forward",
        )
        ax.plot(
            self.polinv(ymock),
            ymock,
            color="r",
            linestyle="-",
            label="fit inverse",
        )
        ax.legend()
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        fig.savefig(fname)
        plt.close(fig)


def _model_residual(name, y, pred):
    """Relative residual (percent) of ``pred`` against ``y`` for a model."""
    y = np.asarray(y, dtype=float)
    pred = np.asarray(pred, dtype=float)
    ok = np.isfinite(y) & np.isfinite(pred) & (y > 0)
    if not ok.any():
        return {
            "model": name,
            "rms_pct": float("inf"),
            "max_pct": float("inf"),
        }
    rel = np.abs(y[ok] - pred[ok]) / y[ok]
    return {
        "model": name,
        "rms_pct": float(np.sqrt(np.mean(rel**2)) * 100.0),
        "max_pct": float(np.max(rel) * 100.0),
    }


def model_spec(name):
    """Map a compare/fit model name to its ``(classpath, extra_init_kwargs)``.

    E.g. ``"sinus"`` ->
    ``("monet.analysis.SinusAttenuationCurveAnalyzer", {})`` and
    ``"poly deg 5"`` ->
    ``("monet.analysis.PolynomAttenuationCurveAnalyzer", {"polydegree": 5})``.
    """
    if name.startswith("poly"):
        deg = int(name.split()[-1])
        return (
            "monet.analysis.PolynomAttenuationCurveAnalyzer",
            {"polydegree": deg},
        )
    return ("monet.analysis.SinusAttenuationCurveAnalyzer", {})


def fit_candidate_models(x, y, analysis_parameters, degrees=(3, 4, 5, 6)):
    """Fit candidate models to a calibration curve; return fitted predictors.

    Fits the sinusoidal model and a least-squares polynomial of each degree in
    ``degrees`` to ``(x, y)`` (angle, measured power).

    Returns
    -------
    list of dict
        ``[{'model', 'predict', 'fit_rms_pct', 'fit_max_pct'}, ...]`` where
        ``predict`` is a callable ``angle -> power`` (vectorized). Models that
        fail to fit are omitted.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    out = []

    try:
        ana = SinusAttenuationCurveAnalyzer(dict(analysis_parameters))
        ana.fit(x, y)
        res = _model_residual("sinus", y, ana.estimate_power(x))
        out.append(
            {
                "model": "sinus",
                "predict": (
                    lambda a, m=ana: np.asarray(
                        m.estimate_power(a), dtype=float
                    )
                ),
                "fit_rms_pct": res["rms_pct"],
                "fit_max_pct": res["max_pct"],
            }
        )
    except Exception as exc:
        logger.debug("fit_candidate_models: sinus fit failed: %s", exc)

    ok = np.isfinite(x) & np.isfinite(y)
    for d in degrees:
        name = "poly deg {:d}".format(d)
        try:
            coef = np.polyfit(x[ok], y[ok], d)
            res = _model_residual(name, y, np.polyval(coef, x))
            out.append(
                {
                    "model": name,
                    "predict": (
                        lambda a, c=coef: np.polyval(
                            c, np.asarray(a, dtype=float)
                        )
                    ),
                    "fit_rms_pct": res["rms_pct"],
                    "fit_max_pct": res["max_pct"],
                }
            )
        except Exception as exc:
            logger.debug("fit_candidate_models: %s failed: %s", name, exc)

    return out


def compare_models(x, y, analysis_parameters, degrees=(3, 4, 5, 6)):
    """Fit candidate models to one calibration curve and rank them by residual.

    Fits the sinusoidal model and a least-squares polynomial of each degree in
    ``degrees`` to ``(x, y)`` (angle, measured power) and returns each one's
    relative residual, best (lowest RMS) first — so a user can pick the model
    that actually describes their attenuator instead of guessing.

    Returns
    -------
    list of dict
        ``[{'model', 'rms_pct', 'max_pct'}, ...]`` sorted by ``rms_pct``.
    """
    fits = fit_candidate_models(x, y, analysis_parameters, degrees)
    results = [
        {
            "model": f["model"],
            "rms_pct": f["fit_rms_pct"],
            "max_pct": f["fit_max_pct"],
        }
        for f in fits
    ]
    results.sort(key=lambda r: r["rms_pct"])
    return results


def test_PolynomAttenuationCurveAnalyzer():
    x = np.arange(21)
    y = np.array(
        [
            1,
            1.5,
            2,
            3,
            4,
            5,
            5.6,
            5.8,
            6,
            6.3,
            6.4,
            6.7,
            8,
            10,
            13,
            14,
            14.5,
            14.6,
            14.4,
            14,
            13,
        ]
    )

    pars = {
        "min": 0.0,
        "max": 22.5,
        "step": 0.1,
        "polydegree": 6,
    }
    paca = PolynomAttenuationCurveAnalyzer(pars)
    paca.fit(x, y)
    paca.plot("testplot.png")
    print("estimating outcomes")
    val = 2
    print(val, paca.estimate_power(val))
    val = 4
    print(val, paca.estimate_power(val))
    val = 10
    print(val, paca.estimate_power(val))
    print("estimating inverse")
    val = 5
    print(val, paca.estimate(val))
    val = 10
    print(val, paca.estimate(val))
    val = 13
    print(val, paca.estimate(val))


if __name__ == "__main__":
    test_PolynomAttenuationCurveAnalyzer()
