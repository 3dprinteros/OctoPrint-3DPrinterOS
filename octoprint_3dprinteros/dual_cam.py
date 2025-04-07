# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019.
# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)

import base64
import logging
import signal
import sys
import os
import time

# fix of broken paths for windows
if not sys.path:
    sys.path = []
path = os.getcwd()
if path not in sys.path:
    sys.path.insert(0, path)


import config
import http_client
import log
import paths
import platforms
import user_login
import threading


class EmptyFrame(Exception):
    pass


class CameraCaptureThread(threading.Thread):
    MAX_FAIL_COUNT = 10
    RETRY_SLEEP = 0.1

    def __init__(self, source, capture, cv2_module, np_module, resized=False, frame_skip=0):
        super(CameraCaptureThread, self).__init__()
        self.stopped = False
        self.resized = resized
        self.frame_skip = frame_skip
        self.capture = capture
        self.source = source
        self.cv2 = cv2_module
        self.np = np_module
        self._failed_count = 0
        self._frame = self.np.zeros((480, 640, 3), self.np.uint8)
        self._frame_lock = threading.RLock()
        self.logger = logging.getLogger(f"Camera {source}")

    def get_frame(self):
        with self._frame_lock:
            return self._frame

    def get_failed_count(self):
        return self._failed_count

    def stop(self):
        self.stopped = True

    def run(self):
        while not self.stopped:
            if self._failed_count > self.MAX_FAIL_COUNT:
                self.logger.error(f"Reached max failed count for {self.source}. Stopping")
                break
            try:
                if isinstance(self.source, int):
                    frame_skip = self.frame_skip
                else:
                    frame_skip = 0
                grab_success = False
                while frame_skip > -1:
                    grab_success |= self.capture.grab()
                    frame_skip -= 1
                if grab_success:
                    state, frame = self.capture.retrieve()
                    if state:
                        with self._frame_lock:
                            self._frame = frame
                            self._failed_count = 0
                        continue
                with self._frame_lock:
                    self._frame = None
                self.logger.error(f"Failed to read frame from {self.source}. Retrying...")
                self._failed_count += 1
                time.sleep(self.RETRY_SLEEP)
            except Exception as e:
                self.logger.error(f"Exception occurred while capturing frame from {self.source}: {e}")
                self._failed_count += 1
                time.sleep(self.RETRY_SLEEP)
                with self._frame_lock:
                    self._frame = None
        if self.capture is not None:
            try:
                self.capture.release()
            except Exception as e:
                self.logger.error(f"Exception occurred while releasing capture from {self.source}: {e}")


