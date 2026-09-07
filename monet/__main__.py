#!/usr/bin/env python
"""
monet/__main__.py
~~~~~~~~~~~~~~~~~

:authors: Heinrich Grabmayr, 2022
:copyright: Copyright (c) 2022 Jungmann Lab, MPI of Biochemistry
"""

import cmd
import copy
import logging
import os
import pprint
import traceback
from contextlib import redirect_stdout
from io import StringIO
from logging import handlers

import yaml as _yaml

import monet.io as io
from monet import CONFIGS, CONFIGS_PATH, PROTOCOLS, PROTOCOLS_PATH


# configure logger and log that this shouldn't be done here
def config_logger():
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s -> %(message)s"
    )
    file_handler = handlers.RotatingFileHandler(
        "monet.log", maxBytes=1e6, backupCount=5
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.WARNING)
    logger.addHandler(file_handler)
    # logger.addHandler(stream_handler)


def main():
    """Function called from the command line."""
    import argparse

    # os.chdir(os.path.split(CONFIGS_PATH)[0])

    # Main parser
    parser = argparse.ArgumentParser("monet")
    parser.add_argument(
        "mode",
        type=str,
        help=(
            'mode. One of "set", "adjust", "caliaotf", "calibrate", '
            '"serve", "migrate", or "gui".'
        ),
    )
    parser.add_argument(
        "name",
        type=str,
        nargs="?",
        default=None,
        help=(
            "Microscope Name, as specified in config. Not required for "
            "serve/migrate modes."
        ),
    )
    parser.add_argument(
        "-c",
        "--configs-file",
        type=str,
        required=False,
        default=None,
        help="""
            path to the configurations yaml file.
            - Only for calibration mode""",
    )
    parser.add_argument(
        "-p",
        "--protocol-file",
        type=str,
        required=False,
        default=None,
        help="""
            path to the protocol yaml file (if not supplied, only attenuation
            will be controlled, no laser control).
            - Only for calibration mode""",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help=(
            "Host to bind the server to (serve mode only). Defaults to "
            "loopback: the serve API actuates laser hardware, so it must "
            "not be exposed on a network without authentication. Binding a "
            "non-loopback host is gated on the shared auth helper (A9 / "
            "ADR-001; built in WP-3b, reused here in WP-12a)."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the server to (serve mode only).",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="calibrations.db",
        help="Path to the SQLite database file (serve/migrate mode).",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help="Path to the source Excel database (migrate mode only).",
    )

    # Parse
    args = parser.parse_args()
    if args.mode == "serve":
        import uvicorn

        os.environ["MONET_DB_PATH"] = args.db_path
        from monet.server import app

        uvicorn.run(app, host=args.host, port=args.port)
    elif args.mode == "migrate":
        from monet.migrate import migrate_excel_to_sqlite

        if args.source is None:
            parser.error("migrate mode requires --source argument")
        migrate_excel_to_sqlite(args.source, args.db_path)
    elif args.mode == "calibrate":
        MonetCalibrateInteractive(
            args.name, args.configs_file, args.protocol_file
        ).do_calibrate(args={})
    elif args.mode == "caliaotf":
        MonetCalibrateInteractive(
            args.name, args.configs_file, args.protocol_file
        ).do_calibrate_aotf(args={})
    elif args.mode == "adjust":
        MonetAdjustInteractive(
            args.name, args.configs_file, args.protocol_file
        ).cmdloop()
    elif args.mode == "set":
        MonetSetInteractive(args.name).cmdloop()
    elif args.mode == "gui":
        import sys

        from PyQt6.QtWidgets import QApplication

        from monet.gui import MonetMainWindow

        app = QApplication(sys.argv)
        window = MonetMainWindow(initial_microscope=args.name)
        window.show()
        sys.exit(app.exec())
    else:
        raise KeyError(
            'monet mode has to be one of "set", "calibrate", "serve", '
            '"migrate", or "gui".'
        )


# def monet_interactive(CONFIG):
#     import monet.calibrate as mca
#     config_items = [
#         config['index'],
#         config['analysis']['init_kwargs'],
#     ]
#     config_cmds = [
#         'database',
#         *[*list(item.keys()) for item in config_items]
#         # *list(config['index'].keys()),
#         # *list(config['analysis']['init_kwargs'].keys())
#     ]
#
#     config = CONFIG
#     pc = mca.PowerCalibrator(config)
#     # do a main loop
#     while True:
#         cmd = input('Enter command: ')
#         if 'calibrate' in cmd:
#             pc.calibrate()
#         elif 'set' in cmd:
#             pwr = int(cmd[3:])
#             pc.set_power(pwr)
#         elif 'config' in cmd:
#             subcommands = {
#                 combi.strip().split(' ')[0]: combi.strip().split(' ')[1]
#                 for i, combi in enumerate(cmd.split('--'))
#                 if i>0}
#
#             if len(subcommands,keys()) == 0:
#                 print_help_interactive_config(config_commands)
#                 continue
#             for c, v in subcommands.items():
#                 if c == 'database':
#                     config['database'] = v
#                 cmd = get_most_similar(c, config_commands)
#                 for item in config_items:
#                     if cmd in item.keys():
#                         try:
#                             item[cmd] = float(v)
#                         except Exception:
#                             item[cmd] = v
#                         print('Setting {:s} to '.format(cmd), v)
#                         pp.pprint(config)
#                         break
#         elif cmd.startswith('load'):
#             fname = cmd.strip().split(' ')[1]
#             config = _yaml.full_load(fname)
#             pc = mca.PowerCalibrator(config)
#         elif cmd == 'q' or 'exit' in cmd:
#             return
#         else:
#             print_help_interactive(config_cmds)
#         time.sleep(.2)


class MonetCalibrateInteractive(cmd.Cmd):
    """Command-line interactive power calibration and setting."""

    intro = """Welcome to interactive monet. Here, Microscope
        illumination can be calibrated and set. Two modes are
        available:
            * only modulate the attenuator (e.g. HWP/Polarizator)

            * modulate laser type, laser power and attenuator
        """
    prompt = "(monet calibrate)"
    file = None

    def __init__(self, config_name, configs_file=None, protocol_file=None):
        super().__init__()
        import monet.calibrate as mca

        global CONFIGS, PROTOCOLS

        if configs_file is not None:
            with open(configs_file, "r") as cf:
                CONFIGS = _yaml.full_load(cf)
        try:
            config = CONFIGS[config_name]
        except KeyError as e:
            print(
                "Could not find "
                + config_name
                + " in configurations. Aborting."
            )
            print("All configurations:")
            pp = pprint.PrettyPrinter(indent=2)
            pp.pprint(CONFIGS)
            raise e

        if protocol_file is not None:
            with open(protocol_file, "r") as pf:
                PROTOCOLS = _yaml.full_load(pf)
        try:
            protocol = PROTOCOLS[config_name]
        except KeyError:
            print(
                "Could not find "
                + config_name
                + " in protocols. Not using laser control."
            )
            print("All protocols:")
            pp = pprint.PrettyPrinter(indent=2)
            pp.pprint(PROTOCOLS)
            protocol = None
            # raise e

        if protocol is None:
            self.pc = mca.CalibrationProtocol1D(config)
            self.run_2d = False
        else:
            self.pc = mca.CalibrationProtocol2D(config, protocol)
            self.run_2d = True
        self.config_name = config_name

    def do_calibrate(self, args):
        """Perform a power calibration with the settings as described
        in the configuration.
        """
        self.pc.instrument.attenuator.home()
        if not self.run_2d:
            self.pc.calibrate()
        else:
            self.pc.run_protocol()

    def do_calibrate_aotf(self, args):
        """Calibrate the AOTF settings (especially the frequency)."""
        import monet.aotf_cali as aotf_cali

        attenuator_classpath = self.pc.instrument.config["attenuation"][
            "classpath"
        ]
        if "AOTF" not in attenuator_classpath.upper():
            raise ValueError(
                "Probably, the attenuator is not an AOTF and thus "
                "cannot be calibrated this way."
                + ' No mention of "AOTF" is in {:s}'.format(
                    attenuator_classpath
                )
            )
        aotf_cali.calibrate_all(
            self.pc.instrument, self.pc.protocol, self.pc.powermeter
        )

    def do_set(self, power):
        """Set the power to a specified level.

        Parameters
        ----------
        power : float
            The power to set to [mW].
        """
        if not power:
            print("Please specify a power value.")
        else:
            try:
                print(
                    "Setting power for settings {:s}".format(
                        "\n".join(
                            [
                                str(k) + ": " + str(v)
                                for k, v in self.config["index"].items()
                            ]
                        )
                    )
                )
                self.pc.instrument.power = int(power)
            except ValueError as e:
                print(str(e))

    def do_config(self, line):
        """Change the configuration.

        Notes
        -----
        Format ``--parameter: value``. ``--database`` (str) sets the path
        to the database (ends in .xlsx); other config parameters live in
        'index' or 'analysis/init_kwargs'.
        """
        pp = pprint.PrettyPrinter(indent=2)
        try:
            commands = line.split("--")[1:]
            kwargs = {
                cmd.split(":")[0].strip(): cmd.split(":")[1].strip()
                for cmd in commands
            }
        except Exception:
            print("please format your commands correctly")
            print("Current Configuration:")
            pp.pprint(self.config)

        if "database" in kwargs.keys():
            self.pc.instrument.config["database"] = kwargs["database"]

        config_items = [
            self.pc.instrument.config["index"],
            self.pc.instrument.config["analysis"]["init_kwargs"],
        ]
        config_cmds = []
        for item in config_items:
            config_cmds = config_cmds + list(item.keys())

        for c, v in kwargs.items():
            cmd = get_most_similar(c, config_cmds)
            for item in config_items:
                if cmd in item.keys():
                    try:
                        item[cmd] = float(v)
                    except Exception:
                        item[cmd] = v
                    print("Setting {:s} to ".format(cmd), v)
                    pp.pprint(self.pc.instrument.config)
                    break

    def help_config(self):
        helplines = [
            "--database : str",
            "   the path to the database (ends in .xlsx)",
        ]
        config_items = [
            self.pc.instrument.config["index"],
            self.pc.instrument.config["analysis"]["init_kwargs"],
        ]
        for it in config_items:
            for k, v in it.items():
                helplines.append("--{:s}".format(k))
                helplines.append(" " * 4 + "currently {:s}".format(str(v)))
        print("\n".join(helplines))
        print("Some Fuzziness is allowed. Matching of single words is ok.")

    def do_rename(self, name):
        """Rename the microscope name for the current settings.

        Parameters
        ----------
        name : str
            The microscope name.
        """
        self.config_name = name.strip()
        self.pc.instrument.config["index"]["name"] = self.config_name

    def do_load_config(self, fname):
        """Load configuration from file.

        Parameters
        ----------
        fname : str
            The file name.
        """
        import monet.calibrate as mca

        with open(fname, "r") as f:
            self.pc.instrument.config = _yaml.full_load(f)
        if not self.run_2d:
            self.pc = mca.CalibrationProtocol1D(self.pc.instrument.config)
        else:
            self.pc = mca.CalibrationProtocol2D(
                self.pc.instrument.config, self.pc.protocol
            )

    def do_load_protocol(self, fname=None):
        """Load protocol from file.

        If no file name is given, load from default protocols.

        Parameters
        ----------
        fname : str
            The file name.
        """
        import monet.calibrate as mca

        if fname is not None:
            with open(fname, "r") as f:
                self.pc.protocol = _yaml.full_load(f)
        else:
            self.pc.protocol = PROTOCOLS[self.config_name]

        if not self.run_2d:
            print(
                "Protocol files are only used in with laser control. "
                "Switching mode."
            )
            self.run_2d = True
        self.pc = mca.CalibrationProtocol2D(
            self.pc.instrument.config, self.pc.protocol
        )

    def do_save_config(self, fname=""):
        """Save configuration to file.

        Parameters
        ----------
        fname : str
            The filename to save the yaml file. If empty, save to the
            location loaded during init in __init__.
        """
        if not fname:
            fname = CONFIGS_PATH

        cfgs = CONFIGS
        cfgs[self.config_name] = copy.deepcopy(self.pc.instrument.config)
        with open(fname, "w") as f:
            _yaml.dump(cfgs, f)

    def do_save_protocol(self, fname=""):
        """Save configuration to file.

        Parameters
        ----------
        fname : str
            The filename to save the yaml file. If empty, save to the
            location loaded during init in __init__.
        """
        if not self.run_2d:
            print(
                "Not in laser modulation mode. No protocol available for "
                "saving."
            )
            return

        if not fname:
            fname = PROTOCOLS_PATH

        prts = PROTOCOLS
        prts[self.config_name] = copy.deepcopy(self.pc.protocol)
        with open(fname, "w") as f:
            _yaml.dump(prts, f)

    # def do_EOF(self, line):
    #     return True
    #
    def do_exit(self, line):
        """Exit the interaction."""
        return True

    def precmd(self, line):
        return line

    def close(self):
        pass


class MonetAdjustInteractive(cmd.Cmd):
    """Command-line interactive power calibration and setting."""

    intro = """Welcome to interactive monet adjust. Here, Microscope
        illumination can be aligned and adjusted. To that end, all configured
        lasers can be switched on and off, laser powers set, and power
        limits queried.
        """
    prompt = "(monet adjust)"
    file = None

    def __init__(self, config_name, configs_file=None, protocol_file=None):
        super().__init__()
        import monet.calibrate as mca

        global CONFIGS, PROTOCOLS

        if configs_file is not None:
            with open(configs_file, "r") as cf:
                CONFIGS = _yaml.full_load(cf)
        try:
            config = CONFIGS[config_name]
        except KeyError as e:
            print(
                "Could not find "
                + config_name
                + " in configurations. Aborting."
            )
            print("All configurations:")
            pp = pprint.PrettyPrinter(indent=2)
            pp.pprint(CONFIGS)
            raise e

        if protocol_file is not None:
            with open(protocol_file, "r") as pf:
                PROTOCOLS = _yaml.full_load(pf)
        try:
            protocol = PROTOCOLS[config_name]
        except KeyError:
            print(
                "Could not find "
                + config_name
                + " in protocols. Not using laser control."
            )
            print("All protocols:")
            pp = pprint.PrettyPrinter(indent=2)
            pp.pprint(PROTOCOLS)
            protocol = None
            # raise e

        if protocol is None:
            self.pc = mca.CalibrationProtocol1D(config)
            self.run_2d = False
        else:
            self.pc = mca.CalibrationProtocol2D(config, protocol)
            self.run_2d = True
        self.config_name = config_name

    def do_laser(self, laser):
        """Set the laser to use, enable it and switch beam path accordingly.

        Parameters
        ----------
        laser : int
            Laser to set to (specified by wavelength in nm).
        """
        if not laser:
            print("Please specify a laser.")
        else:
            laser = int(laser)
            try:
                # print('Setting laser')
                self.pc.instrument.laser = laser
                self.pc.instrument.attenuator.set_wavelength(laser)
            except Exception as e:
                print(str(e))
                print("Available lasers: ", str(self.pc.instrument.laser))
                return
            try:
                self.pc.instrument.laser_enabled = True
            except Exception as e:
                print(str(e))
                return
            try:
                self.pc.instrument.beampath.positions = self.pc.protocol[
                    "beampath"
                ][laser]
            except Exception as e:
                print(str(e))
                return
            self.pc.powermeter.wavelength = int(laser)

    def do_laser_power(self, power):
        """Set the power to a specified level.

        Parameters
        ----------
        power : float
            The power to set to [mW].
        """
        if not power:
            print("Please specify a power value.")
        else:
            try:
                self.pc.instrument.laserpower = int(power)
            except ValueError as e:
                print(str(e))

    def do_min_power(self, line):
        """Query the minimum power of the current laser"""
        laser = self.pc.instrument.lasers[self.pc.instrument.curr_laser]
        print("Minimum power of laser: ", laser.min_power)

    def do_max_power(self, line):
        """Query the maximum power of the current laser"""
        laser = self.pc.instrument.lasers[self.pc.instrument.curr_laser]
        print("Maximum power of laser: ", laser.max_power)

    def do_attenuate(self, pos):
        """Set the attenuation device to a position (float)"""
        if pos.upper() == "HOME":
            self.pc.instrument.attenuator.home()
        else:
            pos = float(pos)
            self.pc.instrument.attenuator.set(pos)

    def do_open(self, line):
        """open shutter and set the correct light path positions"""
        try:
            self.pc.instrument.beampath.positions = self.pc.protocol[
                "beampath"
            ][self.pc.instrument.curr_laser]
        except Exception as e:
            print(str(e))
            return

    def do_close(self, line):
        """close shutter"""
        try:
            self.pc.instrument.beampath.positions = self.pc.protocol[
                "beampath"
            ]["end"]
        except Exception as e:
            print(str(e))
            return

    def do_autoshutter(self, line):
        """set autoshutter"""
        try:
            line = int(line)
        except Exception:
            if line.upper() == "TRUE":
                line = 1
            else:
                line = 0
        if line == 1:
            line = True
        else:
            line = False
        try:
            self.pc.instrument.beampath.objects["shutter"].autoshutter = line
        except Exception as e:
            print(str(e))
            return

    def do_py(self, line):
        """Execute a line of code"""
        line = "print(" + line + ")"
        f = StringIO()
        with redirect_stdout(f):
            try:
                exec(line)
            except Exception as e:
                print(str(e))
                print(traceback.format_exc())
        print(f.getvalue())

    def do_restartdb(self, line):
        """Restart the database with the last entries and save a backup"""
        fname = self.pc.config["database"]
        io.restart_database(fname)

    def do_exit(self, line):
        """Exit the interaction."""
        laser = self.pc.instrument.lasers[self.pc.instrument.curr_laser]
        laser.enabled = False
        return True

    def precmd(self, line):
        return line

    def close(self):
        pass


class MonetSetInteractive(cmd.Cmd):
    """Command-line interactive power setting."""

    intro = """Welcome to interactive monet - set. Here, Microscope
        illumination power can be set if calibrations exist. A powermeter
        does not need to be plugged in.
        By default, a laser is switched off when navigating to a different
        one. Use command 'multi_laser'
        """
    prompt = "(monet set)"
    file = None
    multi_laser_operation = True

    def __init__(self, config_name):
        super().__init__()
        import monet.control as mco
        from monet.util import load_class

        try:
            config = CONFIGS[config_name]
        except KeyError as e:
            print(
                "Could not find "
                + config_name
                + " in configurations. Aborting."
            )
            print("All configurations:")
            pp = pprint.PrettyPrinter(indent=2)
            pp.pprint(CONFIGS)
            raise e

        try:
            protocol = PROTOCOLS[config_name]
        except KeyError:
            print(
                "Could not find "
                + config_name
                + " in protocols. Not using laser control."
            )
            print("All protocols:")
            pp = pprint.PrettyPrinter(indent=2)
            pp.pprint(PROTOCOLS)
            protocol = None
        self.protocol = protocol

        self.instrument = mco.IlluminationLaserControl(
            config, auto_enable_lasers=False
        )
        try:
            self.instrument.load_calibration_database()
        except Exception:
            raise KeyError(
                "Microscope probably not calibrated yet. "
                + "Monet Set only works with an existing calibration."
            )

        # load powermeter if present
        try:
            pwrconfig = config["powermeter"]
            self.powermeter = load_class(
                pwrconfig["classpath"], pwrconfig["init_kwargs"]
            )
            self.use_powermeter = True
        except Exception:
            self.use_powermeter = False
            print("Powermeter not connected. Do not use measure function")
            # print(traceback.format_exc())

        # switch on autoshutter (is switched on in initialization because
        # that is necessary for calibrate and adjust)
        try:
            self.instrument.beampath.objects["shutter"].autoshutter = True
        except Exception:
            pass

        self.config_name = config_name

        self.power_setvalues = {}
        for las in self.instrument.laser:
            self.do_laser(las)
            try:
                self.power_setvalues[las] = 1
                self.power_setvalues[las] = round(self.instrument.power)
            except Exception:
                pass

        # power-setting mode (see do_mode) and feedback controller parameters
        # (see do_feedback / do_feedback_config)
        self.power_mode = "combined"
        self.feedback_kp = 0.85
        self.feedback_ki = 0.15
        self.feedback_tol_pct = 1.0
        self.feedback_max_iter = 20

    def do_multi_laser(self, arg):
        if arg.upper() == "0" or arg.upper() == "FALSE":
            self.multi_laser_operation = False
            print("""
                Switching multi-laser operation off.
                Only one laser is on at a time.""")
        else:
            self.multi_laser_operation = True
            print("""
                Switching multi-laser operation on.
                Explicitly switch lasers off when not using.""")

    def do_laser(self, laser):
        """Activate a laser, and open the beam path for it.

        Switch current laser on with 'ON', switch all on with 'ALLON'.
        Switch current laser off with 'OFF', switch all off with 'ALLOFF'.

        Parameters
        ----------
        laser : str
            The laser to activate (its wavelength in nm). Special values:
            'OFF' turns the current laser off, 'ALLOFF' turns all lasers
            off, 'ON' turns the current laser on and 'ALLON' turns all
            lasers on.
        """
        if not laser:
            if self.instrument.lasers[self.instrument.curr_laser].enabled:
                enbl = "on"
            else:
                enbl = "off"
            pwr = self.instrument.power
            print(
                "Currently active laser ",
                self.instrument.curr_laser,
                "is",
                enbl,
                "and set on ",
                pwr,
                "mW.",
            )
        else:
            if isinstance(laser, str) and laser.upper() == "OFF":
                self.instrument.lasers[self.instrument.curr_laser].enabled = (
                    False
                )
            elif isinstance(laser, str) and laser.upper() == "ALLOFF":
                for las in self.instrument.laser:
                    self.instrument.lasers[las].enabled = False
            elif isinstance(laser, str) and laser.upper() == "ON":
                self.instrument.lasers[self.instrument.curr_laser].enabled = (
                    True
                )
            elif isinstance(laser, str) and laser.upper() == "ALLON":
                for las in self.instrument.laser:
                    self.instrument.lasers[las].enabled = True
            else:
                try:
                    print("Setting laser {:s}.".format(str(laser)))
                    if (
                        self.instrument.curr_laser is not None
                        and self.instrument.curr_laser != laser
                        and self.multi_laser_operation
                    ):
                        if self.instrument.lasers[
                            self.instrument.curr_laser
                        ].enabled:
                            print(
                                f"WARNING: switching to {laser}, but"
                                + f" laser {self.instrument.curr_laser}"
                                + "is still ON!"
                            )
                            logger.debug(
                                f"WARNING: switching to {laser}, but"
                                + f" laser {self.instrument.curr_laser}"
                                + "is still ON!"
                            )
                    elif not self.multi_laser_operation:
                        # switch the current laser off
                        self.instrument.lasers[
                            self.instrument.curr_laser
                        ].enabled = False
                    self.instrument.laser = laser
                    self.instrument.attenuator.set_wavelength(laser)
                    # self.do_open('')
                    # set laser power back to the value for that laser
                    try:
                        self.do_power(
                            self.power_setvalues[self.instrument.curr_laser]
                        )
                    except Exception:
                        pass
                    if self.use_powermeter:
                        self.powermeter.wavelength = int(laser)
                except ValueError as e:
                    print(str(e))

    def do_laser_power(self, power):
        """Set the laser power of the current laser"""
        if not power:
            print(
                "Current laserpower out of laser: ", self.instrument.laserpower
            )
        else:
            try:
                self.instrument.laserpower = int(power)
            except ValueError as e:
                print(str(e))

    def do_mode(self, arg):
        """Get or set the power-setting mode used by 'power' and 'feedback'.

        Parameters
        ----------
        arg : str, optional
            One of 'combined' (adjust laser power and attenuator),
            'fixed_laser' (keep laser power fixed, adjust attenuator) or
            'fixed_attenuator' (keep attenuator fixed, adjust laser power).
            With no argument, the current mode is printed.
        """
        modes = ("combined", "fixed_laser", "fixed_attenuator")
        arg = arg.strip()
        if not arg:
            print("Current power mode:", self.power_mode)
            print("Available modes:", ", ".join(modes))
            return
        if arg not in modes:
            print(
                "Unknown mode '{:s}'. Choose one of: {:s}".format(
                    arg, ", ".join(modes)
                )
            )
            return
        self.power_mode = arg
        print("Power mode set to", arg)

    def do_range(self, arg):
        """Print the accessible output power range for the current mode."""
        try:
            lo, hi = self.instrument.accessible_power_range(
                self.power_mode, self.instrument.curr_laser
            )
            print(
                "Accessible range ({:s} mode, laser {:s}): "
                "{:.2f} - {:.2f} mW".format(
                    self.power_mode, str(self.instrument.curr_laser), lo, hi
                )
            )
        except Exception as e:
            print("Could not determine power range:", str(e))

    def do_power(self, power):
        """Set the output power [mW], using the current power mode.

        See the 'mode' command for available power modes.

        Parameters
        ----------
        power : float
            The power to set to [mW].
        """
        if not power:
            print("Current power at objective:", self.instrument.power)
            return
        try:
            pwr = int(power)
            laser = self.instrument.curr_laser
            if self.power_mode == "fixed_laser":
                self.instrument.set_power_fixed_laser(pwr, laser)
                print(
                    "Set attenuator for {:d} mW "
                    "(laser power fixed).".format(pwr)
                )
            elif self.power_mode == "fixed_attenuator":
                self.instrument.set_power_fixed_attenuator(pwr, laser)
                print(
                    "Adjusted laser power for {:d} mW "
                    "(attenuator fixed).".format(pwr)
                )
            else:
                print("Setting output power to ", pwr)
                self.instrument.power = pwr
            self.power_setvalues[self.instrument.curr_laser] = pwr
        except ValueError as e:
            print(str(e))

    def do_feedback(self, arg):
        """Closed-loop power setting.

        Iteratively measure with the powermeter and correct until the
        output power is within tolerance. Requires a powermeter and a fixed
        mode (see 'mode' and 'feedback_config').

        Parameters
        ----------
        arg : str
            '<power> [tolerance_pct]' — target output power [mW] and,
            optionally, the convergence tolerance in percent.
        """
        from monet.control import run_power_feedback
        from monet.util import update_mm_acquisition_comment

        if not self.use_powermeter:
            print("No powermeter is connected. Feedback control unavailable.")
            return
        if self.power_mode == "combined":
            print(
                "Feedback requires mode 'fixed_laser' or 'fixed_attenuator'."
                " Use the 'mode' command to switch."
            )
            return
        parts = arg.split()
        if not parts:
            print('Please specify a target power, e.g. "feedback 10".')
            return
        try:
            pwr = float(parts[0])
        except ValueError:
            print('Could not parse target power "{:s}".'.format(parts[0]))
            return
        tol = self.feedback_tol_pct
        if len(parts) > 1:
            try:
                tol = float(parts[1])
            except ValueError:
                print('Could not parse tolerance "{:s}".'.format(parts[1]))
                return

        laser = self.instrument.curr_laser

        def _progress(iteration, setpoint, measured):
            dev = (abs(measured - pwr) / pwr * 100.0) if pwr > 0 else 0.0
            print(
                "  [feedback] iter {:2d}: setpoint={:.3f}  "
                "measured={:.3f} mW  ({:.1f}% dev)".format(
                    iteration, setpoint, measured, dev
                )
            )

        print(
            "Feedback ({:s} mode): target {:g} mW, tolerance {:g}%".format(
                self.power_mode, pwr, tol
            )
        )
        try:
            result = run_power_feedback(
                self.instrument,
                self.powermeter,
                pwr,
                laser,
                self.power_mode,
                kp=self.feedback_kp,
                ki=self.feedback_ki,
                max_dev_pct=tol,
                max_iter=self.feedback_max_iter,
                progress_callback=_progress,
            )
        except KeyboardInterrupt:
            print("\nFeedback aborted by user.")
            return
        except ValueError as e:
            print(str(e))
            return

        measured = result["measured"]
        dev = (abs(measured - pwr) / pwr * 100.0) if pwr > 0 else 0.0
        print(
            "Target {:g} mW -> measured {:.3f} mW ({:.1f}% deviation) "
            "after {:d} iteration(s).".format(
                pwr, measured, dev, result["iterations"]
            )
        )
        if not result["converged"]:
            print(
                "  Did not converge within {:d} steps.".format(
                    self.feedback_max_iter
                )
            )
        if result["out_of_range"]:
            print("  Attenuator range limit reached.")
        cali_pred = result["cali_pred"]
        if cali_pred is not None and cali_pred > 0:
            cali_dev = (measured - cali_pred) / cali_pred * 100.0
            print(
                "  Calibration deviation: {:+.1f}%  (calibration predicts "
                "{:.3f} mW).".format(cali_dev, cali_pred)
            )
        self.power_setvalues[self.instrument.curr_laser] = pwr

        # Write the measured power into the MicroManager acquisition comment
        try:
            unit = self.powermeter.unit
        except Exception:
            unit = "mW"
        factor = result.get("transmission")
        if factor is not None and factor != 1.0:
            print(
                "  (sample plane; measured {:.3f} {} in BFP x T_obj={:.4f})".format(
                    result.get("measured_raw"), unit, factor
                )
            )
        else:
            print("  (no objective transmission factor applied)")
        mm_err = update_mm_acquisition_comment(
            laser,
            measured,
            unit,
            result["att_pos"],
            result["laser_pwr"],
            raw_power=result.get("measured_raw"),
            transmission=factor,
        )
        if mm_err is not None:
            print("  MicroManager comment update failed:", mm_err)

    def do_feedback_config(self, line):
        """Configure the feedback controller parameters.

        Notes
        -----
        Format ``--parameter: value``, where the parameters are ``--kp``
        (float, proportional gain, default 0.85), ``--ki`` (float, integral
        gain, default 0.15), ``--tol`` (float, convergence tolerance in
        percent, default 1.0) and ``--max_iter`` (int, maximum correction
        iterations, default 20). With no argument, the current settings are
        printed.
        """
        params = {
            "kp": ("feedback_kp", float),
            "ki": ("feedback_ki", float),
            "tol": ("feedback_tol_pct", float),
            "max_iter": ("feedback_max_iter", int),
        }
        line = line.strip()
        if not line:
            print("Feedback controller settings:")
            print("  kp       =", self.feedback_kp)
            print("  ki       =", self.feedback_ki)
            print("  tol      =", self.feedback_tol_pct, "%")
            print("  max_iter =", self.feedback_max_iter)
            return
        try:
            commands = line.split("--")[1:]
            kwargs = {
                c.split(":")[0].strip(): c.split(":")[1].strip()
                for c in commands
            }
        except Exception:
            print("Please format your commands as --parameter: value")
            return
        for key, val in kwargs.items():
            match = get_most_similar(key, list(params.keys()))
            if match is None:
                print('Unknown parameter "{:s}".'.format(key))
                continue
            attr, caster = params[match]
            try:
                setattr(self, attr, caster(val))
                print("Set {:s} to {:s}".format(match, val))
            except ValueError:
                print(
                    'Could not parse value "{:s}" for {:s}.'.format(val, match)
                )

    def do_attenuate(self, pos):
        """Set the attenuation device to a position (float)"""
        if pos.upper() == "HOME":
            self.instrument.attenuator.home()
        else:
            pos = float(pos)
            self.instrument.attenuator.set(pos)

    def do_status(self, line):
        """Display the status of all available laser lines"""
        for lsr in self.instrument.laser:
            if self.instrument.lasers[lsr].enabled:
                enbl = "on"
            else:
                enbl = "off"
            pwr = self.power_setvalues[lsr]
            print("Laser ", lsr, "is", enbl, "and set to ", pwr, "mW.")
        print("Currently active laser: ", self.instrument.curr_laser)
        try:
            print(
                "Current attenuator position: ",
                self.instrument.attenuator.curr_pos(),
            )
        except Exception:
            pass
        try:
            print("Beam path positions:", self.instrument.beampath.positions)
        except Exception:
            pass
        try:
            print(
                "Autoshutter:",
                self.instrument.beampath.objects["shutter"].autoshutter,
            )
        except Exception:
            pass
        print("Power mode:", self.power_mode)
        try:
            lo, hi = self.instrument.accessible_power_range(
                self.power_mode, self.instrument.curr_laser
            )
            print("Accessible power range: {:.2f} - {:.2f} mW".format(lo, hi))
        except Exception:
            pass

    def do_open(self, line):
        """open shutter and set the correct light path positions"""
        try:
            self.instrument.beampath.positions = self.protocol["beampath"][
                self.instrument.curr_laser
            ]
        except Exception as e:
            print(str(e))
            return

    def do_close(self, line):
        """close shutter"""
        try:
            self.instrument.beampath.positions = self.protocol["beampath"][
                "end"
            ]
        except Exception as e:
            print(str(e))
            return

    def do_autoshutter(self, line):
        """set autoshutter"""
        try:
            line = int(line)
        except Exception:
            if line.upper() == "TRUE":
                line = 1
            else:
                line = 0
        if line == 1:
            line = True
        else:
            line = False
        try:
            self.instrument.beampath.objects["shutter"].autoshutter = line
        except Exception as e:
            print(str(e))
            return

    def do_measure(self, averaging):
        """Measure the power and report the deviation from the prediction.

        Measures with the powermeter and reports the deviation from the
        power the calibration predicts.

        Parameters
        ----------
        averaging : int
            The number of measurements to average.
        """
        try:
            averaging = int(averaging)
        except Exception:
            averaging = 10

        from monet import POWER_TAG
        from monet.util import update_mm_acquisition_comment

        if not self.use_powermeter:
            print("No powermeter is connected. Cannot measure.")
            return

        laser = self.instrument.curr_laser
        # Project the raw reading to the sample plane (no-op unless the active
        # calibration used the BFP meter and a transmission factor exists).
        raw = self.powermeter.read(averaging)
        measured = self.instrument.to_sample_plane(raw, laser)
        try:
            factor = self.instrument._bfp_factor(laser)
        except Exception:
            factor = None
        print(POWER_TAG + ": " + str(measured))
        try:
            unit = self.powermeter.unit
        except Exception:
            unit = "mW"
        if factor is not None and factor != 1.0:
            print(
                "  (sample plane; measured {:.3f} {} in BFP x T_obj={:.4f})".format(
                    raw, unit, factor
                )
            )
        else:
            print("  (no objective transmission factor applied)")

        # Calibration deviation: what the calibration predicts vs. measured
        cali_pred = None
        try:
            if self.power_mode == "fixed_attenuator":
                curr_lp = self.instrument.lasers[laser].power
                cali_pred = self.instrument.predict_power_fixed_attenuator(
                    curr_lp, laser
                )
            else:
                cali_pred = self.instrument.power
        except Exception:
            cali_pred = None
        if cali_pred is not None and cali_pred > 0:
            dev = (measured - cali_pred) / cali_pred * 100.0
            print(
                "Calibration deviation: {:+.1f}%  (calibration predicts "
                "{:.3f} {:s}).".format(dev, cali_pred, unit)
            )

        # Write the measured power into the MicroManager acquisition comment
        try:
            att_pos = self.instrument.attenuator.curr_pos()
        except Exception:
            att_pos = None
        try:
            laser_pwr = self.instrument.lasers[laser].power
        except Exception:
            laser_pwr = None
        mm_err = update_mm_acquisition_comment(
            laser,
            measured,
            unit,
            att_pos,
            laser_pwr,
            raw_power=raw,
            transmission=factor,
        )
        if mm_err is not None:
            print("MicroManager comment update failed:", mm_err)

    def do_py(self, line):
        """Execute a line of code"""
        line = "print(" + line + ")"
        f = StringIO()
        with redirect_stdout(f):
            try:
                exec(line)
            except Exception as e:
                print(str(e))
        print(f.getvalue())

    def do_exit(self, line):
        """Exit the interaction."""
        # self.do_laser('off')
        self.close()
        return True

    def precmd(self, line):
        return line

    def close(self):
        print("Switching all lasers off.")
        self.do_laser("off")
        pass


def get_most_similar(input, options):
    equals = [input == opt for opt in options]
    partof = [input in opt for opt in options]

    if any(equals):
        return options[equals.index(True)]
    elif any(partof):
        return options[partof.index(True)]
    else:
        return None


def print_help_interactive(config_commands):
    pp = pprint.PrettyPrinter(indent=2)
    print("Interactive monet.")
    print("Commands:")
    cmd_desc = {
        "calibrate": "Start calibration as per the configuration",
        "set": "set power to a level. Args: Power in mW, float.",
        "config": "alter the configuration",
        "q or exit": "quit",
    }
    pp.pprint(cmd_desc)
    print_help_interactive_config(config_commands)


def print_help_interactive_config(config_commands):
    print('for config commands, use pairs of "--[CMD] [ARG]"')
    print("Commands config (CMD): ")
    print(config_commands)


if __name__ == "__main__":
    config_logger()
    logger = logging.getLogger(__name__)
    logger.debug("start logging")
    main()
