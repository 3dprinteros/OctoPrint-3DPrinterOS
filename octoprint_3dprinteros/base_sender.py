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

import re
import time
import base64
import logging
import threading
import collections

class BaseSender:

    def __init__(self, parent, usb_info, profile):
        self.logger = parent.logger
        self.stop_flag = False
        self.parent = parent
        self.profile = profile
        self.usb_info = usb_info
        self.position = [0, 0, 0, 0]  # X, Y, Z, E
        self.temps = [0,0]
        self.target_temps = [0,0]
        self.total_gcodes = None
        self.buffer = collections.deque()
        self.current_line_number = 0
        self.pause_flag = False
        self.recv_callback_lock = threading.Lock()
        self.recv_callback = None
        self.responses_planned = 0
        self.responses = []
        self.filename = None

    def set_total_gcodes(self, length):
        raise NotImplementedError

    def load_gcodes(self, gcodes):
        raise NotImplementedError

    def unbuffered_gcodes(self, gcodes):
        raise NotImplementedError

    def cancel(self):
        self.parent.register_error(605, "Cancel is not supported for this printer type", is_blocking=False)
        return False

    def preprocess_gcodes(self, gcodes):
        gcodes = gcodes.replace("\r", "")
        gcodes = gcodes.split("\n")
        gcodes = filter(lambda item: item, gcodes)
        if gcodes:
            while gcodes[-1] in ("\n", "\n", "\t", " ", "", None):
                line = gcodes.pop()
                self.logger.info("Removing corrupted line '%s' from gcodes tail" % line)
        self.logger.info('Got %d gcodes to print.' % len(gcodes))
        return gcodes

    def gcodes(self, gcodes):
        is_base64_re = re.compile("^([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{4}|[A-Za-z0-9+/]{3}=|[A-Za-z0-9+/]{2}==)$")
        start_time = time.time()
        self.logger.debug("Determining gcode format")
        if is_base64_re.match(gcodes):
            gcodes = base64.b64decode(gcodes)
            self.unbuffered_gcodes(gcodes)
        else:
            self.logger.debug("Start loading gcodes. Determination time:" + str(time.time()-start_time))
            self.load_gcodes(gcodes)
            self.logger.debug("Done loading gcodes. Time:" + str(time.time()-start_time))

    def set_filename(self, filename):
        self.filename = str(filename) if filename else None

    def get_position(self):
        return self.position

    def get_temps(self):
        return self.temps

    def get_target_temps(self):
        return self.target_temps

    def pause(self):
        self.pause_flag = True

    def unpause(self):
        self.pause_flag = False

    def close(self):
        self.stop_flag = True

    def is_paused(self):
        return self.pause_flag

    def is_operational(self):
        return False

    def get_downloading_percent(self):
        return self.parent.downloader.get_percent()
