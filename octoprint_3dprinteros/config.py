# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019.
# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)

import os
import json
import threading

import paths

REMOTE_IP = ""
LOCAL_IP = "127.0.0.1"


def get_settings():
    return Config.instance().settings


def get_profiles():
    return Config.instance().profiles


def get_app():
    return Config.instance().app


def get_last_exception():
    return Config.instance().last_exception_text


def set_last_exception(text):
    Config.instance().last_exception_text = text


def get_error_reports():
    return [pi.errors for pi in get_app().printer_interfaces]


def merge_dictionaries(base_dict, update_dict, overwrite = False):
    result_dict = {}
    result_dict.update(base_dict)
    if not update_dict:
        return dict(base_dict) # a copy
    for key, value in list(update_dict.items()):
        value_to_update = result_dict.get(key)
        if type(value_to_update) != type(value):
            result_dict[key] = value
        elif isinstance(value_to_update, dict) and isinstance(value, dict):
            result_dict[key] = merge_dictionaries(value_to_update, value, overwrite=overwrite)
        elif isinstance(value_to_update, list) and isinstance(value, list):
            # remove non unique items
            result_dict[key] = value_to_update + [item for item in value if item not in value_to_update]
        elif overwrite and value_to_update != value:
            result_dict[key] = value
    #print("Result dictionary:", result_dict)
    return result_dict


class Singleton:

    lock = threading.Lock()
    _instance = None

    @classmethod
    def instance(cls, **kwargs):
        with cls.lock:
            if not cls._instance:
                #print("Creating new instance of " + cls.__name__)
                cls._instance = cls(**kwargs)
        return cls._instance


class Config(Singleton):

    DEFAULT_SETTINGS_FILE = os.path.join(paths.APP_FOLDER, 'default_settings.json')
    USER_SETTINGS_FILE = os.path.join(paths.CURRENT_SETTINGS_FOLDER, 'user_settings.json')
    DEFAULT_PRINTER_PROFILES_FILE = os.path.join(paths.APP_FOLDER, 'default_printer_profiles.json')

    def __init__(self, patch={}):
        self.settings = {}
        self.settings_lock = threading.RLock()
        self.patch = patch
        self.init_settings()
        self.profiles = self.load_default_printer_profiles()
        self.last_exception_text = ""
        self.app = None

    def init_settings(self):
        settings = self.load_settings(self.DEFAULT_SETTINGS_FILE)
        user_settings = self.load_settings(self.USER_SETTINGS_FILE, warnings = False)
        if user_settings:
            settings = merge_dictionaries(user_settings, settings)
        if settings != user_settings:
            self.save_settings(settings, self.USER_SETTINGS_FILE)
        self.settings = settings
        self.settings.update(self.patch)

    def load_settings(self, settings_path=None, warnings=True):
        if not settings_path:
            settings_path=self.USER_SETTINGS_FILE
        try:
            with open(settings_path) as settings_file:
                settings = json.load(settings_file)
        except Exception as e:
            if warnings:
                print("Error reading config from %s: %s" % (settings_path, str(e)))
        else:
            return settings

    def save_settings(self, settings, settings_path=None):
        if not settings_path:
            settings_path=self.USER_SETTINGS_FILE
        try:
            with open(settings_path, 'w') as settings_file:
                json_config = json.dumps(settings, sort_keys = True, indent = 4, separators = (',', ': '))
                settings_file.write(json_config)
        except Exception as e:
            print("Error writing config to %s: %s" % (settings_path, str(e)))
        else:
            self.settings = settings
            print("Settings are successfully updated")

    def update_settings(self, update_dict):
        print("Settings update:", update_dict)
        new_settings = merge_dictionaries(self.load_settings(), update_dict, overwrite = True)
        self.save_settings(new_settings)

    def restore_default_settings(self):
        if os.path.exists(self.USER_SETTINGS_FILE):
            try:
                os.remove(self.USER_SETTINGS_FILE)
            except Exception as e:
                return str(e)
        self.settings = self.load_settings(self.DEFAULT_SETTINGS_FILE)

    def set_profiles(self, profiles):
        self.profiles = profiles

    def set_app_pointer(self, app):
        self.app = app

    def load_default_printer_profiles(self, warnings=True):
        try:
            with open(self.DEFAULT_PRINTER_PROFILES_FILE) as profiles_file:
                profiles = json.load(profiles_file)
        except Exception as e:
            if warnings:
                print("Error reading config from %s: %s" % (self.DEFAULT_PRINTER_PROFILES_FILE, str(e)))
        else:
            return profiles

    def save_default_printer_profiles(self, profiles):
        try:
            with open(self.DEFAULT_PRINTER_PROFILES_FILE, "w") as profiles_file:
                json_profiles = json.dumps(profiles, sort_keys = True, indent = 4, separators = (',', ': '))
                profiles_file.write(json_profiles)
        except Exception as e:
            print("Error writing profiles: %s" % str(e))
    
    def get_settings_as_text(self):
        return json.dumps(self.settings, sort_keys = True, indent = 4, separators = (',', ': '))

    def set_settings_as_text(self, settings_text):
        try:
            settings = json.loads(settings_text)
        except Exception as e:
            return str(e)
        else:
            self.save_settings(settings)


if __name__ == "__main__":
    from pprint import pprint
    pprint(get_profiles())
    pprint("#"*80)
    pprint(get_settings())
