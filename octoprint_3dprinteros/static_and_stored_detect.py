import json
import logging
import os
import threading

import config
import paths
from base_detector import BaseDetector


class StaticDetector(BaseDetector):

    STORAGE_FILEPATH = os.path.join(paths.CURRENT_SETTINGS_FOLDER, 'static_printers.json')
    STORAGE_CONFIG_SECTION = 'static_printers'
    DEFAULT_SNR = ''

    @staticmethod
    def is_same_printer(checking_dict, checked_dict): #TODO rename args in all other classes
        for key in StaticDetector.PRINTER_ID_DICT_KEYS:
            checking_value = checking_dict.get(key)
            checked_value = checked_dict.get(key)
            if checking_value and checking_value != checked_value:
                return False
        return True

    @staticmethod
    def prepare_and_filter(printer_list):
        enabled_printers = []
        for printer in printer_list:
            enabled = printer.get('enabled')
            if enabled is None:
                enabled_printers.append(printer)
                if not printer.get('SNR'):
                    printer['SNR'] = StaticDetector.DEFAULT_SNR
            elif enabled:
                printer_without_enabled = dict(printer)
                del printer_without_enabled['enabled']
                if not printer_without_enabled.get('SNR'):
                    printer_without_enabled['SNR'] = StaticDetector.DEFAULT_SNR
                enabled_printers.append(printer_without_enabled)
        return enabled_printers

    def __init__(self, _):
        self.logger = logging.getLogger(__class__.__name__)
        self.lock = threading.RLock()
        self.printers_from_config = self.load_from_config()
        self.printers_from_file = self.load_from_file()
        if self.printers_from_config:
            self.logger.info(f'Static printers: {self.printers_from_config}')
            self.logger.info(f'Loaded printers: {self.printers_from_file}')

    def load_from_config(self, section_name=None):
        if not section_name:
            section_name = self.STORAGE_CONFIG_SECTION
        try:
            return list(config.get_settings().get(section_name, {}))
        except:
            self.logger.warning('Invalid static printers section')
            return []

    def save_to_config(self, section_name=None):
        if not section_name:
            section_name = self.STORAGE_CONFIG_SECTION
        settings = config.get_settings()
        settings[section_name] = self.printers_from_config
        config.Config.instance().save_settings(settings)

    def add_printer(self, printer, to_config=False, save=True):
        with self.lock:
            if to_config:
                printers_list = self.printers_from_config 
            else:
                printers_list = self.printers_from_file 
            if printer not in printers_list:
                self.logger.info(f'Adding printer: {printer}')
                for existings_printer in printers_list:
                    if self.is_same_printer(printer, existings_printer):
                        existings_printer.update(printer)
                        break
                else:
                    printers_list.append(printer)
            if save:
                if to_config:
                    self.save_to_config()
                else:
                    self.save_to_file()

    def load_from_file(self, filepath=None):
        with self.lock:
            if not filepath:
                filepath = self.STORAGE_FILEPATH
            if os.path.isfile(filepath):
                try:
                    with open(filepath) as f:
                        return json.load(f)
                except Exception:
                    self.logger.exception('Exception on load of stored printers from: ' + str(filepath))
                    os.remove(filepath)
            else:
                self.logger.debug(f'No file {filepath}. That is OK')
            return []

    def save_to_file(self, printers=None, filepath=None):
        with self.lock:
            if not printers:
                printers = self.printers_from_file
            if not filepath:
                filepath = self.STORAGE_FILEPATH
            try:
                with open(filepath, 'w') as f:
                    json.dump(printers, f, indent = 4, separators = (',', ': '), sort_keys = True)
                    return True
            except Exception:
                self.logger.exception('Exception on save of stored printers from: ' + str(filepath))
            return False


    def get_printers_list(self):
        with self.lock:
            self.logger.debug(f'Static printers: {self.printers_from_config}')
            self.logger.debug(f'Saved printers: {self.printers_from_file}')
            return self.prepare_and_filter(self.printers_from_config) + \
                    self.prepare_and_filter(self.printers_from_file)

    # def without_enabled(self, printer):
    #     printer_without_enabled = {}
    #     printer_without_enabled.update(printer)
    #     if "enabled" in printer_without_enabled:
    #         del printer["enabled"]
    #     return printer_without_enabled

    def remove_printer(self, printer, allow_remove_from_config=False):
        with self.lock:
            for stored_printer in self.printers_from_file:
                if printer.get('SNR') == stored_printer.get('SNR'):
                    if printer['PID'] == stored_printer['PID'] and printer['VID'] == stored_printer['VID']:
                        self.printers_from_file.remove(stored_printer)
                        self.save_to_file(self.printers_from_file)
                        return True
            if allow_remove_from_config:
                for stored_printer in self.printers_from_config:
                    if printer['PID'] == stored_printer['PID'] and printer['VID'] == stored_printer['VID']:
                        if printer.get('SNR') == stored_printer.get('SNR'):
                            self.printers_from_config.remove(stored_printer)
                            self.save_to_config(self.printers_from_config)
                            return True
        return False

    def remove_all(self, save=False):
        with self.lock:
            self.printers_from_config.clear()
            if save:
                self.save_to_config()
            self.printers_from_file.clear()
            if save:
                self.save_to_file()

    def edit_printer(self, printer, allow_edit_in_config=False):
        with self.lock:
            for stored_printer in self.printers_from_file:
                if printer.get('SNR') == stored_printer.get('SNR'):
                    if printer['PID'] == stored_printer['PID'] and printer['VID'] == stored_printer['VID']:
                        stored_printer.update(printer)
                        self.save_to_file(self.printers_from_file)
                        return True
            if allow_edit_in_config:
                for stored_printer in self.printers_from_config:
                    if printer['PID'] == stored_printer['PID'] and printer['VID'] == stored_printer['VID']:
                        if printer.get('SNR') == stored_printer.get('SNR'):
                            stored_printer.update(printer)
                            self.save_to_config(self.printers_from_config)
                            return True
        return False


if __name__ == "__main__":
    print(StaticDetector(None).get_printers_list())
