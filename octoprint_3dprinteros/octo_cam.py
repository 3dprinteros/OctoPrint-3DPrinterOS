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
    KOEF_RESOLUTION = X_RESOLUTION / Y_RESOLUTION
    X_SMALL_RESOLUTION = 64.0
    Y_SMALL_RESOLUTION = 48.0
    MAX_SMALL_IMAGE_SIZE = 2000
    MAX_IMAGE_SIZE = 50000
    SAME_IMAGE_FRAME = 'S'
    STATE_INACTIVE = 0
    STATE_ACTIVE = 1
    STATE_INACTIVE_RESEND = -1

    def __init__(self, parent):
        self.logger = parent.logger
        self.printer_token = parent.printer_token
        self.image_extension = '.jpg'  # config.get_settings()["camera"]["img_ext"]
        self.image_quality = 60  # config.get_settings()["camera"]["img_qual"]
        self.hardware_resize = True  # config.get_settings()["camera"]["hardware_resize"]
        self.min_loop_time = 1  # config.get_settings()["camera"]["min_loop_time"]
        self.stop_flag = False
        self.parent = parent
        self.min_empty_frame_time = 20
        self.http_client = http_client.HTTPClient(self, keep_connection_flag=True, max_send_retry_count=0, debug=False)
        self.ip = '127.0.0.1'
        # self.port = 8080
        # self.camera_url = 'http://' + self.ip + ':' + str(self.port) + '/?action=snapshot'
        self.camera_url = 'http://' + self.ip + '/webcam/?action=snapshot'
        self.init_parameters()

    def init_parameters(self):
        self.cloud_camera_state = {}
        self.last_sent_frame_time = {}
        self.active_camera_number = 0

    def is_same_image_frame(self):
        number = self.active_camera_number
        return not self.cloud_camera_state.get(number) and \
               (self.last_sent_frame_time.get(number) or self.STATE_INACTIVE)+self.min_empty_frame_time >= time.time()

    # @log.log_exception
    def main_loop(self):
        time.sleep(self.min_loop_time)
        while not self.stop_flag and not self.parent.stop_flag:
            self.logger.debug("camera cycle")
            frame_start_time = time.time()
            frame = self.make_shot()
            if frame:
                # self.logger.debug("Got frame from camera N" + str(number))
                self.send_frame(frame)
            else:
                self.logger.warning("No frame from camera")
            while time.time() < frame_start_time + 0.001 + self.min_loop_time:
                time.sleep(0.01)
        self.http_client.close()
        # sys.exit(0)

    def get_camera_number_for_cloud(self):
        return 127001

    def send_frame(self, frame):
        number = self.active_camera_number
        if frame != self.SAME_IMAGE_FRAME:
            self.last_sent_frame_time[number] = time.time()
            frame = base64.b64encode(str(frame))
        send_number = self.get_camera_number_for_cloud()
        message = self.printer_token, send_number, frame
        #self.logger.debug("Camera %d sending frame to server..." % send_number)
        answer = self.http_client.pack_and_send('camera', *message)
        if answer is None:
            self.logger.debug("Camera %d can't send frame to server - HTTP error" % send_number)
        else:
            self.cloud_camera_state[number] = answer.get('state')
            # need to resend small image (image key is already expired in cloud)
            if self.cloud_camera_state.get(number) == self.STATE_INACTIVE_RESEND:
                self.cloud_camera_state[number] = self.STATE_INACTIVE
                self.last_sent_frame_time[number] = 0

    def get_resize_resolution(self, image):
        number = self.active_camera_number
        width, height = image.size
        if self.cloud_camera_state.get(number):
            sizes = self.X_RESOLUTION, self.Y_RESOLUTION
        else:
            sizes = self.X_SMALL_RESOLUTION, self.Y_SMALL_RESOLUTION
        if width > sizes[0] or height > sizes[1]:
            if self.KOEF_RESOLUTION == 1.0*width/height:
                return sizes
            koef = min(sizes[0] / width, sizes[1] / height)
            return round(width * koef), round(height * koef)

    def make_shot(self):
        if self.is_same_image_frame():
            return self.SAME_IMAGE_FRAME
        try:
            r = requests.get(self.camera_url, timeout=5)
            r.raise_for_status()
            image_bytes = r.content
        except Exception as e:
            self.logger.exception("Could not capture image from %s: %s" % (self.camera_url, str(e)))
            return
        if not image_bytes:
            return
        image_size = len(image_bytes)
        number = self.active_camera_number
        max_size = self.MAX_IMAGE_SIZE if self.cloud_camera_state.get(number) else self.MAX_SMALL_IMAGE_SIZE
        if image_size <= max_size:
            return image_bytes
        try:
            buf = StringIO()
            buf.write(image_bytes)
            image = Image.open(buf)
            sizes = self.get_resize_resolution(image)
            if not sizes:
                return image_bytes
            self.logger.debug("Recompressing snapshot to smaller size")
            image.thumbnail(sizes)
            image_bytes = StringIO()
            image.save(image_bytes, format="jpeg")
            image_bytes.seek(0, 2)
            new_image_size = image_bytes.tell()
            image_bytes.seek(0)
            image_bytes = image_bytes.read()
            self.logger.debug("Image transcoded from size {} to {}".format(image_size, new_image_size))
            self.logger.debug("Image captured from {}".format(self.camera_url))
            return image_bytes
        except Exception as e:
            self.logger.exception("Could not resize snapshot: " + str(e))

    def register_error(self, code, message, is_blocking=False):
        self.logger.warning("Error N%d. %s" % (code, message))
