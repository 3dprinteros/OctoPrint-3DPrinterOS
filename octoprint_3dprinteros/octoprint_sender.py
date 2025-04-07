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

import collections
import io
import os
import time
import pprint

from base_sender import BaseSender
from octoprint.filemanager import FileDestinations
from octoprint.filemanager.util import DiskFileWrapper


class Sender(BaseSender):

    FOLDER_NAME = "3dprinteros"
    FILE_NAME = "3dprinteros.gcode"
    OCTOPRINT_UPDATE_PERIOD = 2
    TEMP_NAMES = ('bed', 'tool0', 'tool1')

    def __init__(self, parent, usb_info, profile):
        BaseSender.__init__(self, parent, usb_info, profile)
        self.temps = [0.0, 0.0, 0.0]
        self.target_temps = [0.0, 0.0, 0.0]
        self.parent = parent
        self.app = parent.app
        self.octo_printer = self.app.owner.get_octo_printer()
        self.file_manager = self.app.owner.get_octo_file_manager()
        self.last_octoprint_status_update_time = 0.0
        self.last_octoprint_temps_update_time = 0.0
        self.octoprint_status_dict = self.octo_printer.get_current_data()
        self.update_octoprint_temperatures()
        self.file_pos = 0
        self.remove_print_file()

    def gcodes(self, path):
        if not self.is_operational():
            self.parent.register_error(604, "Printer is not ready.", is_blocking=False)
        elif self.is_printing():
            self.parent.register_error(604, "Printer already printing.", is_blocking=False)
            return False
        else:
            self.percent = 0.0
            octo_path = self.file_manager.add_folder(FileDestinations.LOCAL, self.FOLDER_NAME, ignore_existing=True)
            octo_path = self.file_manager.join_path(FileDestinations.LOCAL, octo_path, self.FILE_NAME)
            path = self.file_manager.add_file(FileDestinations.LOCAL, octo_path, DiskFileWrapper(os.path.split(path)[-1], path, move=True), allow_overwrite=True, analysis={})
            if not path:
                self.parent.register_error(604, "Some error on file upload to OctoPrint", is_blocking=True)
                return False
            self.logger.info("File transfer to printer successful")
            path = self.file_manager.path_on_disk(FileDestinations.LOCAL, path)
            self.octo_printer.select_file(path, False, printAfterSelect=True)
            self.logger.info("File was selected and print was started")
            return True

    def unbuffered_gcodes(self, gcodes):
        self.logger.info("Gcodes to send now: " + str(gcodes))
        try:
            if type(gcodes) == bytes:
                gcodes = gcodes.decode(errors='ignore').split('\n')
            elif type(gcodes) == str:
                gcodes = gcodes.split('\n')
            else:
                if isinstance(gcodes, collections.deque):
                    gcodes = list(gcodes)
                if isinstance(gcodes, list):
                    for index, gcode in enumerate(gcodes):
                        if type(gcodes) == bytes:
                            gcodes[index] = gcode.decode(errors='ignore')
                        elif type(gcode) != str:
                            raise TypeError(f'Gcode format not supported type:{type(gcode)}. Gcode: {gcode}')
                else:
                    raise TypeError('Gcode format not supported {type(gcodes)}')
        except Exception as e:
            self.logger.exception(e)
            return False
        self.logger.info("Gcodes to send now(processed): " + str(gcodes))
        self.octo_printer.command(gcodes)
        self.logger.info("Gcodes were sent to printer")
        return True

    def pause(self):
        if not self.is_paused():
            self.octo_printer.pause_print()
            return True
        return False

    def unpause(self):
        if self.is_paused():
            self.octo_printer.resume_print()
            return True
        return False

    def cancel(self):
        self.octo_printer.cancel_print()
        self.logger.info('Cancelled!')
        return True

    def is_operational(self):
        if self.octo_printer:
            return self.octo_printer.is_operational()
        return False

    def is_paused(self):
        if self.octo_printer:
            return self.octo_printer.is_paused()
        return False

    def is_printing(self):
        if self.octo_printer:
            return self.octo_printer.is_printing() or self.is_paused()
        return False

    def get_percent(self):
        if self.is_printing():
            percent = self.get_current_data().get('progress', {}).get('completion')
            if percent is not None:
                self.percent = round(float(percent), 2)
        return self.percent

    def get_current_line_number(self):
        if self.is_printing():
            file_pos = self.get_current_data().get('progress', {}).get('filepos')
            if file_pos is not None:
                self.file_pos = int(file_pos)
        else:
            self.file_pos = 0
        return self.file_pos

    def get_time_left(self):
        if self.is_printing():
            print_time_left = self.get_current_data().get('progress', {}).get('printTimeLeft')
            if print_time_left is not None:
                self.print_time_left = int(print_time_left)
        else:
            self.print_time_left = 0
        self.est_print_time = self.print_time_left
        return self.print_time_left

    def update_octoprint_temperatures(self):
        now = time.monotonic()
        if self.octo_printer:
            if now - self.last_octoprint_temps_update_time > self.OCTOPRINT_UPDATE_PERIOD:
                octo_temps_dict = self.octo_printer.get_current_temperatures()
                self.logger.debug('Octo temps: ' + pprint.pformat(octo_temps_dict))
                self.last_octoprint_temps_update_time = now
                if 'bed' in octo_temps_dict:
                    bed_dict = octo_temps_dict.get('bed', {})
                    self.temps[0] = round(bed_dict.get('actual', 0.0), 2)
                    self.target_temps[0] = round(bed_dict.get('target', 0.0), 2)
                if 'tool0' in octo_temps_dict:
                    self.temps[1] = round(octo_temps_dict['tool0']['actual'], 2)
                    self.target_temps[1] = (octo_temps_dict['tool0']['target'], 2)
                if 'tool1' in octo_temps_dict:
                    if len(self.temps) == 2:
                        self.temps.append(0)
                        self.target_temps.append(0)
                    self.temps[2] = round(octo_temps_dict['tool1']['actual'], 2)
                    self.target_temps[2] = round(octo_temps_dict['tool1']['target'], 2)
                # self.last_octoprint_temps_update_time = now
                # for index, temp_name in enumerate(self.TEMP_NAMES):
                #     temp_dict = octo_temps_dict.get(temp_name)
                #     if temp_dict:
                #         self.temps[index] = temp_dict.get('actual', 0.0)
                #         self.target_temps[index] = temp_dict.get('target', 0.0)

    def get_current_data(self):
        self.update_octoprint_status()
        return self.octoprint_status_dict

    def get_temps(self):
        self.update_octoprint_temperatures()
        return self.temps

    def get_target_temps(self):
        self.update_octoprint_temperatures()
        return self.target_temps

    def get_remaining_print_time(self, ignore_state=False):
        return self.print_time_left

    def update_octoprint_status(self):
        now = time.monotonic()
        if now - self.last_octoprint_status_update_time > self.OCTOPRINT_UPDATE_PERIOD:
            self.last_octoprint_status_update_time = now
            self.octoprint_status_dict = self.octo_printer.get_current_data()
            #class octoprint.printer.PrinterCallback
            #on_printer_send_current_data(data)
            #on_printer_add_temperature(data)

    def remove_print_file(self):
        path = self.file_manager.join_path(FileDestinations.LOCAL, Sender.FOLDER_NAME, Sender.FILE_NAME)
        job = self.octo_printer.get_current_job()
        if job and isinstance(job, dict) and job.get('file', {}).get('path') == path:
            self.logger.info('Removing print file' + Sender.FILE_NAME)
            self.octo_printer.unselect_file()
            self.logger.info('Print file unselected')
        self.file_manager.remove_file(FileDestinations.LOCAL, path)
        self.logger.info(f'Removed file: {path}')
