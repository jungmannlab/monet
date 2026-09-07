#!/usr/bin/env python
"""
monet/beampath.py
~~~~~~~~~~~~~~~~~

This module provides functionality to control things in the beam path
other than those which are central to the functionality of monet. E.g.
opening shutters or positioning dichroics.

:authors: Heinrich Grabmayr, 2022
:copyright: Copyright (c) 2022 Jungmann Lab, MPI of Biochemistry
"""

import abc
import logging

from icecream import ic

from monet.util import load_class, refresh_mm_gui

# pycromanager is imported lazily inside get_pycromgr() so the rest of the
# package can be used (and tested) on machines without Micro-Manager.
# import pymmcore


logger = logging.getLogger(__name__)
ic.configureOutput(outputFunction=logger.debug)

pycrocore = None
# or load specific config here: https://github.com/micro-manager/pymmcore/


def get_pycromgr(pycore_config=None):
    """Initialize the pycromanager core.

    Uses a saved configuration if supplied, otherwise the default.

    Parameters
    ----------
    pycore_config : None or dict
        If a dict, with keys 'micromanager_path' and 'mmconfig_name'.

    Returns
    -------
    pycrocore : pycromanager.Core
        The global pycromanager core instance.
    """
    global pycrocore
    if pycrocore is not None:
        # logger.debug('Pycromanager Core already initialized. Returning.')
        return pycrocore

    from pycromanager import Core

    if pycore_config is None:
        try:
            pycrocore = Core()
        except TimeoutError as e:
            raise TimeoutError(
                "Timed out connecting to Micro-Manager (pycromanager). "
                "Check that Micro-Manager is running and the Java gateway is "
                "accessible on the expected port."
            ) from e
    else:
        # no need to specifically load the config
        logger.debug(
            "Ignoring pycromanager configuration {:s}.".format(
                str(pycore_config)
            )
        )
        try:
            pycrocore = Core()
        except TimeoutError as e:
            raise TimeoutError(
                "Timed out connecting to Micro-Manager (pycromanager). "
                "Check that Micro-Manager is running and the Java gateway is "
                "accessible on the expected port."
            ) from e
        # pycrocore = pymmcore.CMMCore()
        # pycrocore.setDeviceAdapterSearchPaths(
        #     [pycore_config['micromanager_path']])
        # pycrocore.loadSystemConfiguration(
        #     os.path.join(pycore_config['micromanager_path'],
        #                  pycore_config['mmconfig_name']))

        # logger.debug(pycrocore.getAvailablePropertyBlocks())
        # logger.debug(pycrogore.getChannelGroup())
    return pycrocore


def _config_is_current(core, group, pos):
    """Return True if Micro-Manager config ``group`` is already at ``pos``.

    Used to skip redundant Nikon turret moves, which make the controller
    report error 0xe01004b6. Returns False if the current config cannot be
    queried, so the move is still attempted.
    """
    try:
        return core.get_current_config(group) == pos
    except Exception:
        return False


class BeamPath:
    """Hold all beam-path objects that can be opened or positioned.

    Notes
    -----
    Example config::

        {
            'DC': {
                'classpath': 'monet.beampath.NikonFilterWheel',
                'init_kwargs': {'SN': 1234}},
            'shutter': {
                'classpath': 'monet.beampath.NikonShutter',
                'init_kwargs': {'SN': 123456}},
        }
    """

    def __init__(self, config, pycore_config=None):
        """Initialize the beam path.

        Parameters
        ----------
        config : dict
            Keys are BeamPathObject identifiers, as used in the protocol.
        pycore_config : dict
            Keys 'micromanager_path' and 'mmconfig_name'.
        """
        # Only initialise the Micro-Manager core eagerly when an explicit
        # configuration is supplied. The real (Nikon*) beam-path objects each
        # call get_pycromgr() lazily in their own __init__, so a beam path
        # made up only of test objects needs no Micro-Manager / pycromanager.
        if pycore_config is not None:
            get_pycromgr(pycore_config)
        self.objects = {
            obid: load_class(cfg["classpath"], cfg["init_kwargs"])
            for obid, cfg in config.items()
        }

    @property
    def positions(self):
        """Query the positions of the beam path objects.

        Returns
        -------
        positions : dict
            Keys are object ids as in self.objects.
        """
        return {obid: obj.position for obid, obj in self.objects.items()}

    @positions.setter
    def positions(self, positions):
        """Set the position of beam path objects.

        Each object is set independently: a hardware error on one object
        (e.g. a Nikon filter turret the controller rejects) does not stop
        the others from being set. In particular the shutter is still
        commanded even if a filter/nosepiece move fails, so closing the
        beam path keeps working. Any errors are collected and re-raised
        together once every object has been attempted.

        Parameters
        ----------
        positions : dict
            Keys are object ids as in self.objects; values are position
            values compatible with the respective object.
        """
        errors = []
        for obid, pos in positions.items():
            try:
                self.objects[obid].position = pos
            except Exception as exc:
                logger.exception(
                    "Could not set beam-path object %r to %r", obid, pos
                )
                errors.append("{!r}->{!r}: {}".format(obid, pos, exc))
        # Changes issued through the Core are invisible in the MicroManager
        # main window until its GUI is refreshed.
        refresh_mm_gui()
        if errors:
            raise RuntimeError(
                "Failed to set beam-path object(s): " + "; ".join(errors)
            )


