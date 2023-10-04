# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019.
# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)

import logging
import time

class Awaitable:

    CHECK_PERIOD = 0.1
    NAME = 'awaitable'

    def __init__(self, parent):
        if not getattr(self, "check_function", None):
            raise AssertionError("Error: No check_function for delivered from class Awaitable!")
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel('INFO')
        self.parent = parent
        self.ignore_flag = False
        self.waiting = not self.check_function()

    def _check_function(self):
        NotImplemented

    def check_function(self):
        return self._check_function()

    def __str__(self):
        return self.NAME

    def wait(self):
        if self.waiting:
            self.logger.info('Waiting for an action: ' + self.NAME)
            while not self.check_function() and not self.ignore_flag and not self.parent.stop_flag:
                time.sleep(self.CHECK_PERIOD)
            self.waiting = False
            self.logger.info('...end of waiting for: ' + self.NAME)
        return not self.parent.stop_flag

