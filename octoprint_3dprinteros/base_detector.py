#!/usr/bin/env python
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

class BaseDetector:

    CONFLICTS = []

    PRINTER_ID_DICT_KEYS = ('VID', 'PID', 'SNR')
    PRINTER_NONID_KEYS = ('COM', 'IP', 'PORT', 'PRT', 'PASS', 'SSHP', 'OFF')
    ALL_KEYS = PRINTER_NONID_KEYS + PRINTER_NONID_KEYS
    HIDDEN_PASSWORD_MASK = "_password_hidden_"

    CAN_DETECT_SNR = False

    @staticmethod
    def format_vid_or_pid(vid_or_pid): #cuts "0x", fill with zeroes if needed, doing case up
        if vid_or_pid:
            return hex(vid_or_pid)[2:].zfill(4).upper()

    @staticmethod
    def is_same_printer(printer, other_printer):
        if printer['VID'] == other_printer['VID'] and printer['PID'] == other_printer['PID']:
            if printer['SNR']:
                if printer['SNR'] == other_printer['SNR']:
                    return True
            elif other_printer.get('COM'): # if two printers with have vid and pid, but one lacks COM, then it is same print but from pyusb(bug of different PRTs on mswin)
                if not printer.get('COM') or printer.get('COM') == other_printer['COM']:
                    return True
            elif printer.get('PRT') and other_printer.get('PRT'):
                if printer['PRT'] == other_printer['PRT']:
                    if not printer['SNR'] or printer['SNR'] == other_printer['SNR']:
                        return True
            else: # No SNR, PRT or COM so we got only VID and PID which are the same
                return True
        return False

    def __init__(self, _=None):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.devices_list = []

    def get_printers_list(self):
        return self.devices_list

    def close(self):
        pass