class AbstractBeamPathObject(abc.ABC):
    """The prototypic beam path object, with standard methods."""

    _position = None

    def __init__(self, config):
        self._position = 0
        self._autoshutter = True
        pass

    @property
    @abc.abstractmethod
    def position(self):
        """Get the position of the beam path object."""
        return self._position

    @position.setter
    @abc.abstractmethod
    def position(self, pos):
        """Set the position of the beam path object"""
        self._position = pos


class TestShutter(AbstractBeamPathObject):
    """Implments a test shutter."""

    def __init__(self, config):
        """Initialize the shutter.

        Parameters
        ----------
        config : dict
            The configuration of the shutter, e.g. with key 'SN' (serial
            number).
        """
        super().__init__(config)
        logger.debug("initializing TestShutter")
        self.device = self._connect(config)
        self._autoshutter = True

    @property
    def autoshutter(self):
        """Get whether the shutter is on autoshutter."""
        return self._autoshutter

    @autoshutter.setter
    def autoshutter(self, pos):
        """Set the autoshutter state"""
        self._autoshutter = pos

    def _connect(self, config):
        device = None
        logger.debug("connecting to TestShutter")
        return device

    @property
    def position(self):
        logger.debug("querying position of TestShutter.")
        return super().position

    @position.setter
    def position(self, pos):
        if not isinstance(pos, bool):
            raise ValueError(
                "TestShutter position must be bool, got {!r}".format(pos)
            )
        logger.debug("setting position of TestShutter to {:b}".format(pos))
        # Mirror NikonShutter: autoshutter is off while the shutter is held
        # open, and back on once it is closed again.
        self._autoshutter = not pos
        super(self.__class__, self.__class__).position.__set__(self, pos)


class NikonShutter(AbstractBeamPathObject):
    """Implments the shutter of a Nikon Ti2 Microscope."""

    def __init__(self, config):
        """Initialize the shutter.

        Parameters
        ----------
        config : dict
            The configuration of the shutter, e.g. with key 'SN' (serial
            number).
        """
        super().__init__(config)
        self._connect(config)

    def _connect(self, config):
        self.core = get_pycromgr()
        # Leave MicroManager's autoshutter on so the microscope is ready for
        # normal imaging. monet switches it off only transiently while it
        # holds the shutter open itself (see the position setter).
        self.core.set_property("Core", "AutoShutter", "1")

    @property
    def autoshutter(self):
        return self.core.get_property("Core", "AutoShutter")

    @autoshutter.setter
    def autoshutter(self, val):
        if val:
            val = "1"
        else:
            val = "0"
        self.core.set_property("Core", "AutoShutter", val)

    @property
    def position(self):
        # Query the hardware rather than returning the cached value, so the
        # state is correct also when the shutter was operated elsewhere
        # (e.g. in the MicroManager GUI).
        try:
            return bool(self.core.get_shutter_open())
        except Exception:
            return super().position

    @position.setter
    def position(self, pos):
        if not isinstance(pos, bool):
            raise ValueError(
                "NikonShutter position must be bool, got {!r}".format(pos)
            )
        # Autoshutter normally stays on so the microscope drives the shutter
        # around acquisitions. To hold the shutter open ourselves we must
        # switch it off first (otherwise MicroManager would re-close it), and
        # hand control back once we close the shutter again.
        if pos:
            self.autoshutter = False
            self.core.set_shutter_open(True)
        else:
            self.core.set_shutter_open(False)
            self.autoshutter = True
        super(self.__class__, self.__class__).position.__set__(self, pos)


