# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019.

# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)

import time
import getpass
import signal
import sys
import platform
import pprint
import subprocess
import time
import threading
import logging
import traceback
from collections import OrderedDict

import config
import log
import paths
import platforms
import version

try:
    import hw_diag_utils
except:
    hw_diag_utils = None
try:
    import rights
except ImportError:
    rights = None

from user_login import UserLogin
from printer_interface import PrinterInterface
from no_subproc_camera_controller import NoSubprocCameraController
from static_and_stored_detect import StaticDetector

if not 'slave_app' in sys.modules:
    try:
        from host_commands_interface import HostCommandsInterface
        from camera_controller import CameraController
        from usb_detect import USBDetector
        from network_detect import NetworkDetector
        from klipper_detect import KlipperDetector
        from local_moonraker_detector import LocalMoonrakerDetector
        from updater import Updater
        from client_scanner import ClientScanner
        from tray_controller import TrayController
        from rights import RightsChecker
        from gpio_uart_detect import GPIOUARTDetector
        from makerware_utils import ConveyorKillWaiter
        from detection_wizard import DetectionWizard
        from plugin_controller import PluginController
    except ImportError as e:
        error = traceback.format_exc()
        print('Error: ' + error)
        try:
            with open(log.EXCEPTIONS_LOG_FILE, "a") as f:
                f.write("\n" + log.form_log_title() + time.ctime() + "\n" + error + "\n")
        except OSError:
            pass


