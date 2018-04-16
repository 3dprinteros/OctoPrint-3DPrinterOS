#
# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019. Built in San Francisco.
#
# This software is distributed under commercial non-GPL license for personal, educational,
# corporate or any other use. The software as a whole or any parts of that are prohibited
# for distribution and/or use without obtaining license from 3D Control Systems, Inc.
#
# If you do not have the license to use this software, please delete all software files
# immediately and contact sales to obtain the license: sales@3dprinteros.com.
# If you are unsure about the licensing please contact directly our sales: sales@3dprinteros.com.

import sys
import time
import logging
import requests
import base64

from PIL import Image
try:
    from cStringIO import StringIO
except:
    from StringIO import StringIO

import http_client


class OctoCamera:
    X_RESOLUTION = 640.0
    Y_RESOLUTION = 480.0

    def __init__(self, parent):
        self.logger = parent.logger
        self.printer_token = parent.printer_token
        self.image_extension = '.jpg'  # config.get_settings()["camera"]["img_ext"]
        self.image_quality = 60  # config.get_settings()["camera"]["img_qual"]
        self.hardware_resize = True  # config.get_settings()["camera"]["hardware_resize"]
        self.min_loop_time = 1  # config.get_settings()["camera"]["min_loop_time"]
        self.stop_flag = False
        self.parent = parent
        self.http_client = http_client.HTTPClient(self, keep_connection_flag=True, max_send_retry_count=0, debug=False)
        self.ip = '127.0.0.1'
        self.port = 8080
        self.camera_url = 'http://' + self.ip + ':' + str(self.port) + '/?action=snapshot'
        self._max_image_size = 100000

    # @log.log_exception
    def main_loop(self):
        number = 127000
        time.sleep(self.min_loop_time)
        while not self.stop_flag and not self.parent.stop_flag:
            self.logger.debug("camera %s cycle" % number)
            frame_start_time = time.time()
            frame = self.make_shot()
            if frame:
                # self.logger.debug("Got frame from camera N" + str(number))
                self.send_frame(number, frame)
            else:
                self.logger.warning("No frame from camera N" + str(number))
            while time.time() < frame_start_time + 0.001 + self.min_loop_time:
                time.sleep(0.01)
        self.http_client.close()
        # sys.exit(0)

    def send_frame(self, number, frame):
        frame = base64.b64encode(str(frame))
        number = number + 1
        message = self.printer_token, number, frame
        #self.logger.debug("Camera %d sending frame to server..." % number)
        answer = self.http_client.pack_and_send('camera', *message)
        if answer is None:
            self.logger.debug("Camera %d can't send frame to server - HTTP error" % number)

    def make_shot(self):
        try:
            r = requests.get(self.camera_url, timeout=5)
            r.raise_for_status()
        except Exception:
            self.logger.exception("Could not capture image from %s" % self.camera_url)
            return

        try:
            image_bytes = r.content
            image_size = len(image_bytes)
            if image_size > self._max_image_size:
                self.logger.debug("Recompressing snapshot to smaller size")
                buf = StringIO()
                buf.write(image_bytes)
                image = Image.open(buf)
                image.thumbnail((self.X_RESOLUTION, self.Y_RESOLUTION))
                # if self._settings.global_get(["webcam", "flipH"]):
                #     image = image.transpose(Image.FLIP_LEFT_RIGHT)
                # if self._settings.global_get(["webcam", "flipV"]):
                #     image = image.transpose(Image.FLIP_TOP_BOTTOM)
                # if self._settings.global_get(["webcam", "rotate90"]):
                #     image = image.transpose(Image.ROTATE_90)
                image_bytes = StringIO()
                image.save(image_bytes, format="jpeg")
                image_bytes.seek(0, 2)
                new_image_size = image_bytes.tell()
                image_bytes.seek(0)
                self.logger.debug("Image transcoded from size {} to {}".format(image_size, new_image_size))
            self.logger.debug("Image captured from {}".format(self.camera_url))
            return image_bytes
        except Exception:
            self.logger.exception("Could not get snapshot")

    def register_error(self, code, message, is_blocking=False):
        self.logger.warning("Error N%d. %s" % (code, message))
