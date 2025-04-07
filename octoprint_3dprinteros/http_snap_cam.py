# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019.

# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)

import io
import time

import requests

import config
import dual_cam

class Camera(dual_cam.Camera):

    SOURCE_TIMEOUT = 5
    IMAGE_FORMAT = "jpeg"

    def init(self):
        try:
            import PIL.Image as PIL_Image
            self.cv2 = None
            self.PIL_Image = PIL_Image
            self.logger.info('Snap Camera: use PIL for resize')
        except (ImportError, RuntimeError):
            self.PIL_Image = None
            super().init()
            self.logger.info('Snap Camera: use OpenCV2 for resize')
        self.init_parameters()
        self.url = config.get_settings().get('camera', {}).get('snap_cam_url', 'http://127.0.0.1/webcam/?action=snapshot')

    def search_cameras(self):
        self.captures = [None]
        self.resized.append(False)
        self.fails.append(0)

    def make_shot(self, _, __):
        try:
            r = requests.get(self.url, timeout=self.SOURCE_TIMEOUT)
            r.raise_for_status()
            image_bytes = r.content
        except Exception as e:
            #self.logger.exception("Error getting camera frame from source:")
            time.sleep(self.SOURCE_TIMEOUT)
        else:
            if image_bytes:
                if len(image_bytes) > self.MAX_SMALL_IMAGE_SIZE:
                    if self.PIL_Image:
                        buf_file = io.BytesIO()
                        buf_file.write(image_bytes)
                        try:
                            pil_image = self.PIL_Image.open(buf_file)
                        except Exception:
                            self.logger.exception("Error getting camera frame from source:")
                        else:
                            cloud_correct_res = self.get_resize_resolution(*pil_image.size)
                            if cloud_correct_res:
                                pil_image = pil_image.resize(cloud_correct_res[::-1])  # reverse tuple
                            buf_file.truncate(0)
                            buf_file.seek(0)
                            pil_image.save(buf_file, format=self.IMAGE_FORMAT)
                            buf_file.seek(0)
                            image_bytes = buf_file.read()
                            pil_image.close()
                            buf_file.close()
                    else:
                        image_bytes = self.resize_image(image_bytes)
            return image_bytes

    def close_captures(self):
        pass

if __name__ == '__main__':
    Camera()
