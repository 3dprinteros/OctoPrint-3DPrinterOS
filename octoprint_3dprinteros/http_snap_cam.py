import io
import time

import PIL.Image
import requests

import dual_cam

class Camera(dual_cam.Camera):

    CAMERA_SOURCE_URL = 'http://127.0.0.1/webcam/?action=snapshot'
    SOURCE_TIMEOUT = 5
    IMAGE_FORMAT = "jpeg"

    def init(self):
        self.init_parameters()
        self.cv2 = None

    def search_cameras(self):
        self.captures = [None]
        self.resized.append(False)
        self.fails.append(0)

    def make_shot(self, _, __):
        try:
            r = requests.get(self.CAMERA_SOURCE_URL, timeout=self.SOURCE_TIMEOUT)
            r.raise_for_status()
            image_bytes = r.content
        except Exception as e:
            #self.logger.exception("Error getting camera frame from source:")
            time.sleep(self.SOURCE_TIMEOUT)
        else:
            if image_bytes:
                if len(image_bytes) > self.MAX_SMALL_IMAGE_SIZE:
                    buf_file = io.BytesIO()
                    buf_file.write(image_bytes)
                    try:
                        pil_image = PIL.Image.open(buf_file)
                    except Exception:
                        self.logger.exception("Error getting camera frame from source:")
                    else:
                        cloud_correct_res = self.get_resize_resolution(*pil_image.size)
                        if cloud_correct_res:
                            pil_image.resize(cloud_correct_res)
                        buf_file.truncate(0)
                        buf_file.seek(0)
                        pil_image.save(buf_file, format=self.IMAGE_FORMAT)
                        buf_file.seek(0)
                        image_bytes = buf_file.read()
                        pil_image.close()
                        buf_file.close()
            return image_bytes

    def close_captures(self):
        pass

if __name__ == '__main__':
    Camera()