class App:

    LOG_TIMESTAMP_PERIOD = 3600
    BROWSER_OPENING_DELAY = 2
    QUIT_THREAD_JOIN_TIMEOUT = 21
    LOOP_SLEEP_STEPS = 10
    WAIT_FOR_PRINTER_TIMEOUT = 6
    SLAVE_MODE = False
    NUM_THREADS_WARNING_THREASHOLD = 40

    HOST_COMMAND_HACK_PRINTER_ID = {'VID': 'HOST', 'PID': 'HOST', 'SNR': '0'}
    LOOPS_TO_SWITCH_HOST_COMMAND_INTERFACE = 5 # really would need x2+1 to enable/disable

    def __init__(self):
        if not hasattr(self, 'logger'):
            self.logger = log.create_logger(None)
        self.logger.info('Starting 3DPrinterOS client...')
        self.logger.info('Version: ' + version.full_version_string())
        self.logger.info('Platform: ' + platform.platform())
        self.logger.info('Python: ' +  sys.version)
        self.settings = config.get_settings()
        self.printer_profiels = []
        if self.settings['dynamic_gcodes_buffer'] and sys.version_info < (3,7):
            logging.getLogger('GcodesBuffer').warning('Disabling dynamic gcodes buffer due to unsupported Python version')
            self.settings['dynamic_gcodes_buffer'] = False
            config.Config.instance().save_settings(self.settings)
        if self.settings['protocol']['encryption']:
            port_key_name = 'https_port'
        else:
            port_key_name = 'http_port'
        port = self.settings['protocol'].get(port_key_name, '')
        if port:
            port = ":" + str(port)
        self.logger.info('Cloud server: %s%s' % (config.get_settings()['URL'], port))
        self.stop_flag = False
        try:
            self.init_config()
            self.init_basics()
            self.init_adv()
            self.init_main_loop()
        except Exception as e:
            self.logger.exception("Exception in App.init: " + str(e))
            self.quit()

    @log.log_exception
    def init_config(self):
        config.Config.instance().set_app_pointer(self)

    @log.log_exception
    def init_basics(self):
        self.detectors = OrderedDict()
        self.printer_interfaces = []
        self.printer_interface_lock = threading.RLock()
        self.printer_interfaces_ready = False
        self.host_commands_interface = None
        self.detection_wizard = None
        self.virtual_printer_enabled = False
        self.gpio_interface = None
        self.verbose = self.settings.get('verbose')
        self.stop_flag = False
        self.poweroff_flag = False
        self.restart_flag = self.settings['quit']['always_restart']
        self.invalid_usb_infos = []
        self.closing_status = []
        self.os_user_name = getpass.getuser()
        self.logger.info('Running as user: ' + self.os_user_name)
        self.offline_mode = self.settings.get('offline_mode', False)
        self.min_loop_time = self.settings['main_loop_period']
        self.volatile_printer_settings = {}
        self.volatile_printer_settings_lock = threading.RLock()
        self.host_commands_enabled = False
        if not self.settings['keep_print_files']:
            paths.remove_downloaded_files()

    @log.log_exception
    def init_adv(self):
        if rights:
            rights.add_cacert_to_certifi()
        signal.signal(signal.SIGINT, self.intercept_signal)
        signal.signal(signal.SIGTERM, self.intercept_signal)
        try:
            self.plugin_controller = PluginController(self)
        except NameError:
            pass
        try:
            self.rights_checker = RightsChecker(self)
        except NameError:
            pass
        try:
            self.conveyor_kill_waiter = ConveyorKillWaiter(self)
        except NameError:
            pass
        self.user_login = UserLogin(self, retry_in_background=config.get_settings()['pre_login_ui'])
        config.Config.instance().set_profiles(self.user_login.profiles)
        if self.settings['camera']["no_subprocess"]:
            self.camera_controller = NoSubprocCameraController(self)
        else:
            self.camera_controller = CameraController(self)
        self.updater = Updater(self)
        if not self.offline_mode:
            self.updater.start_checks_loop()
        self.client_scanner = ClientScanner(self)
        self.tray_controller = TrayController()
        self.detection_wizard = DetectionWizard(self)
        self.init_user_interfaces()
        # maintain detectors order, so that KlipperDetect can disable USBDetector before it will run detect
        for detector_class in (GPIOUARTDetector, LocalMoonrakerDetector, KlipperDetector, USBDetector, NetworkDetector, StaticDetector):
            self.detectors[detector_class.__name__] = detector_class(self)
        self.logger.info('Detectors: ' + pprint.pformat(self.detectors.keys()))
        if self.settings['web_interface']['browser_opening_on_start'] and self.settings['pre_login_ui']:
            self.open_browser()
        if self.user_login.wait() and self.rights_checker.wait() and self.conveyor_kill_waiter.wait():
            if self.settings['web_interface']['browser_opening_on_start'] and not self.settings['pre_login_ui']:
                self.open_browser()
            self.camera_controller.start_camera_process()
            config.Config.instance().set_profiles(self.user_login.profiles)
            virtual_printer = self.settings['virtual_printer']
            self.virtual_printer_enabled = virtual_printer['enabled']
            self.virtual_printer_usb_info = dict(virtual_printer)
            del self.virtual_printer_usb_info['enabled']
            self.host_commands_enabled = self.settings.get('host_commands')

    def init_main_loop(self):
        try:
            self.main_loop()
        except Exception as e:
            self.logger.exception("Exception in main_loop: " + str(e))
        finally:
            self.quit()

    @staticmethod
    def open_browser(force=False):
        logger = logging.getLogger()
        if force or config.get_settings()['web_interface']['browser_opening_on_start']:
            address = "http://%s:%d" % (config.LOCAL_IP, config.get_settings()['web_interface']['port'])
            logger.info("Opening browser tab %s" % address)
            import webbrowser
            webbrowser.open(address, 2)
        else:
            logger.info("Browser opening prevented by settings")
    
    @property
    def local_ip(self) -> str:
        http_connection = getattr(getattr(self, "user_login", None), "http_connection", None)
        if http_connection:
            return http_connection.local_ip
        return ""

    @property
    def host_id(self) -> str:
        http_connection = getattr(getattr(self, "user_login", None), "http_connection", None)
        if http_connection:
            return http_connection.host_id
        return ""

    @property
    def macaddr(self) -> str:
        http_connection = getattr(getattr(self, "user_login", None), "http_connection", None)
        if http_connection:
            return http_connection.macaddr
        return ""

    def init_user_interfaces(self):
        if self.settings['gpio']['enabled']:
            import gpio_interface
            self.gpio_interface = gpio_interface.GPIOInterface()
        else:
            self.gpio_interface = None
        if self.settings['web_interface']['enabled']:
            import web_interface
            self.web_interface = self.init_user_interface(web_interface.WebInterface)
        if self.settings['qt_interface']['enabled']:
            try:
                import qt_interface
            except ImportError:
                self.logger.error("Error on import of qt_interface - disabling it")
            else:
                self.qt_interface = self.init_user_interface(qt_interface.QtInterface)

    def init_user_interface(self, interface_class):
        interface = interface_class(self)
        interface.start()
        self.logger.debug("Waiting for %s to start..." % str(interface))
        interface.wait()
        self.logger.debug("...interface %s is up and running." % interface_class.__class__.__name__)
        return interface

    def intercept_signal(self, signal_code, frame):
        self.logger.warning("SIGINT or SIGTERM received. Starting exit sequence")
        if self.stop_flag:
            self.quit()
        self.stop_flag = True
        self.restart_flag = False

    def toggle_virtual_printer(self):
        self.virtual_printer_enabled = not self.virtual_printer_enabled
        self.settings['virtual_printer']['enabled'] = self.virtual_printer_enabled
        config.Config.instance().save_settings(self.settings)

    def get_printer_interface(self, index=0):
        with self.printer_interface_lock:
            try:
                if self.printer_interfaces:
                    return self.printer_interfaces[index]
            except (IndexError, AttributeError):
                pass

    def wait_for_printer(self):
        time_left = self.WAIT_FOR_PRINTER_TIMEOUT
        step = 0.01
        while not self.stop_flag and time_left:
            time.sleep(step)
            if self.printer_interfaces_ready:
                return True
            time_left -= step
        return False

    @log.log_exception
    def main_loop(self):
        last_timestamp_time = 0
        host_commands_interface_enabling_weight = -2
        undervoltage_detected = False
        while not self.stop_flag:
            loop_start_time = time.monotonic()
            detected_printers = []
            if self.virtual_printer_enabled:
                detected_printers.append(self.virtual_printer_usb_info)
            conflicts = set()
            if self.detection_wizard and self.detection_wizard.active:
                active_detectors = {}
            else:
                active_detectors = config.get_settings()['active_detectors']
            for detector_name in self.detectors:
                if active_detectors.get(detector_name):
                    if not detector_name in conflicts:
                        printers = self.detectors[detector_name].get_printers_list()
                        if printers:
                            detected_printers.extend(printers)
                            conflicts.update(self.detectors[detector_name].CONFLICTS)
            self.connect_new_printers(detected_printers)
            for pi in self.printer_interfaces:
                if not pi.is_alive():
                    try:
                        pi.close()
                    except:
                        pass
                    self.logger.info('Removing %s from printers list' % pi)
                    with self.printer_interface_lock:
                        self.printer_interfaces.remove(pi)
                elif pi.usb_info not in detected_printers:
                    pi.register_error(99, "Printer no longer detected", is_blocking=True, is_critical=True)
                elif pi.usb_info.get('OFF', False):
                    pi.register_error(98, "Printer had been disabled on the client side", is_blocking=True, is_critical=True)
            if not self.printer_interfaces_ready:
                self.printer_interfaces_ready = True
            if self.gpio_interface:
                self.gpio_interface.check_events(self.printer_interfaces)
            if not undervoltage_detected and hw_diag_utils and hw_diag_utils.undervoltage_detected():
                self.logger.warning("Under-voltage detected! This could cause instability. Please upgrade RPi power supply or disconnect some peripherals such as camera.")
                undervoltage_detected = True
            if self.host_commands_enabled:
                if any((not pi.forced_state for pi in self.printer_interfaces)):
                    if host_commands_interface_enabling_weight > -self.LOOPS_TO_SWITCH_HOST_COMMAND_INTERFACE:
                        host_commands_interface_enabling_weight -= 1
                    if host_commands_interface_enabling_weight < 0:
                        if self.host_commands_interface:
                            self.stop_host_commands_interface()
                else:
                    if host_commands_interface_enabling_weight < self.LOOPS_TO_SWITCH_HOST_COMMAND_INTERFACE:
                        host_commands_interface_enabling_weight += 1
                    else:
                        if not self.host_commands_interface:
                            self.start_host_commands_interface()
            loop_end_time = time.monotonic()
            if loop_end_time > last_timestamp_time + self.LOG_TIMESTAMP_PERIOD:
                self.logger.info(time.strftime("Main loop: %d %b %Y %H:%M:%S", time.localtime()))
                last_timestamp_time = loop_end_time
                self.logger.info("Threads count: " + str(threading.active_count()))
            sleep_time = loop_start_time - loop_end_time + self.min_loop_time
            if sleep_time > 0:
                steps_left = self.LOOP_SLEEP_STEPS
                while steps_left and not self.stop_flag:
                    time.sleep(sleep_time/self.LOOP_SLEEP_STEPS)
                    steps_left -= 1

    def validate_usb_info(self, usb_info):
        if isinstance(usb_info, dict):
            vid = usb_info.get('VID')
            pid = usb_info.get('PID')
            if pid and vid and isinstance(vid, str) and isinstance(pid, str):
                return True
        if not usb_info in self.invalid_usb_infos:
            self.invalid_usb_infos.append(usb_info)
            self.logger.error("Invalid printer info: " + str(usb_info))
        return False

    def connect_new_printers(self, detected_printers):
        currently_connected_usb_info = [pi.usb_info for pi in self.printer_interfaces]
        for usb_info in detected_printers:
            if usb_info not in currently_connected_usb_info:
                if self.validate_usb_info(usb_info) and not usb_info.get('disabled', False):
                    pi = PrinterInterface(self, usb_info,
                                          config.get_settings()['printer_loop_period'],
                                          self.offline_mode)
                    self.logger.info('Adding %s to printers list' % pi)
                    pi.start()
                    currently_connected_usb_info.append(pi.usb_info)
                    with self.printer_interface_lock:
                        self.printer_interfaces.append(pi)

    def close_module(self, state, closing_function, *args):
        self.logger.info(state)
        self.closing_status.append(state)
        closing_function(*args)
        self.closing_status[-1] += 'done'
        time.sleep(0.1)

    def register_error(self, code, message, is_blocking=False, is_info=False, is_critical=False):
        self.logger.warning("Error:N%d in child of app. %s" % (code, message))

    def raise_stop_flag(self):
        self.stop_flag = True

    def set_offline_mode(self, enabled=True):
        with self.printer_interface_lock:
            for pi in self.printer_interfaces:
                pi.offline_mode = enabled
        self.offline_mode = enabled

    def start_host_commands_interface(self):
        self.host_commands_interface = HostCommandsInterface(self, self.HOST_COMMAND_HACK_PRINTER_ID,
                                      config.get_settings()['printer_loop_period'],
                                      self.offline_mode)
        self.host_commands_interface.start()
        self.logger.info("Host commands interface enabled") 

    def stop_host_commands_interface(self):
        if self.host_commands_interface:
            self.host_commands_interface.close()
            self.host_commands_interface.join(self.min_loop_time)
            self.host_commands_interface = None

    @log.log_exception
    def quit(self): # TODO refactor all the closing by creating class Closable and subclasses for each case
        self.logger.info("Starting exit sequence...")
        if hasattr(self, 'client_detector'):
            self.close_module('Closing client detector...', self.client_scanner.close)
        if getattr(self, 'gpio_interface', None):
            self.close_module('Closing GPIO interface...', self.gpio_interface.close)
        printer_interfaces = getattr(self, "printer_interfaces", [])
        self.host_commands_enabled = False
        if self.host_commands_interface:
            self.stop_host_commands_interface()
        for pi in printer_interfaces:
            printer_name = pi.printer_profile.get('name', 'nameless printer')
            state = 'Closing ' + printer_name + ' ' + str(pi) + '...'
            self.close_module(state, pi.close)
        for pi in printer_interfaces:
            printer_name = pi.printer_profile.get('name', 'nameless printer')
            state = 'Joining ' + printer_name + ' ' + str(pi) + '...'
            self.close_module(state, pi.join, self.QUIT_THREAD_JOIN_TIMEOUT)
        if hasattr(self, 'camera_controller'):
            self.close_module('Closing camera...', self.camera_controller.stop_camera_process)
        if hasattr(self, "plugin_controller"):
            self.close_module('Closing Plugins...', self.plugin_controller.close)
        if hasattr(self, 'tray_controller'):
            self.close_module('Closing tray subprocess...', self.tray_controller.close)
        time.sleep(0.2)  # need to reduce logging spam
        if hasattr(self, "web_interface"):
            self.close_module("Closing web server...", self.web_interface.close)
        if hasattr(self, "qt_interface"):
            self.close_module("Closing Qt interface...", self.qt_interface.close)
        # go through all existing threads and try to close lost ones
        for thread in threading.enumerate():
            if thread.is_alive:
                close_method = getattr(thread, "close", None)
                if close_method:
                    close_method()
                else:
                    thread.stop_flag = True
        self.logger.info("...exit sequence finish.")
        if getattr(self, "restart_flag", False):
            self.logger.info("Restart !")
        else:
            self.logger.info("Goodbye ;-)")
        for handler in self.logger.handlers:
            try:
                handler.flush()
            except:
                pass
        if getattr(self, "poweroff_flag", False) and self.settings['quit']['allow_poweroff'] :
            if rights.is_sudo_available():
                if config.get_settings()['quit']['dim_display'] and platforms.get_platform() == 'rpi':
                    try:
                        subprocess.call('sudo bash -c "echo 1 > /sys/class/backlight/rpi_backlight/bl_power"', shell=True)
                    except:
                        pass
                if self.restart_flag:
                    try:
                        subprocess.call(["sudo", "systemctl", "reboot"])
                    except:
                        pass
                else:
                    try:
                        subprocess.call(["sudo", "systemctl", "poweroff"])
                    except:
                        pass
                time.sleep(1)
