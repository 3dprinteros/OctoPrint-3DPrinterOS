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
import os
import getpass
import signal
import sys
import platform
import pprint
import subprocess
import time
import threading
import logging
from collections import OrderedDict

import paths
import config
import log
try:
    import rights
except ImportError:
    rights = None
import platforms
import version

try:
    from user_login import UserLogin
    from printer_interface import PrinterInterface
    from camera_controller import CameraController
    from no_subproc_camera_controller import NoSubprocCameraController
    from static_and_stored_detect import StaticDetector
    from usb_detect import USBDetector
    from network_detect import NetworkDetector
    from klipper_detect import KlipperDetector
    #from win_driver_detector import WinDriverDetector
    from updater import Updater
    from client_scanner import ClientScanner
    from tray_controller import TrayController
    from rights import RightsChecker
    from gpio_uart_detect import GPIOUARTDetector
    from makerware_utils import ConveyorKillWaiter
    from detection_wizard import DetectionWizard
    from plugin_controller import PluginController
except ImportError:
    pass

class App:

    LOG_TIMESTAMP_PERIOD = 3600
    BROWSER_OPENING_DELAY = 2
    QUIT_THREAD_JOIN_TIMEOUT = 21
    LOOP_SLEEP_STEPS = 10
    WAIT_FOR_PRINTER_TIMEOUT = 6
    SLAVE_MODE = False

    def __init__(self):
        self.logger = log.create_logger(None)
        self.logger.info('Starting 3DPrinterOS client...')
        self.logger.info('Version: ' + version.full_version_string())
        self.logger.info('Platform: ' + platform.platform())
        self.logger.info('Python: ' +  sys.version)
        if config.get_settings()['protocol']['encryption']:
            port_key_name = 'https_port'
        else:
            port_key_name = 'http_port'
        port = config.get_settings()['protocol'].get(port_key_name, '')
        if port:
            port = ":" + str(port)
        self.logger.info('Server: %s%s' % (config.get_settings()['URL'], port))
        try:
            self.init_config()
            self.init_basics()
            self.init_adv()
        except:
            self.logger.exception("Exception in App.init:")
        finally:
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
        self.new_printers_detection_wizard = False
        self.virtual_printer_enabled = False
        self.gpio_interface = None
        self.stop_flag = False
        self.poweroff_flag = False
        self.restart_flag = False
        self.invalid_usb_infos = []
        self.closing_status = []
        self.local_ip = ""
        self.host_id = ""
        self.macaddr = ""
        self.os_user_name = getpass.getuser()
        self.offline_mode = config.get_settings().get('offline_mode', False)
        self.min_loop_time = config.get_settings()['main_loop_period']
        if not config.get_settings()['keep_print_files']:
            paths.remove_downloaded_files()

    @log.log_exception
    def init_adv(self):
        if rights:
            rights.add_cacert_to_certifi()
        signal.signal(signal.SIGINT, self.intercept_signal)
        signal.signal(signal.SIGTERM, self.intercept_signal)
        self.plugin_controller = PluginController(self)
        self.rights_checker = RightsChecker(self)
        self.conveyor_kill_waiter = ConveyorKillWaiter(self)
        self.user_login = UserLogin(self, retry_in_background=config.get_settings()['pre_login_ui'])
        self.detection_wizard = DetectionWizard(self)
        self.init_user_interfaces()
        self.wait_and_open_browser()
        self.client_scanner = ClientScanner(self)
        for detector_class in (GPIOUARTDetector, KlipperDetector, USBDetector, NetworkDetector): #maintain order KlipperDetect should disable usb before it produce output
            self.detectors[detector_class.__name__] = detector_class(self)
        self.logger.info('Detectors: ' + pprint.pformat(self.detectors.keys()))
        if self.user_login.wait() and self.rights_checker.wait() and self.conveyor_kill_waiter.wait():
            http_connection = getattr(self.user_login, "http_connection", None)
            if http_connection:
                self.local_ip = http_connection.local_ip
                self.host_id = http_connection.host_id
                self.macaddr = http_connection.macaddr
                self.logger.info(f'IP:{self.local_ip} HostID:{self.host_id} MACAddr:{self.macaddr}')
            config.Config.instance().set_profiles(self.user_login.profiles)
            self.virtual_printer_enabled = config.get_settings()['virtual_printer']['enabled']
            self.virtual_printer_usb_info = dict(config.get_settings()['virtual_printer'])
            del self.virtual_printer_usb_info['enabled']
            self.camera_controller = CameraController(self)
            self.tray_controller = TrayController()
            self.updater = Updater(self)
            if not self.offline_mode:
                self.updater.start_checks_loop()
            self.main_loop()

    @staticmethod
    def open_browser(start=False):
        import logging 
        logger = logging.getLogger()
        if start and not config.get_settings()['web_interface']['browser_opening_on_start']:
            logger.info("Browser opening prevented by settings")
            return 
        address = "http://%s:%d" % (config.LOCAL_IP, config.get_settings()['web_interface']['port'])
        logger.info("Opening browser tab %s" % address)
        import webbrowser
        webbrowser.open(address, 2)

    def wait_and_open_browser(self):
        if config.get_settings()['web_interface']['browser_opening_on_start']:
            self.logger.info("Waiting for timeout or login to open browser...")
            start = time.monotonic()
            while time.monotonic() < start + self.BROWSER_OPENING_DELAY and not self.user_login.user_token:
                time.sleep(0.01)
            self.logger.info("...done")
            self.open_browser()

    def init_user_interfaces(self):
        if config.get_settings()['gpio']['enabled']:
            import gpio_interface 
            self.gpio_interface = gpio_interface.GPIOInterface()
        else:
            self.gpio_interface = None
        if config.get_settings()['web_interface']['enabled']:
            import web_interface 
            self.web_interface = self.init_user_interface(web_interface.WebInterface)
        if config.get_settings()['qt_interface']['enabled']:
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

    def toggle_virtual_printer(self):
        self.virtual_printer_enabled = not self.virtual_printer_enabled
        settings = config.get_settings()
        settings['virtual_printer']['enabled'] = self.virtual_printer_enabled
        config.Config.instance().save_settings(settings)

    def get_printer_interface(self, index=0):
        with self.printer_interface_lock:
            try:
                if self.printer_interface_lock:
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
        while not self.stop_flag:
            loop_start_time = time.monotonic()
            detected_printers = []
            if self.virtual_printer_enabled:
                detected_printers.append(self.virtual_printer_usb_info)
            conflicts = set()
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
                if pi.usb_info not in detected_printers:
                    if not pi.forced_state == "error":
                        pi.register_error(99, "Printer no longer detected", is_blocking=True)
                if not pi.is_alive():
                    try:
                        pi.close()
                    except:
                        pass
                    self.logger.info('Removing %s from printers list' % pi)
                    with self.printer_interface_lock:
                        self.printer_interfaces.remove(pi)
            if not self.printer_interfaces_ready:
                self.printer_interfaces_ready = True
            if self.gpio_interface:
                self.gpio_interface.check_events(self.printer_interfaces)
            loop_end_time = time.monotonic()
            if loop_end_time > last_timestamp_time + self.LOG_TIMESTAMP_PERIOD:
                self.logger.info(time.strftime("Main loop: %d %b %Y %H:%M:%S", time.localtime()))
                last_timestamp_time = loop_end_time
            sleep_time = loop_end_time - loop_start_time + self.min_loop_time
            if sleep_time > 0:
                steps_left = self.LOOP_SLEEP_STEPS
                while steps_left and not self.stop_flag:
                    time.sleep(sleep_time/self.LOOP_SLEEP_STEPS)
                    steps_left -= 1

    def validate_usb_info(self, usb_info):
        if isinstance(usb_info, dict):
            vid = usb_info.get('VID')
            pid = usb_info.get('VID')
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
                if self.validate_usb_info(usb_info):
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

    def register_error(self, code, message, is_blocking=False, is_info=False):
        self.logger.warning("Error:N%d in child of app. %s" % (code, message))

    def raise_stop_flag(self):
        self.stop_flag = True

    def set_offline_mode(self, enabled=True):
        if enabled:
            self.user_login.init_profiles()
        with self.printer_interface_lock:
            for pi in self.printer_interfaces:
                pi.offline_mode = enabled
        self.offline_mode = enabled

    @log.log_exception
    def quit(self): # TODO refactor all the closing by creating class Closable and subclasses for each case
        self.logger.info("Starting exit sequence...")
        if hasattr(self, 'client_detector'):
            self.close_module('Closing client detector...', self.client_scanner.close)
        if hasattr(self, 'camera_controller'):
            self.close_module('Closing camera...', self.camera_controller.stop_camera_process)
        if getattr(self, 'gpio_interface', None):
            self.close_module('Closing GPIO interface...', self.gpio_interface.close)
        printer_interfaces = getattr(self, "printer_interfaces", [])
        for pi in printer_interfaces:
            printer_name = pi.printer_profile.get('name', 'nameless printer')
            state = 'Closing ' + printer_name + ' ' + str(pi) + '...'
            self.close_module(state, pi.close)
        for pi in printer_interfaces:
            printer_name = pi.printer_profile.get('name', 'nameless printer')
            state = 'Joining ' + printer_name + ' ' + str(pi) + '...'
            self.close_module(state, pi.join, self.QUIT_THREAD_JOIN_TIMEOUT)
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
        self.logger.info("Goodbye ;-)")
        for handler in self.logger.handlers:
            handler.flush()
        if getattr(self, "poweroff_flag", False):
            if platforms.get_platform() == 'rpi' and rights.is_sudo_available():
                if config.get_settings()['dim_display_on_quit']:
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