class NikonFilterWheel(AbstractBeamPathObject):
    """Implments the filter wheel of a Nikon Ti2 Microscope."""

    def __init__(self, config):
        """Initialize the filter wheel.

        Parameters
        ----------
        config : dict
            The configuration of the filter wheel, e.g. with key 'SN'
            (serial number).
        """
        super().__init__(config)
        self._connect(config)

    def _connect(self, config):
        self.core = get_pycromgr()
        # find the correct filter config name
        filter_config_name = "Filter turret"
        cfg_groups = self.core.get_available_config_groups()
        config_names = [cfg_groups.get(i) for i in range(cfg_groups.size())]
        if filter_config_name not in config_names:
            config_names_upper = [it.upper() for it in config_names]
            if filter_config_name.upper() in config_names_upper:
                filter_config_name = config_names[
                    config_names_upper.index(filter_config_name.upper())
                ]
            else:
                # try the parts
                name_candidates = []
                for test_cn in filter_config_name.split(" "):
                    found = [
                        test_cn.upper() in cn for cn in config_names_upper
                    ]
                    if sum(found) > 0:
                        name_candidates.append(config_names[found.index(True)])
                if len(name_candidates) == 1:
                    filter_config_name = name_candidates[0]
                elif len(name_candidates) > 1:
                    logger.debug(
                        "Multiple configs could be the "
                        + filter_config_name
                        + ": "
                        + ", ".join(name_candidates)
                        + ". Choosing the first."
                    )
                    filter_config_name = name_candidates[0]
                else:
                    raise KeyError(
                        "Cannot find Micro-Manager configuration group for "
                        "{!r}. Available groups: {}".format(
                            filter_config_name, config_names
                        )
                    )
        self.filter_config_name = filter_config_name
        # load the options
        configopts = self.core.get_available_configs(filter_config_name)
        self.filter_options = [
            configopts.get(i) for i in range(configopts.size())
        ]

    @property
    def position(self):
        curr_pos = self.core.get_current_config(self.filter_config_name)
        return curr_pos
        return super().position

    @position.setter
    def position(self, pos):
        if not isinstance(pos, str):
            raise ValueError(
                "MMConfigFilter position must be str, got {!r}".format(pos)
            )
        if pos not in self.filter_options:
            raise ValueError(
                "Position {!r} not available for {}. Options: {}".format(
                    pos, self.filter_config_name, self.filter_options
                )
            )
        # Re-selecting the filter block's *current* position makes the
        # Nikon Ti controller report an error (0xe01004b6), so only issue
        # the move when the requested position actually differs.
        if not _config_is_current(self.core, self.filter_config_name, pos):
            self.core.set_config(self.filter_config_name, pos)

        super(self.__class__, self.__class__).position.__set__(self, pos)


class NikonNosepiece(AbstractBeamPathObject):
    """Implments the Nosepiece / objective turret of a Nikon Ti2 Microscope."""

    def __init__(self, config):
        """Initialize the nosepiece.

        Parameters
        ----------
        config : dict
            The configuration of the nosepiece, e.g. with key 'SN' (serial
            number).
        """
        super().__init__(config)
        self._connect(config)

    def _connect(self, config):
        self.core = get_pycromgr()
        # find the correct filter config name
        filter_config_name = "Nosepiece"
        cfg_groups = self.core.get_available_config_groups()
        config_names = [cfg_groups.get(i) for i in range(cfg_groups.size())]
        if filter_config_name not in config_names:
            config_names_upper = [it.upper() for it in config_names]
            if filter_config_name.upper() in config_names_upper:
                filter_config_name = config_names[
                    config_names_upper.index(filter_config_name.upper())
                ]
            else:
                # try the parts
                name_candidates = []
                for test_cn in filter_config_name.split(" "):
                    found = [
                        test_cn.upper() in cn for cn in config_names_upper
                    ]
                    if sum(found) > 0:
                        name_candidates.append(config_names[found.index(True)])
                if len(name_candidates) == 1:
                    filter_config_name = name_candidates[0]
                elif len(name_candidates) > 1:
                    logger.debug(
                        "Multiple configs could be the "
                        + filter_config_name
                        + ": "
                        + ", ".join(name_candidates)
                        + ". Choosing the first."
                    )
                    filter_config_name = name_candidates[0]
                else:
                    raise KeyError(
                        "Cannot find Micro-Manager configuration group for "
                        "{!r}. Available groups: {}".format(
                            filter_config_name, config_names
                        )
                    )
        self.filter_config_name = filter_config_name
        # load the options
        configopts = self.core.get_available_configs(filter_config_name)
        self.filter_options = [
            configopts.get(i) for i in range(configopts.size())
        ]

    @property
    def position(self):
        curr_pos = self.core.get_current_config(self.filter_config_name)
        return curr_pos
        return super().position

    @position.setter
    def position(self, pos):
        if not isinstance(pos, str):
            raise ValueError(
                "NikonNosepiece position must be str, got {!r}".format(pos)
            )
        if pos not in self.filter_options:
            raise ValueError(
                "Position {!r} not available for {}. Options: {}".format(
                    pos, self.filter_config_name, self.filter_options
                )
            )
        # Avoid re-selecting the current position (see NikonFilterWheel):
        # the Nikon controller errors (0xe01004b6) on a redundant move.
        if not _config_is_current(self.core, self.filter_config_name, pos):
            self.core.set_config(self.filter_config_name, pos)

        super(self.__class__, self.__class__).position.__set__(self, pos)
