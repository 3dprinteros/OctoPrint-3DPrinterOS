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


def remove_non_existed_modules(camera_modules_dict):
    working_dir = os.path.dirname(os.path.abspath(__file__))
    verified_camera_modules = {}
    for camera_name,module in camera_modules_dict.items():
        if not module or os.path.exists(os.path.join(working_dir, module)):
            verified_camera_modules[camera_name] = module
    return verified_camera_modules


class NoSubprocCameraController(camera_controller.CameraController):

    def __init__(self, app):
        self.app = app
        self.logger = logging.getLogger(__name__)
        self.mac = app.host_id
        self.current_camera_name = self.DISABLE_CAMERA_NAME
        self.camera_check_and_restart_thread = None
        self.camera_class = None
        self.camera_thread = None
        self.thread_stop_handle = [False]
        self.enabled = config.get_settings()["camera"]["enabled"]
        self.token = ""
        self.start_camera_process()

    def check_camera_and_restart_on_error(self):
        try:
            return self.camera_thread and self.camera_thread.is_alive()
        except (AttributeError, RuntimeError):
            return False

    def run_camera(self, module_name, camera_name, _):
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
                    self.camera_class = camera_module.Camera(autostart=False)
                    self.camera_thread = threading.Thread(target=self.camera_class.start)
                    self.logger.warning('Starting camera thread for: ' + module_name)
                    self.camera_thread.start()
                except Exception as e:
                    self.logger.warning('Could not launch camera due to error: ' + str(e))
                else:
                    return self.camera_thread.is_alive()
        return False

    def stop_camera_process(self):
        self.logger.info('Stopping camera thread...')
        try:
            if self.camera_class:
                self.camera_class.close()
                self.camera_thread.join(3)
        except AttributeError:
            pass
        self.logger.info('...camera thread stopped.')
        self.current_camera_name = "Disable camera"
        self.camera_class = None
        self.camera_thread = None


if __name__ == "__main__":
    cc = NoSubprocCameraController(None)
