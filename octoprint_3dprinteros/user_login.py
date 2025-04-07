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
import hashlib
import logging
import marshal
import pprint
import time
import threading

import config
import paths
from awaitable import Awaitable
from http_client import HTTPClient, HTTPClientPrinterAPIV1


class UserLogin(Awaitable):

    NAME = 'user login'
    STORAGE_PATH = os.path.join(paths.CURRENT_SETTINGS_FOLDER, 'stuff.bin')
    DEFAULT_PRINTER_PROFILES_PATH = os.path.join(os.path.dirname(__file__), 'default_printer_profiles.json')
    USER_PRINTER_PROFILES_PATH = os.path.join(paths.CURRENT_SETTINGS_FOLDER, 'user_printer_profiles.json')
    CACHED_PRINTER_PROFILES_PATH = os.path.join(paths.CURRENT_SETTINGS_FOLDER, 'cached_printer_profiles.json')
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
        if settings_mod:
            logger = logging.getLogger(__name__)
            if not isinstance(settings_mod, dict):
                logger.warning("Server's setting mods is not dict:\n" + pprint.pformat(settings_mod))
            else:
                logger.info("Server's setting mods:\n" + pprint.pformat(settings_mod))
                current_settings = config.get_settings()
                new_settings = config.merge_dictionaries(current_settings, settings_mod, overwrite=True)
                config.Config.instance().settings = new_settings
                logger.info("Setting:\n" + pprint.pformat(new_settings))

    @staticmethod
    def load_local_printer_profiles():
        profiles = []
        if config.get_settings()['printer_profiles']['only_local']:
            profile_sources = (UserLogin.DEFAULT_PRINTER_PROFILES_PATH, UserLogin.USER_PRINTER_PROFILES_PATH)
        else:
            profile_sources = (UserLogin.DEFAULT_PRINTER_PROFILES_PATH, UserLogin.CACHED_PRINTER_PROFILES_PATH, UserLogin.USER_PRINTER_PROFILES_PATH)
        for path in profile_sources:
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
    def save_profiles(profiles, filename=None):
        if not filename:
            filename = UserLogin.CACHED_PRINTER_PROFILES_PATH
        try:
            if not isinstance(profiles, str):
                profiles = json.dumps(profiles, sort_keys = True, indent = 4, separators = (',', ': '))
            with open(filename, "w") as f:
                f.write(profiles)
        except (OSError, ValueError) as e:
            logging.getLogger(__name__).warning("Error saving profile file %s: %s" % (profiles, str(e)))

    @staticmethod
    def merge_profiles(base_profiles, updating_profiles):
        base_profiles_dict = {}
        for profile in base_profiles:
            alias = profile.get('alias')
            if not alias:
                logging.getLogger(__name__).warning('Invalid profile(no alias):\n%s', pprint.pformat(profile))
            elif 'name' not in profile:
                logging.getLogger(__name__).warning('Invalid profile(no name):\n%s', pprint.pformat(profile))
            elif not profile.get('vids_pids') and 'v2' not in profile:
                logging.getLogger(__name__).warning('Invalid profile(no vids_pids or v2):\n%s', pprint.pformat(profile))
            else:
                base_profiles_dict[alias] = profile
        updating_profiles_dict = {}
        if updating_profiles:
            for profile in updating_profiles:
                alias = profile.get('alias')
                if not alias:
                    logging.getLogger(__name__).warning('Invalid profile(no alias):\n%s', pprint.pformat(profile))
                elif 'name' not in profile:
                    logging.getLogger(__name__).warning('Invalid profile(no name):\n%s', pprint.pformat(profile))
                elif not profile.get('vids_pids') and 'v2' not in profile:
                    logging.getLogger(__name__).warning('Invalid profile(no vids_pids or v2):\n%s', pprint.pformat(profile))
                else:
                    updating_profiles_dict[alias] = profile
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
        config.Config.instance().set_profiles(self.profiles)
        self.profiles_sha256 = self.get_cloud_profiles_cache()
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
        if auto_login and not self.parent or not self.parent.offline_mode:
            if not self.login_from_saved_creds(exit_no_saved=False):
                if retry_in_background:
                    self.login_retry_thread = threading.Thread(target=self.retry_login_until_success)
                    self.login_retry_thread.start()
                else:
                    self.retry_login_until_success(exit_no_saved=True)
        Awaitable.__init__(self, parent)

    def login_from_saved_creds(self, exit_no_saved=False):
        with self.login_lock:
            if self.login_mode == self.APIPRINTER_MODE:
                self.auth_tokens = self.load_printer_auth_tokens()
                self.logger.info("Getting profiles without user login")
                self.http_connection = HTTPClientPrinterAPIV1(self.parent, exit_on_fail=True)
                profiles = []
                if self.http_connection.connection:
                    self.got_network_connection = True
                    if config.get_settings()['printer_profiles']['get_updates']:
                        profiles = self.http_connection.pack_and_send(HTTPClientPrinterAPIV1.PRINTER_PROFILES)
                        if profiles:
                            self.save_profiles(profiles)
                else:
                    self.got_network_connection = False
                self.http_connection.close()
                self.update_profiles()
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
                if not error:
                    if login:
                        return True
                    else:
                        return exit_no_saved
                return False

    def login_as_user(self, login=None, password=None, disposable_token=None, save_password_flag=True):
        with self.login_lock:
            if not login:
                return 0, "Empty login"
            if password is None and not disposable_token:
                return 0, "Empty password"
            if not self.http_connection:
                self.http_connection = HTTPClient(self.parent, exit_on_fail=True)
            answer = self.http_connection.pack_and_send(HTTPClient.USER_LOGIN, \
                    login, \
                    password, \
                    disposable_token=disposable_token, \
                    profiles_sha256=self.profiles_sha256)
            if answer:
                settings_mod = answer.get('settings_mod')
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
                profiles = answer.get('all_profiles', [])
                if profiles and config.get_settings()['printer_profiles']['get_updates']:
                    try:
                        profiles_sha256 = hashlib.sha256(str(profiles).encode('ascii', errors='ignore')).hexdigest()
                    except:
                        profiles_sha256 = 'hash error'
                    self.logger.info('Received cloud profiles with sha256: %s', profiles_sha256)
                    self.save_profiles(profiles)
                    if isinstance(profiles, str):
                        try:
                            profiles = json.loads(profiles)
                        except:
                            profiles = []
                            self.logger.error("Server's user_login response got invalid printer profiles - not json")
                    elif not isinstance(profiles, list):
                        self.logger.error("Server's user_login response got invalid printer profiles - not json list")
                        profiles = []
                self.update_profiles()
                self.macaddr = self.http_connection.host_id
                if login:
                    self.login = login
                else:
                    login_name = answer.get('user_login')
                    if login_name:
                        self.login = login_name
                    else:
                        self.login = "Temporary login"
                user_token = answer.get('user_token')
                if not user_token:
                    raise ValueError('Server returned empty user token')
                self.user_token = user_token
                self.logger.info("Successful login from user %s" % self.login)

    def save_printer_auth_token(self, usb_info=None, auth_token=None):
        if usb_info and auth_token:
            self.auth_tokens.append((usb_info,  auth_token))
        data = json.dumps({'auth_tokens': self.auth_tokens})
        return self.save_data(self.endecrypt_stuffbin(data.encode('utf-8')))

    def forget_auth_tokens(self):
        self.auth_tokens = []
        self.save_printer_auth_token()

    def update_profiles(self, profiles=None):
        if not profiles:
            profiles = self.load_local_printer_profiles()
        self.profiles = profiles
        self.logger.info("Got profiles for %d printers" % len(self.profiles))
        config.Config.instance().set_profiles(self.profiles)

    def check_function(self):
        return bool(self.user_token or \
                    self.auth_tokens or \
                    not config.get_settings()['protocol']['user_login'] or \
                    (self.parent and getattr(self.parent, 'offline_mode', False)))

    def retry_login_until_success(self, exit_no_saved=False):
        while not self.parent or not getattr(self.parent, "stop_flag"):
            if self.login_from_saved_creds(exit_no_saved) or self.check_function():
                return
            time.sleep(2)

    def get_cloud_profiles_cache(self):
        try:
            with open(self.CACHED_PRINTER_PROFILES_PATH, encoding='ascii') as f:
                profiles_string = f.read()
                cloud_profiles_sha256 = hashlib.sha256(profiles_string.encode('ascii', errors='ignore')).hexdigest()
                self.logger.info('Current printers profiles sha256: ' + cloud_profiles_sha256)
        except FileNotFoundError:
            cloud_profiles_sha256 = ''
        except Exception as e:
            self.logger.exception('Error calculating printer profiles hash:' + str(e))
            cloud_profiles_sha256 = ''
        return cloud_profiles_sha256


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