class Camera:

    MAX_CAMERA_INDEX = 10
    FAILS_BEFORE_REINIT = 10
    X_RESOLUTION = 640
    Y_RESOLUTION = 480
    KOEF_RESOLUTION = X_RESOLUTION / Y_RESOLUTION
    X_SMALL_RESOLUTION = 64
    Y_SMALL_RESOLUTION = 48
    MAX_SMALL_IMAGE_SIZE = 2000
    MAX_IMAGE_SIZE = 50000
    MIN_LOOP_TIME  = 1.0
    REINIT_PAUSE  = 6
    QUALITY = 30 #jpeg
    FPS = 5
    FOCUS = config.get_settings()["camera"].get("focus")
    EXPOSURE = config.get_settings()["camera"].get("exposure")
    IMAGE_EXT = ".jpg"
    SAME_IMAGE = 'S'
    JOIN_TIMEOUT = 10

    DEBUG = config.get_settings()["camera"]["logging"]
    SAVE_IMG_PATH = ""
    #SAVE_IMG_PATH = '/tmp/3dprinteros_cam_frame.jpeg'

    @log.log_exception
    def __init__(self, autostart=True):
        self.pre_init(autostart)
        self.stop_flag = False
        self.init_settings()
        self.init_parameters()
        self.init()
        self.init_user_token()
        if autostart:
            self.start()

    def pre_init(self, autostart):
        kwargs = {}
        if config.get_settings()['camera']['logging']:
            kwargs["log_file_name"] = log.CAMERA_LOG_FILE
        if autostart:
            self.logger = log.create_logger(self.__class__.__name__, **kwargs)
            signal.signal(signal.SIGINT, self.intercept_signal)
            signal.signal(signal.SIGTERM, self.intercept_signal)
            self.read_argv = True
        else:
            self.read_argv = False
            self.logger = logging.getLogger(__class__.__name__)
        if self.DEBUG:
            self.logger.setLevel(logging.DEBUG)
            self.http_client_logging_level = logging.INFO
        else:
            self.http_client_logging_level = logging.WARNING

    def init_settings(self):
        self.offline_mode = bool("--offline" in sys.argv) or config.get_settings().get('offline_mode')
        self.hardware_resize = config.get_settings()["camera"]["hardware_resize"]
        self.send_as_imagejpeg = config.get_settings()["camera"]["binary_jpeg"]
        self.allow_net_input = config.get_settings()["camera"]["network_input"]
        self.allow_usb_input = config.get_settings()["camera"]["usb_input"]
        self.reconnect = config.get_settings()["camera"]["reconnect"]
        server_settings = config.get_settings()["camera"]["http_output"]
        if self.offline_mode and server_settings['enabled']:
            self.frame_skip = 0
        else:
            self.frame_skip = config.get_settings()["camera"]["frame_skip"]
        self.local_http_server = None
        if server_settings['enabled']:
            from camera_server import HTTPMJEPServer
            try:
                self.local_http_server = HTTPMJEPServer()
                self.local_http_server.start()
            except Exception as e:
                self.logger.exception('Exception on start of HTTPMJEPServer:' + str(e))

    def init_parameters(self):
        self.cloud_camera_state = {}
        self.last_sent_frame_time = {}
        self.active_camera_number = 0
        self.resized = []
        self.fails = []
        self.camera_sources = []

    def init(self):
        import numpy as np
        import cv2 as opencv
        self.np = np
        self.cv2 = opencv
        self.cv2_use_int = False

    def init_user_token(self, token=None, mac=None):
        self.token = token
        self.mac = mac
        if not self.token and self.read_argv and len(sys.argv) > 2:
            self.token = sys.argv[1]
            mac = sys.argv[2]
        if self.offline_mode:
            self.http_client = None
        else:
            if config.get_settings()['protocol']['user_login']:
                self.http_client = http_client.HTTPClient(self, logging_level=self.http_client_logging_level)
                self.logger.info("Camera: using UserLogin protocol")
                if not self.token:
                    ul = user_login.UserLogin(self)
                    ul.wait()
                    self.token = ul.user_token
            else:
                self.http_client = http_client.HTTPClientPrinterAPIV1(self, logging_level=self.http_client_logging_level)
                self.logger.info("Camera: using APIPrinter protocol")
                if not self.token:
                    auth_tokens = user_login.UserLogin.load_printer_auth_tokens()
                    if not auth_tokens:
                        self.logger.warning("No auth_token found to start camera. Camera quit...")
                    elif len(auth_tokens) > 1:
                        self.logger.warning("Several auth_tokens stored in login file! Camera can't determine correct one to use. Guessing correct one...")
                    if auth_tokens:
                        self.token = auth_tokens[-1][1] #TODO add ability to get proper auth_token for each usb_info
            #self.logger.debug("Camera auth_token=" + self.token)
            if not self.token:
                self.logger.info("Camera: no token to start. Exit...")
                if __name__ == '__main__':
                    sys.exit(1)
            if mac:
                self.http_client.host_id = mac #we need to use MAC from client to ensure that it's not changed on camera restart

    def start(self):
        self.search_cameras()
        self.main_loop()

    def intercept_signal(self, signal_code, frame):
        self.logger.info("SIGINT or SIGTERM received. Closing Camera Module...")
        self.close()

    def close(self):
        self.stop_flag = True

    def load_camera_urls(self):
        urls = []
        try:
            if os.path.isfile(paths.CAMERA_URLS_FILE):
                with open(paths.CAMERA_URLS_FILE, "r") as f:
                    for camera_ulr_line in f:
                        urls.append(camera_ulr_line)
        except Exception as e:
            self.logger.error(f'Error reading camera urls file: {e}')
        return urls

    def get_cam_backend(self, source):
        if platforms.PLATFORM in ("rpi", "linux") and isinstance(source, int):
            return self.cv2.CAP_V4L2

    def search_cameras(self):
        self.init_parameters()
        self.captures = []
        if self.allow_usb_input:
            for index in range(0, self.MAX_CAMERA_INDEX):
                if not self.stop_flag:
                    self.init_capture(index, self.get_cam_backend(index))
        if self.allow_net_input:
            for url in self.load_camera_urls():
                if not self.stop_flag:
                    self.init_capture(url.strip())
        if self.captures:
            self.logger.info("Got %d operational cameras" % len(self.captures))

    def init_capture(self, capture_name, backend=None, force_capture_number=None):
        self.logger.debug(f"Probing for camera {capture_name}")
        try:
            if backend:
                capture = self.cv2.VideoCapture(capture_name, backend)
            else:
                capture = self.cv2.VideoCapture(capture_name)
            capture.setExceptionMode(True)
            if capture.isOpened():
                self.logger.info(f"Found capture for {capture_name}")
                try:
                    capture.set(self.cv2.CAP_PROP_FPS, self.FPS)
                except:
                    self.logger.error(f'Error setting FPS({self.FPS}) for {capture_name}')
                self.logger.info("Camera FPS:" + str(capture.get(self.cv2.CAP_PROP_FPS)))
                if self.FOCUS is not None:
                    try:
                        capture.set(self.cv2.CAP_PROP_AUTOFOCUS, 0)
                    except:
                        self.logger.error(f'Error disabling AUTOFOCUS for {capture_name}')
                    try:
                        capture.set(self.cv2.CAP_PROP_FOCUS, self.FOCUS)
                    except:
                        self.logger.error(f'Error setting FOCUS({self.FOCUS}) for {capture_name}')
                    self.logger.info("Camera focus:" + str(capture.get(self.cv2.CAP_PROP_FOCUS)))
                if self.EXPOSURE is not None:
                    try:
                        capture.set(self.cv2.CAP_PROP_AUTO_EXPOSURE, 3) # 3 is manual exposure
                    except:
                        self.logger.error(f'Error setting EXPOSURE({self.FPS}) for {capture_name}')
                    try:
                        capture.set(self.cv2.CAP_PROP_EXPOSURE, self.EXPOSURE)
                    except:
                        self.logger.error(f'Error setting EXPOSURE({self.EXPOSURE}) for {capture_name}')
                    self.logger.info("Camera focus:" + str(capture.get(self.cv2.CAP_PROP_FOCUS)))
                if force_capture_number != None: # Can be index 0
                    try:
                        self.resized[force_capture_number] = self.set_resolution(capture)
                        capture_thread = CameraCaptureThread(capture_name, capture, self.cv2, self.np,
                                                             self.resized[force_capture_number], self.frame_skip)
                        capture_thread.start()
                        self.captures[force_capture_number] = capture_thread
                        self.fails[force_capture_number] = 0
                        self.cloud_camera_state[force_capture_number] = 1
                        self.last_sent_frame_time[force_capture_number] = time.monotonic()
                    except IndexError:
                        self.logger.error(f'Error re-init camera capture #{force_capture_number} for {capture_name}')
                else:
                    resize = self.set_resolution(capture)
                    self.resized.append(resize)
                    capture_thread = CameraCaptureThread(capture_name, capture, self.cv2, self.np, resize, self.frame_skip)
                    capture_thread.start()
                    self.captures.append(capture_thread)
                    self.camera_sources.append(capture_name)
                    self.fails.append(0)
                    self.cloud_camera_state[len(self.fails)-1] = 1
                    self.last_sent_frame_time[len(self.fails)-1] = time.monotonic()
                return True
            else:
                self.logger.debug(f"Camera at index {capture_name} can't be opened")
        except Exception as e:
            self.logger.error(f"Error on creation of video capture {capture_name}. Desc: {str(e)}")
        return False

    def set_resolution(self, cap):
        result = False
        if not self.hardware_resize:
            self.logger.info('Setting hardware resolution disabled in the settings. Relying on software resize')
        else:
            try:
                w = cap.get(self.cv2.CAP_PROP_FRAME_WIDTH)
                h = cap.get(self.cv2.CAP_PROP_FRAME_HEIGHT)
            except AttributeError:
                self.logger.warning('Unable to get current resolution. Assuming no access to camera resolution controls')
            else:
                if w and h:
                    if w == self.X_RESOLUTION and h == self.Y_RESOLUTION:
                        result = True
                    else:
                        try:
                            if cap.set(self.cv2.CAP_PROP_FRAME_WIDTH, type(w)(self.X_RESOLUTION)) and \
                               cap.set(self.cv2.CAP_PROP_FRAME_HEIGHT, type(h)(self.Y_RESOLUTION)):
                                self.logger.info(f'Capture {cap} resolution set to {self.X_RESOLUTION}x{self.Y_RESOLUTION}')
                                result = True
                            else:
                                self.logger.warning(f"Capture {cap} rejected resolution {self.X_RESOLUTION}x{self.Y_RESOLUTION}")
                        except:
                            self.logger.error(f"Capture {cap} got no attributes to set resolution")
        return result

    def get_resize_resolution(self, y, x):
        number = self.active_camera_number
        if self.cloud_camera_state.get(number):
            sizes = self.X_RESOLUTION, self.Y_RESOLUTION
        else:
            sizes = self.X_SMALL_RESOLUTION, self.Y_SMALL_RESOLUTION
        if x > sizes[0] or y > sizes[1]:
            if x / y != self.KOEF_RESOLUTION:
                koef = min(sizes[0] / x, sizes[1] / y)
                sizes = round(x * koef), round(y * koef)
        return sizes

    def resize_cv2_frame(self, frame):
        number = self.active_camera_number
        self.logger.debug("Resizing frame of camera" + str(number))
        if not self.resized[number] and self.cloud_camera_state.get(number):  # resize using software
            try:
                sizes = self.get_resize_resolution(*frame.shape[:2])
                if not sizes:
                    return frame
                if self.cv2_use_int:
                    sizes = (int(sizes[0]), int(sizes[1]))
                frame = self.cv2.resize(frame, sizes, interpolation=self.cv2.INTER_NEAREST)
                #if not frame and self.empty_frame_error:
                if not frame.any():
                    raise EmptyFrame
            except Exception as e:
                if isinstance(e, TypeError) and not self.cv2_use_int:
                    # some opencv version accept integer args here
                    self.cv2_use_int = True
                    self.fails[number] += 1
                    self.logger.warning("TypeError while software resize of frame: " + str(e))
                    return self.resize_cv2_frame(frame)
                self.logger.warning("Error while software resize of frame: " + str(e))
        return frame

    def get_image_from_cv2_frame(self, frame):
        if frame.any():
            encode_param = [
                int(self.cv2.IMWRITE_JPEG_QUALITY),
                40 if not self.cloud_camera_state.get(self.active_camera_number) else self.QUALITY
            ]
            try:
                result, encoded_frame = self.cv2.imencode(self.IMAGE_EXT, frame, encode_param)
            except Exception as e:
                self.logger.warning('Failed to encode camera frame: ' + str(e))
                result, encoded_frame = None, None
            if result:
                data = self.np.array(encoded_frame)
                string_data = data.tostring()
                if self.SAVE_IMG_PATH:
                    with open(self.SAVE_IMG_PATH, 'wb') as f:
                        f.write(string_data)
                # self.logger.debug("Successfully captured and encoded from" + str(capture))
                return string_data

    def resize_image(self, img):
        number = self.active_camera_number
        max_image_size = self.MAX_IMAGE_SIZE if self.cloud_camera_state.get(number) else self.MAX_SMALL_IMAGE_SIZE
        if not img or len(img) <= max_image_size:
            return img
        try:
            buf = self.np.fromstring(img, dtype=self.np.uint8)
            frame = self.cv2.imdecode(buf, self.cv2.IMREAD_UNCHANGED)
        except Exception as e:
            self.logger.warning('Failed to decode camera frame: ' + str(e))
            return img
        return self.get_image_from_cv2_frame(self.resize_cv2_frame(frame))

    def is_same_image_frame(self):
        number = self.active_camera_number
        return not self.cloud_camera_state.get(number)

    def make_shot(self, number, capture_thread):
        self.logger.debug("Capturing frame from " + str(capture_thread.source))
        frame = capture_thread.get_frame()
        if not frame is None and frame.any():
            self.fails[number] = capture_thread.get_failed_count()
            if self.is_same_image_frame():
                return self.SAME_IMAGE
            frame = self.resize_cv2_frame(frame)
            return self.get_image_from_cv2_frame(frame)
        else:
            self.fails[number] += 1

    def get_camera_number_for_cloud(self):
        return self.active_camera_number + 1

    def send_frame(self, frame):
        number = self.active_camera_number
        if frame != Camera.SAME_IMAGE:
            self.last_sent_frame_time[number] = time.monotonic()
        send_number = self.get_camera_number_for_cloud()
        message = self.token, send_number, "Camera" + str(send_number)
        #self.logger.debug("Camera %d sending frame to server..." % send_number)
        if self.send_as_imagejpeg:
            if frame == Camera.SAME_IMAGE:
                frame == ""
            answer = self.pack_and_send_as_imagejpeg(message, frame)
        else:
            if frame != Camera.SAME_IMAGE:
                frame = base64.b64encode(frame).decode()
            answer = self.pack_and_send(message, frame)
        if type(answer) != dict:
            self.logger.debug("Camera %d can't send frame to server - HTTP error" % send_number)
        else:
            self.cloud_camera_state[number] = answer.get('state', 0)
            if Camera.DEBUG:
                self.logger.debug("REQ: " + str(message))
                self.logger.debug("RESP: " + str(answer))
                if frame == Camera.SAME_IMAGE:
                    self.logger.debug("Frame: 'S'")
                else:
                    self.logger.debug("Frame: %dB", len(frame))

    def pack_and_send(self, message, frame):
        message = list(message)
        message.append(frame)
        return self.http_client.pack_and_send('camera', *message)

    def pack_and_send_as_imagejpeg(self, message, frame):
        target_url_path, package_message = self.http_client.pack(http_client.HTTPClient.CAMERA_IMAGEJPEG, *message)
        headers = { "Content-Type": "image/jpeg",
                "Content-Length": len(frame),
                "Camera-Properties": package_message }
        return self.http_client.send(target_url_path, frame, headers)

    def main_loop(self):
        while not self.stop_flag:
            frame_start_time = time.monotonic()
            reinit_needed = False
            for number, capture_thread in enumerate(self.captures):
                self.active_camera_number = number
                if self.fails[number] > self.FAILS_BEFORE_REINIT:
                    self.logger.warning("Reached fail threshold on camera number %d" % number)
                    if self.reconnect:
                        self.logger.info("Restarting camera number %d" % number)
                        try:
                            capture_thread.stop()
                        except:
                            pass
                        if self.init_capture(self.camera_sources[number], self.get_cam_backend(self.camera_sources[number]), number):
                            capture_thread = self.captures[number]
                    else:
                        self.fails[number] = 0 
                frame = self.make_shot(number, capture_thread)
                if not self.offline_mode:
                    if frame:
                        self.logger.debug("Got frame from camera N{number} of size: {len(frame)}")
                        self.send_frame(frame)
                    else:
                        self.logger.warning("No frame from camera N" + str(number))
                if self.local_http_server:
                    for key in self.local_http_server.watched_streams.keys():
                        key = key - 1
                        if self.offline_mode or not self.cloud_camera_state.get(key):
                            self.cloud_camera_state[key] = bool(self.local_http_server.watched_streams.get(key+1))
                        if frame and frame != self.SAME_IMAGE:
                            self.local_http_server.put_frame(frame, number + 1)
            if self.captures:
                if not self.offline_mode:
                    while time.monotonic() <= frame_start_time + self.MIN_LOOP_TIME:
                        time.sleep(0.001)
                else:
                    time.sleep(0.001) # frame rate limit for faulty cameras in offline mode
            else:
                time.sleep(5)
                if config.get_settings()['camera']['reinit_on_no_cam']:
                    reinit_needed = True
            if reinit_needed:
                self.logger.debug("Starting cameras reinitialisation...")
                if self.local_http_server:
                    self.local_http_server.flush_storages()
                self.close_captures()
                time.sleep(self.REINIT_PAUSE)
                self.search_cameras()
                self.logger.warning("...done reinitialising cameras.")
        self.close_captures()
        if self.http_client:
            self.http_client.close()
        if self.local_http_server:
            self.local_http_server.stop()
            self.local_http_server.join()
        sys.exit(0)

    def close_captures(self):
        for capture_thread in self.captures:
            capture_thread.stop()
        for capture_thread in self.captures:
            if capture_thread and capture_thread.is_alive():
                capture_thread.join(self.JOIN_TIMEOUT)
            self.logger.info("Closed camera capture " + str(capture_thread.source))

    def register_error(self, code, message, is_blocking=False, is_info=False):
        self.logger.warning("Error N%d. %s" % (code, message))


if __name__ == '__main__':
    Camera()
