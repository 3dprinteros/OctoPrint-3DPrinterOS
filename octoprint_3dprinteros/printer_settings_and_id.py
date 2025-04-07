import os

import json
import logging
import time

import config
import paths
import log


ID_SEPARATOR = "_"

ID_FIELDS_NAMES = ('VID', 'PID', 'SNR')


class PrinterID(dict):

    def __init__(self, vid, pid, snr, ip=None):
        self['VID'] = vid
        self['PID'] = pid
        self['SNR'] = snr
        if ip:
            self['IP'] = ip
        self['enabled'] = Tru

    def __eq__(self, other):
        if not isinstance(other, dict):
            raise TypeError
        for field_name in ID_FIELDS_NAMES:
            if self[field_name] != other[field_name]:
                return False
        return True


def create_id_string(usb_info: dict) -> str:
    try:
        id_string = ""
        for char in usb_info['VID'] + ID_SEPARATOR + \
                    usb_info['PID'] + ID_SEPARATOR + \
                    usb_info['SNR']:
            if char.isalnum():
                id_string += char
            else:
                id_string += ID_SEPARATOR
    except:
        id_string = "invalid_printer_id"
    return id_string


def load_settings(id_string, retries=3) -> dict:
    logger = logging.getLogger(id_string + ".settings")
    settings = {}
    settings_path = os.path.join(paths.PRINTER_SETTINGS_FOLDER, id_string + ".json")
    try:
        with open(settings_path) as f:
            settings = json.loads(f.read())
            logger.info('Settings loaded: ' + str(settings))
    except FileNotFoundError:
        pass
    except json.decoder.JSONDecodeError:
        logger.error(f'Unable to load printer settings for {id_string}')
        try:
            os.remove(settings_path)
            logger.error('Removing invalid settings file: {settings_path}')
        except OSError:
            pass
    except Exception as e:
        retries -= 1
        if retries:
            time.sleep(0.1)
            return load_settings(id_string, retries)
        logger.error(f'Error loading settings')
    return settings


def save_settings(id_string, settings, retries=3) -> bool:
    logger = logging.getLogger(id_string + ".settings")
    try:
        path = os.path.join(paths.PRINTER_SETTINGS_FOLDER, id_string + ".json")
        paths.check_and_create_dirs(path)
        with open(path, "w") as f:
            f.write(json.dumps(settings))
            logger.info('Settings saved: ' + str(settings))
            return True
    except OSError as e:
        retries -= 1
        if retries:
            time.sleep(0.05)
            return save_settings(id_string, settings, retries)
        logger.info(f'Error saving settings: {settings}\n{e}')
        return False


def reset_printer_settings(id_string):
    return save_settings(id_string, {})


# def save_printer_settings_by_id_dict(usb_info_dict, settings) -> bool:
#     return save_settings(create_id_string(usb_info_dict), settings)


# def load_printer_settings_by_id_dict(usb_info_dict) -> dict:
#     return load_settings(create_id_string(usb_info_dict))
