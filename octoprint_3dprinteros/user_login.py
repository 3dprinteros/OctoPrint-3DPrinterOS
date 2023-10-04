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
import paths
import logging
import marshal
import time
import threading

import config
from http_client import HTTPClient, HTTPClientPrinterAPIV1
from awaitable import Awaitable


class UserLogin(Awaitable):

    NAME = 'user login'
    STORAGE_PATH = os.path.join(paths.CURRENT_SETTINGS_FOLDER, 'stuff.bin')
    DEFAULT_PRINTER_PROFILES_PATH = os.path.join(os.path.dirname(__file__), 'default_printer_profiles.json')
    USER_PRINTER_PROFILES_PATH = os.path.join(paths.CURRENT_SETTINGS_FOLDER, 'printer_profiles.json')
    KEY = b"3dprinteros_used_to_store_password_hashes_in_almost_plain_text_but_no_more!"*3
    APIPRINTER_MODE = 'apiprinter'
    STREAMERAPI_MODE = 'streamerapi'

    @staticmethod
    def load_stuffbin(): #TODO rename this to show that auth_tokens can be read here too
        try:
            with open(UserLogin.STORAGE_PATH, "rb") as f:
                return marshal.load(f)
        except FileNotFoundError:
            pass
        except:
            return UserLogin.load_new_format_stuff_bin()
    
    @staticmethod
    def load_new_format_stuff_bin():
        try:
            with open(UserLogin.STORAGE_PATH, "rb") as f:
                data = UserLogin.endecrypt_stuffbin(f.read())
                return json.loads(data)
        except FileNotFoundError:
            pass
        except (IOError, OSError, ValueError, TypeError) as e:
            logger = logging.getLogger(__name__)
            logger.info("Can't read or decode login storage file: " + str(e))
            try:
                UserLogin.logout()
            except:
                pass

    @staticmethod
    def endecrypt_stuffbin(data):
        return bytes([x ^ y for x, y in zip(data, UserLogin.KEY)])

    @staticmethod
    def validate_login_password(login, password):
        for cred_str in (login, password):
            if type(cred_str) != str:
                login = None
                password = None
                logger = logging.getLogger(__name__)
                logger.warning('Invalid login format in stuff.bin. Assuming corrupted file. Removing.')
                UserLogin.logout()
                break
        return login, password

    @staticmethod
    def load_login():
        decoded_data = UserLogin.load_stuffbin()
        if decoded_data == None: # to prevent logout and warnings when no save login file exists
            return None, None
        elif type(decoded_data) in (tuple, list) and len(decoded_data) > 1:
            login, password = decoded_data[:2]
            if type(login) == bytes:
                login = login.decode(errors='ignore', encoding='latin1')
            if type(password) == bytes:
                password = password.decode(errors='ignore', encoding='latin1')
        elif type(decoded_data) == dict:
            login = decoded_data.get('login')
            password = decoded_data.get('password')
        else:
            login, password = None, None
        return UserLogin.validate_login_password(login, password)

    @staticmethod
    def save_login(login, password):
        data = json.dumps({'login': login, 'password': password})
        UserLogin.save_data(UserLogin.endecrypt_stuffbin(data.encode('utf-8')))

    @staticmethod
    def save_data(data): #login storage with some rational paranoia
        logger = logging.getLogger(__name__)
        new_storage_file = os.path.join(paths.CURRENT_SETTINGS_FOLDER, 'new_stuff.bin')
        try:
            with open(new_storage_file, "wb") as f:
                f.write(data)
        except (IOError, OSError) as e:
            logger.warning('Error writing login to storage file: ' + str(e))
        else:
            try:
                os.rename(new_storage_file, UserLogin.STORAGE_PATH)
                return True
            except (IOError, OSError) as e:
                logger.warning('Error renaming login to storage file: ' + str(e))
                try:
                    os.remove(UserLogin.STORAGE_PATH)
                except OSError:
                    pass
                try:
                    os.rename(new_storage_file, UserLogin.STORAGE_PATH)
                    return True
                except (IOError, OSError) as e:
                    logger.warning('Error renaming login to storage file even after removal of some logs: ' + str(e))
        return False

    @staticmethod
    def logout():
        logger = logging.getLogger(__name__)
        settings = config.get_settings()
        if settings.get('offline_mode', False):
            logger.info("Turning offline mode off in the settings")
            settings['offline_mode'] = False
            config.Config.instance().save_settings(settings)
            return True
        else:
            logger.info("Removing login file")
            try:
                os.remove(UserLogin.STORAGE_PATH)
            except FileNotFoundError:
                logger.info("No login file to remove")
            else:
                logger.info("Login file removed")
                return True
        return False

    @staticmethod
    def apply_settings_mod(settings_mod):
        current_settings = config.get_settings()
        new_settings = config.merge_dictionaries(current_settings, settings_mod, overwrite=True)
        config.Config.instance().settings = new_settings
        logger = logging.getLogger(__name__)
        logger.info("Setting modifications:\n" + str(new_settings))

    @staticmethod
    def load_local_printer_profiles():
        profiles = []
        for path in (UserLogin.DEFAULT_PRINTER_PROFILES_PATH, UserLogin.USER_PRINTER_PROFILES_PATH):
            try:
                with open(path) as f:
                    profiles = UserLogin.merge_profiles(profiles, json.load(f))
            except FileNotFoundError:
                pass
            except Exception as e:
                logger = logging.getLogger(__name__)
                logger.warning("Error loading profile file %s: %s" % (path, str(e)))
        return profiles

    @staticmethod
    def save_profiles(profiles):
        try:
            profiles = json.dumps(profiles, sort_keys = True, indent = 4, separators = (',', ': '))
            with open(UserLogin.USER_PRINTER_PROFILES_PATH, "w") as f:
                f.write(profiles)
        except (OSError, ValueError) as e:
            logger = logging.getLogger(__name__)
            logger.warning("Error saving profile file %s: %s" % (profiles, str(e)))

    @staticmethod
    def merge_profiles(base_profiles, updating_profiles):
        base_profiles_dict = {}
        for profile in base_profiles:
            base_profiles_dict[profile.get('alias', "Unknown")] = profile
        updating_profiles_dict = {}
        if updating_profiles:
            for profile in updating_profiles:
                updating_profiles_dict[profile.get('alias', "Unknown")] = profile
        return list(config.merge_dictionaries(base_profiles_dict, updating_profiles_dict, overwrite=True).values())

    @staticmethod
    def load_printer_auth_tokens():
        decoded_data = UserLogin.load_stuffbin()
        if type(decoded_data) == dict:
            auth_tokens = decoded_data.get('auth_tokens')
            if type(auth_tokens) == list:
                return auth_tokens
        return []

    def __init__(self, parent, auto_login=True, retry_in_background=False):
        self.logger = logging.getLogger(__class__.__name__)
        self.user_token = None
        self.parent = parent
        self.login = None
        self.macaddr = ''
        self.profiles = self.load_local_printer_profiles()
        self.auth_tokens = []
        self.login_lock = threading.RLock()
        self.retry_in_background = retry_in_background
        self.login_retry_thread = None
        self.got_network_connection = False
        self.http_connection = None
        if config.get_settings()['protocol']['user_login']:
            self.login_mode = self.STREAMERAPI_MODE
        else:
            self.login_mode = self.APIPRINTER_MODE
        if auto_login and self.parent and not self.parent.offline_mode:
            if retry_in_background:
                self.login_retry_thread = threading.Thread(target=self.retry_login_until_success)
                self.login_retry_thread.start()
            else:
                self.retry_login_until_success(exit_no_saved=True)
        Awaitable.__init__(self, parent)

    def login_from_saved_creds(self, exit_no_saved=False):
        with self.login_lock:
            if self.login_mode == self.APIPRINTER_MODE:
                if self.auth_tokens: #TODO is this check really necessary?
                    return True
                self.auth_tokens = self.load_printer_auth_tokens()
                self.logger.info("Getting profiles without user login")
                self.http_connection = HTTPClientPrinterAPIV1(self.parent, exit_on_fail=True)
                profiles = []
                if self.http_connection.connection:
                    self.got_network_connection = True
                    profiles = self.http_connection.pack_and_send(HTTPClientPrinterAPIV1.PRINTER_PROFILES)
                else:
                    self.got_network_connection = False
                self.http_connection.close()
                self.init_profiles(profiles)
                return True
            else:
                if self.user_token:
                    return True
                login, password = UserLogin.load_login()
                self.http_connection = HTTPClient(self.parent, exit_on_fail=True)
                error = None
                if self.http_connection.connection:
                    self.got_network_connection = True
                    if login:
                        error = self.login_as_user(login, password, save_password_flag=False)
                        if error:
                            self.logger.info(str(error))
                    self.http_connection.close()
                else:
                    self.got_network_connection = False
                    error = "No network"
                return not error or (exit_no_saved and not login) #return True when no token

    def login_as_user(self, login=None, password=None, disposable_token=None, save_password_flag=True):
        with self.login_lock:
            if not login:
                return 0, "Empty login"
            if password is None and not disposable_token:
                return 0, "Empty password"
            if not self.http_connection:
                self.http_connection = HTTPClient(self.parent, exit_on_fail=True)
            answer = self.http_connection.pack_and_send(HTTPClient.USER_LOGIN, login, password, disposable_token=disposable_token)
            if answer:
                user_token = answer.get('user_token')
                profiles_str = answer.get('all_profiles')
                settings_mod = answer.get('settings_mod')
                if settings_mod:
                    self.apply_settings_mod(settings_mod)
                error = answer.get('error', None)
                if error:
                    self.logger.warning("Error processing user_login " + str(error))
                    self.logger.error("Login rejected")
                    code, message = error['code'], error['message']
                    if code == 3:
                        if self.logout() and self.parent:
                            self.logger.info("Initiating application restart")
                            self.parent.restart_flag = True
                            self.parent.stop_flag = True
                    return code, message
                if login and save_password_flag:
                    self.save_login(login, password)
                try:
                    profiles = json.loads(profiles_str)
                    if not profiles:
                        raise RuntimeError("Server returned empty profiles on user login")
                except Exception as e:
                    self.user_token = user_token
                    self.logger.warning("Error while parsing profiles: " + str(e))
                    self.init_profiles()
                    return 42, "Error parsing profiles"
                else:
                    self.macaddr = self.http_connection.host_id
                    self.user_token = user_token
                    if login:
                        self.login = login
                    else:
                        login_name = answer.get('user_login')
                        if login_name:
                            self.login = login_name
                        else:
                            self.login = "Temporary login"
                    self.init_profiles(profiles)
                    self.logger.info("Successful login from user %s" % self.login)

    def save_printer_auth_token(self, usb_info=None, auth_token=None):
        if usb_info and auth_token:
            self.auth_tokens.append((usb_info,  auth_token))
        data = json.dumps({'auth_tokens': self.auth_tokens})
        return self.save_data(self.endecrypt_stuffbin(data.encode('utf-8')))

    def forget_auth_tokens(self):
        self.auth_tokens = []
        self.save_printer_auth_token()

    # TODO refactor printer profiles system; currently it is hacky due to remaking of merging priority from local to cloud and back 
    def init_profiles(self, profiles=None):
        if profiles:
            self.save_profiles(profiles)
        local_profiles = self.load_local_printer_profiles()
        if profiles != local_profiles:
            profiles = self.merge_profiles(local_profiles, profiles)
            self.save_profiles(profiles)
        self.profiles = profiles
        self.logger.info("Got profiles for %d printers" % len(self.profiles))
        config.Config.instance().set_profiles(self.profiles)

    def check_function(self):
        return bool(self.user_token or \
                    self.auth_tokens or \
                    not config.get_settings()['protocol']['user_login'] or \
                    (self.parent and getattr(self.parent, 'offline_mode', False)))

    def retry_login_until_success(self, exit_no_saved=False):
        while not self.parent or not getattr(self.parent, "stop_flag", False):
            if self.login_from_saved_creds(exit_no_saved) or self.check_function():
                return
            time.sleep(2)
            self.logger.info('Retrying with login...')


if __name__ == "__main__":
    import sys

    class FakeApp:
        def __init__(self):
            self.offline_mode = False
            self.stop_flag = False

    login, _ = UserLogin.load_login()
    cloud_auth_res = 'Fail'
    if login:
        ul = UserLogin(FakeApp(), retry_in_background=False)
        if ul.login:
            cloud_auth_res = 'Success'
    if '--login' in sys.argv:
        if not login:
            sys.exit(1)
        print(login)
    elif '--auth' in sys.argv:
        if not cloud_auth_res:
            sys.exit(2)
        print(cloud_auth_res)
    else:
        if not login:
            print('Login: no')
        else:
            print('Login: ' + login)
        print('Cloud authorization: ' + cloud_auth_res)
