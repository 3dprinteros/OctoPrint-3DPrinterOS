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

import config


def remove_non_existed_modules(camera_modules_dict):
    working_dir = os.path.dirname(os.path.abspath(__file__))
    verified_camera_modules = {}
    for camera_name,module in camera_modules_dict.items():
        if not module or os.path.exists(os.path.join(working_dir, module)):
            verified_camera_modules[camera_name] = module
    return verified_camera_modules


class CameraController:

    DUAL_CAMERA_NAME = "Dual camera"
    MULTI_CAMERA_NAME = "Multi camera"
    PI_CAMERA_NAME = "Pi camera"
    HTTP_SNAP_CAMERA_NAME = "MJPGS camera"
    DISABLE_CAMERA_NAME = "Disable camera"
    MP_CAMERA = "MP camera"

    CAMERA_MODULES = { DUAL_CAMERA_NAME: "dual_cam.py",
                       MULTI_CAMERA_NAME: "multi_cam.py",
                       PI_CAMERA_NAME: "rpi_cam.py",
                       HTTP_SNAP_CAMERA_NAME: "http_snap_cam.py",
                       #MP_CAMERA: "ffmpeg",  
                       DISABLE_CAMERA_NAME: None }

    CAMERA_MODULES = remove_non_existed_modules(CAMERA_MODULES)

    CAMERA_FAIL_ERROR_STR = b"select() timeout."
    CAMERA_FAIL_AFTER_RESTART_SLEEP = 60
    CAMERA_FAIL_CHECK_TIME = 2
    SUBPROC_STOP_TIMEOUT = 6

    MP_CAMERA_DEFAULT_PARAMS = { "-framerate": "5",
            "-video_size": "640x480",
            "-pix_fmt": "yuv420p",
            "-f": "flv",
            "-c:v": "h264_omx",
            "-b:v": "60k",
            "-i": "",
            }

    MP_CAMERA_DEFAULT_URL = "cam-" + config.get_settings()['URL']
    MP_CAMERA_DEFAULT_WEBAPP = "live"
    MP_CAMERA_URL_MASK = "rtmp://%s/%s?token=%s/%s"

    CAMERA_MODULES = remove_non_existed_modules(CAMERA_MODULES)

    def __init__(self, app):
        self.app = app
        self.logger = logging.getLogger(__name__)
        self.mac = app.host_id
        self.current_camera_name = self.DISABLE_CAMERA_NAME
        self.camera_check_and_restart_thread = None
        self.camera_process = None
        self.enabled = config.get_settings()["camera"]["enabled"]
        self.token = ""
        self.start_camera_process()

    def check_camera_and_restart_on_error(self):
        while not self.app.stop_flag and self.camera_check_and_restart_thread:
            if self.camera_process:
                output = self.camera_process.communicate()
                if output:
                    if self.CAMERA_FAIL_ERROR_STR in output:
                        if self.camera_process:
                            self.restart_camera()
                            time.sleep(self.CAMERA_FAIL_AFTER_RESTART_SLEEP)
            time.sleep(self.CAMERA_FAIL_CHECK_TIME)

    def init_camera_check_thread(self):
        if config.get_settings()['camera']['restart_on_error_output']:
            self.camera_check_and_restart_thread = threading.Thread(target=self.check_camera_and_restart_on_error)
        else:
            self.camera_check_and_restart_thread = None

    def load_token(self):
        if config.get_settings()['protocol']['user_login']:
            self.token = self.app.user_login.user_token
        else:
            auth_tokens = self.app.user_login.load_printer_auth_tokens()
            if auth_tokens:
                self.token = auth_tokens[-1][1] #TODO add ability to get proper auth_token for each usb_info
            else:
                self.token = None

    def start_camera_process(self, camera_name=None, token=None):
        self.logger.info('Launching camera subprocess')
        if not token:
            self.load_token()
            token = self.token
        if not token and not self.app.offline_mode:
            self.logger.info("No token to start the camera process")
            return False
        if not self.mac:
            self.mac = ""
        settings = config.get_settings()
        camera_name_default = settings['camera']['default']
        for new_camera_name in (camera_name, camera_name_default, self.DUAL_CAMERA_NAME, self.DISABLE_CAMERA_NAME):
            module_name = self.CAMERA_MODULES.get(new_camera_name)
            if module_name or (new_camera_name == self.DISABLE_CAMERA_NAME and new_camera_name != camera_name_default):
                camera_name = new_camera_name
                break
        if camera_name_default != camera_name:
            settings['camera']['default'] = camera_name
            config.Config.instance().save_settings(settings)
        if not self.enabled:
            self.logger.info("Can't launch camera - disabled in config")
        elif module_name:
            if self.run_camera(module_name, camera_name, token):
                self.current_camera_name = camera_name
                self.token = token
                self.logger.info('Camera started: ' + camera_name)
                self.init_camera_check_thread()
                return True
        return False

    def run_camera(self, module_name, camera_name, token):
        cam_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), module_name)
        if module_name == "ffmpeg":
            start_command = self.form_mp_camera_command(token)
        else:
            start_command = [sys.executable, cam_path, token, self.mac]
            if self.app.offline_mode:
                start_command.append("--offline")
            camera_popen_kwargs = {'close_fds': True}
            if platforms.get_platform() == 'win':
                camera_popen_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            if self.camera_check_and_restart_thread:
                camera_popen_kwargs['stdout'] = subprocess.PIPE
                camera_popen_kwargs['stderr'] = subprocess.STDOUT
        try:
            self.camera_process = subprocess.Popen(start_command, **camera_popen_kwargs)
        except Exception as e:
            self.logger.warning('Could not launch camera due to error: ' + str(e))
        else:
            return True
        return False

    def restart_camera(self, token=None):
        if self.current_camera_name != self.DISABLE_CAMERA_NAME:
            new_camera_name = self.current_camera_name
        else:
            new_camera_name = config.get_settings()['camera']['default']
        self.load_token()
        self.logger.info('Restarting camera module: ' + new_camera_name)
        self.stop_camera_process()
        return self.start_camera_process(new_camera_name)

    def form_mp_camera_command(self, token):
        cam_path = self.CAMERA_MODULES['MP camera']
        if platforms.PLATFORM == 'win':
            cam_path += ".exe"
        stream_id = uuid.uuid4().hex
        # TODO make a real crypto token instead of this dummy token
        token_hash = hashlib.sha256()
        token_hash.update(stream_id)
        token_hash.update(token)
        new_token = token_hash.hexdigest()
        args = []
        for key, value in self.MP_CAMERA_DEFAULT_PARAMS.items():
            args.append(key) 
            if key == '-i':
                # using first camera until multiple camera processes support will be implemented
                cameras = self.get_cameras_list()
                if cameras:
                    value = cameras[0]
            args.append(value) 
        url = self.MP_CAMERA_URL_MASK % (self.MP_CAMERA_DEFAULT_URL, self.MP_CAMERA_DEFAULT_WEBAPP, new_token, uuid)
        args.append(url)
        start_command = [cam_path] + args
        return start_command

    def get_current_camera_name(self):
        return self.current_camera_name

    def switch_camera(self, new_camera_name, token=None):
        if not token:
            token=self.token
        if not config.get_settings()['camera']['switch_type'] or not new_camera_name:
            new_camera_name = config.get_settings()['camera']['default']
        self.logger.info('Switching camera module from %s to %s' % (self.current_camera_name, new_camera_name))
        self.stop_camera_process()
        return self.start_camera_process(camera_name=new_camera_name, token=token)

    def stop_camera_process(self):
        self.logger.info('Terminating camera process...')
        counter = self.SUBPROC_STOP_TIMEOUT
        while counter and self.camera_process:
            self.camera_check_and_restart_thread = None
            try:
                self.camera_process.terminate()
                time.sleep(0.1)
                if self.camera_process.poll() != None:
                    self.camera_process = None
                    break
            except (OSError, AttributeError):
                self.camera_process = None
                break
            counter -= 1
            time.sleep(1)
        if self.camera_process:
            self.logger.info('Sending kill signal to camera process...')
            try:
                self.camera_process.kill()
            except:
                pass
            time.sleep(1) # give subprocess a time to go down
        self.logger.info('...camera process terminated.')
        self.current_camera_name = "Disable camera"
        self.camera_process = None

    def get_cameras_list(self):
        cameras = []
        if platforms.PLATFORM == "win":
            cameras = self.get_cameras_list_windows()
        elif platforms.PLATFORM == "rpi" or platforms.PLATFORM == 'linux':
            dev_folder = "/dev/"
            for filename in os.listdir('dev'):
                if "video" in filename:
                    cameras.append(dev_folder + filename)
        return cameras

    def get_cameras_list_windows(self):
        ffmpeg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.CAMERA_MODULES["MP camera"])
        cmd_args = [ffmpeg_path, "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
        proc = subprocess.run(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8")
        lines = proc.stdout.split("\n")
        cameras = []
        for line in lines:
            if line.startswith("["):
                if 'DirectShow audio devices' in line:
                    break
                elif 'DirectShow video devices' in line or "Alternative name" in line:
                    continue
                else:
                    device = "video=" + line.split(' ')[-1].strip().strip('"')
                    cmd_args = [ffmpeg_path, "-list_options", "true", "-f", "dshow", "-i"]
                    cmd_args.append(device)
                    proc = subprocess.run(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8")
                    #TODO parse available camera modes from this call
                    if not proc.returncode:
                        self.logger.info("Camera device detected: " +  str(device))
                        cameras.append(device)
        return cameras
