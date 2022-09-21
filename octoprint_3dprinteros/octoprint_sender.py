#!/usr/bin/env python
# -*- coding: utf-8 -*-
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

import io

# import log
from base_sender import BaseSender
from octoprint.filemanager import FileDestinations
from octoprint.filemanager.util import StreamWrapper


class Sender(BaseSender):

    FOLDER_NAME = "3dprinteros"
    FILE_NAME = "3dprinteros.gcode"

    def __init__(self, parent, usb_info, profile):
        BaseSender.__init__(self, parent, usb_info, profile)
        self.printer = self.parent.get_octo_printer()
        self.file_manager = self.parent.get_octo_file_manager()
        self.paused = False
        self.cloud_printing_flag = False
        self.cloud_printing_init_flag = False
        self.percent = 0
        self.current_data = None
        self.file_pos = 0
        self.print_time_left = 0
        # self.printer.commands()

    def load_gcodes(self, gcodes):
        # if self.cloud_printing_init_flag
        if self.is_operational() and not self.is_printing():
            return self.upload_gcodes_and_print(gcodes)
        else:
            self.parent.register_error(604, "Printer already printing.", is_blocking=False)
            return False

    # def unbuffered_gcodes(self, gcodes):
    #     self.logger.info("Gcodes to send now: " + str(gcodes))
    #     for gcode in self.preprocess_gcodes(gcodes):
    #         self.send_and_wait_for(gcode, "ok")
    #     self.logger.info("Gcodes were sent to printer")

    def upload_gcodes_and_print(self, gcodes):
        self.percent = 0
        self.cloud_printing_init_flag = True

        path = self.file_manager.add_folder(FileDestinations.LOCAL, self.FOLDER_NAME)
        path = self.file_manager.join_path(FileDestinations.LOCAL, path, self.FILE_NAME)
        res = self.file_manager.add_file(FileDestinations.LOCAL, path, StreamWrapper(path, io.BytesIO(gcodes)),
                                         allow_overwrite=True, analysis={})
        if not res:
            self.parent.register_error(604, "Some error on file upload to OctoPrint", is_blocking=True)
            return False
        self.logger.info("File transfer to printer successful")

        # if self._printer.is_closed_or_error():
        #     self._printer.disconnect()
        #     self._printer.connect()

        # meta = self.file_manager.get_metadata(FileDestinations.LOCAL, path)
        # print "file print", path, meta
        path = self.file_manager.path_on_disk(FileDestinations.LOCAL, path)
        self.printer.select_file(path, False, printAfterSelect=True)
        self.logger.info("File was selected and print was started")
        # self.cloud_printing_flag = True

        # if self.send_and_wait_for("M29", "ok", timeout=10000)[0]:
        #     ok, answer = self.send_and_wait_for("M23 0:/user/%s" % self.FILE_NAME, "ok", timeout=10000)
        #     if not answer:
        #         message = "Printer not answered anything after send M23"
        #     elif "Disk read error" in answer:
        #             message = "Disk read error"
        #     elif "File selected" in answer:
        #         self.logger.info("File transfer to printer successful")
        #         self.printing_flag = True
        #         self.printing_started_flag = True
        #         time.sleep(2)  # to double-protect from 'Cancelled manually' on printing start
        #         self.monitoring_thread_start()
        #         return True
        # self.parent.register_error(604, message, is_blocking=True)
        self.cloud_printing_init_flag = False
        self.cloud_printing_flag = True
        return True

    def pause(self):
        if not self.is_paused():
            self.printer.pause_print()
            return True
        return False

    def unpause(self):
        if self.is_paused():
            self.printer.resume_print()
            return True
        return False

    def cancel(self):
        self.printer.cancel_print()
        self.cloud_printing_flag = False
        self.logger.info('Cancelled!')
        return True
        # self.logger.info('Cancel error')
        # return False

    def is_operational(self):
        return self.printer.is_operational()

    def is_paused(self):
        return self.printer.is_paused()

    def is_printing(self):
        return self.printer.is_printing() or self.is_paused()

    def get_percent(self):
        if self.is_printing():
            percent = self.get_current_data()['progress']['completion']
            if percent is not None:
                self.percent = int(percent)
        return self.percent

    def get_current_line_number(self):
        if self.is_printing():
            file_pos = self.get_current_data()['progress']['filepos']
            if file_pos is not None:
                self.file_pos = int(file_pos)
        else:
            self.file_pos = 0
        return self.file_pos

    def get_time_left(self):
        if self.is_printing():
            print_time_left = self.get_current_data()['progress']['printTimeLeft']
            if print_time_left is not None:
                self.print_time_left = int(print_time_left)
        else:
            self.print_time_left = 0
        return self.print_time_left

    def update_temps(self):
        ctemps = self.printer.get_current_temperatures()
        if 'bed' in ctemps:
            self.temps[0] = ctemps['bed']['actual']
            self.target_temps[0] = ctemps['bed']['target']
        if 'tool0' in ctemps:
            self.temps[1] = ctemps['tool0']['actual']
            self.target_temps[1] = ctemps['tool0']['target']
        if 'tool1' in ctemps:
            if len(self.temps) == 2:
                self.temps.append(0)
                self.target_temps.append(0)
            self.temps[2] = ctemps['tool1']['actual']
            self.target_temps[2] = ctemps['tool1']['target']
        return

    def get_current_data(self, refresh_from_octo=False):
        if refresh_from_octo and self.printer:
            self.current_data = self.printer.get_current_data()
        return self.current_data

    def get_temps(self):
        return self.temps

    def get_target_temps(self):
        return self.target_temps

    def octo_cancel(self):
        if self.cloud_printing_flag:
            self.cloud_printing_flag = False
            self.parent.register_error(607, "Cancelled manually", is_blocking=True)

    def octo_failed(self):
        if self.cloud_printing_flag:
            self.cloud_printing_flag = False
            self.parent.register_error(610, "Print failed. Some OctoPrint error", is_blocking=True)

    def octo_done(self):
        if self.cloud_printing_flag:
            self.cloud_printing_flag = False
            if self.percent > 0:
                self.percent = 100

    def octo_error(self, msg):
        self.parent.register_error(611, "OctoPrint error: %s" % msg)
