# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019.
# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)


import hashlib
import os
import sys
import logging
import subprocess
import time
import platforms
import threading
import uuid

import camera_controller
import config


class NoSubprocCameraController(camera_controller.CameraController):

    RUN_FORMAT_NAME = "thread"

    def __init__(self, app):
        self.app = app
        self.call_lock = threading.RLock()
        self.remove_abscent_cameras()
        self.logger = logging.getLogger(__name__)
        self.mac = app.host_id
        self.current_camera_name = self.DISABLE_CAMERA_NAME
        self.camera_check_and_restart_thread = None
        self.camera_obj = None
        self.camera_thread = None
        self.thread_stop_handle = [False]
        self.enabled = config.get_settings()["camera"]["enabled"]
        self.token = ""

    def check_camera_and_restart_on_error(self):
        try:
            return self.camera_thread and self.camera_thread.is_alive()
        except (AttributeError, RuntimeError):
            return False

    def run_camera(self, module_name, camera_name, token):
        if not module_name.endswith(".py"):
            self.logger.error(f'Camera {module_name} can only run as subprocess')
        else:
            module_name = module_name.strip(".py")
            try:
                self.logger.warning('Importing module: ' + module_name)
                camera_module = __import__(module_name)
            except ImportError:
                self.logger.warning('Could not found camera module: ' + module_name)
            else:
                try:
                    self.camera_obj = camera_module.Camera(autostart=False)
                    if not self.camera_obj.token:
                        self.camera_obj.init_token(token, self.mac)
                    self.camera_thread = threading.Thread(target=self.camera_obj.start, daemon=True)
                    self.logger.warning('Starting camera thread for: ' + module_name)
                    self.camera_thread.start()
                except Exception as e:
                    self.logger.warning('Could not launch camera due to error: ' + str(e))
                else:
                    return self.camera_thread.is_alive()
        return False

    def stop_camera_process(self, _=False):
        self.logger.info('Stopping camera thread...')
        try:
            if self.camera_obj:
                self.camera_obj.close()
                self.camera_thread.join(self.camera_obj.JOIN_TIMEOUT)
        except AttributeError:
            pass
        self.logger.info('...camera thread stopped.')
        self.current_camera_name = "Disable camera"
        self.camera_obj = None
        self.camera_thread = None
