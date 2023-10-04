# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2023.

# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)

from collections import OrderedDict

import os

from static_and_stored_detect import StaticDetector
from camera_controller import CameraController
from no_subproc_camera_controller import NoSubprocCameraController

import app
import config
import user_login


class SlaveApp(app.App):

    #def __init__(self, owner=None, **kwargs):
    def __init__(self, owner=None):
        self.owner = owner
        self.init_ok = None
        # for arg_name in kwargs:
        #     setattr(self, arg_name, kwargs[arg_name])
        app.App.__init__(self)

    def init_adv(self):
        for detector_class in (StaticDetector,):
            self.detectors[detector_class.__name__] = detector_class(self)
        self.user_login = user_login.UserLogin(self, retry_in_background=False)
        if self.user_login.wait():
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
            if config.get_settings()["camera"].get("no_subprocess"):
                self.camera_controller = NoSubprocCameraController(self)
            else:
                self.camera_controller = CameraController(self)
                self.init_ok = True

    def quit(self):
        printer_interfaces = getattr(self, "printer_interfaces", [])
        for pi in printer_interfaces:
            printer_name = pi.printer_profile.get('name', 'nameless printer')
            state = 'Closing ' + printer_name + ' ' + str(pi) + '...'
            self.close_module(state, pi.close)
        for pi in printer_interfaces:
            printer_name = pi.printer_profile.get('name', 'nameless printer')
            state = 'Joining ' + printer_name + ' ' + str(pi) + '...'
            self.close_module(state, pi.join, self.QUIT_THREAD_JOIN_TIMEOUT)
        #self.owner = None
        self.init_ok = False
        self.logger.info("3DPrinterOS app stopped")
