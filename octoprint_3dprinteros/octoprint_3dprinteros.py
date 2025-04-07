# Copyright 3D Control Systems, Inc. All Rights Reserved 2023.
# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)

import copy
import json
import logging
import pprint
import time

import flask

import octoprint.plugin
from octoprint.events import Events

import config
import paths
from slave_app import SlaveApp


class Plugin3DPrinterOS(octoprint.plugin.StartupPlugin,
                         octoprint.plugin.ShutdownPlugin,
                         octoprint.plugin.TemplatePlugin,
                         octoprint.plugin.SettingsPlugin,
                         octoprint.plugin.AssetPlugin,
                         octoprint.plugin.SimpleApiPlugin,
                         octoprint.plugin.EventHandlerPlugin):

    EVENT_START_PRINTER_INTERFACE = 'START_PRINTER_INTERFACE'

    STATE_READY = 0
    STATE_PRINTING = 1
    STATE_PAUSED = 2
    STATE_UNPLUGGED = 3
    STATE_ERROR = 4
    STATE_DOWNLOADING = 5
    STATE_CONNECTING = 6
    STATE_LOCAL_MODE = 7
    STATE_PRINTING_LOCALLY = 8
    STATE_CHANGE_FILAMENT = 9
    STATE_BED_NOT_CLEAR = 10

    PRINTER_TIMEOUT = 12

    SUPPORTED_PRINTER_TYPES = {
        'RR2': {'name': 'Robo3D R2', 'VID': 'OCTO', 'PID': '0RR2'},
        'RC2': {'name': 'Robo3D C2', 'VID': 'OCTO', 'PID': '0RC2'},
        'MGM3': {'name': 'MakerGear M3-ID', 'VID': 'OCTO', 'PID': 'MGM3'},
        'TLDQ2': {'name': 'TRILAB DeltiQ 2', 'VID': 'OCTO', 'PID': '0TD2'},
        'OCTOPRINT': {'name': 'Generic OctoPrint', 'VID': 'OCTO', 'PID': 'OCTO'}
    }

    @staticmethod
    def profile_to_printer_info(profile):
        vid, pid = profile['vids_pids'][0]
        return {'name': profile['name'], 'VID': vid, 'PID': pid}

    @staticmethod
    def profiles_to_printer_types(profiles):
        printer_types = {}
        for profile in profiles:
            printer_types[profile['alias']] = Plugin3DPrinterOS.profile_to_printer_info(profile)
        return printer_types

    def __init__(self):
        self.app = None

    def on_after_startup(self):
        if self.app and self.app.is_alive():
            self._logger.info("Plugin for 3DPrinterOS started")
        else:
            self._logger.info("Plugin for 3DPrinterOS failed to start")

    def on_shutdown(self):
        self._logger.info("Plugin received shutdown call")
        self.stop_printer_interface()
        if self.app:
            self.app.stop_flag = True
            try:
                self.app.join(10)
            except:
                pass

    def on_plugin_pending_uninstall(self):
        self._logger.info("Plugin received uninstall call")
        paths.remove_folder_contents(paths.CURRENT_SETTINGS_FOLDER)

    #NOTE called on each start on this plugin instead of actual cleanup on uninstall
    def on_settings_cleanup(self):
        self._logger.info("Plugin received cleanup call")
        #paths.remove_folder_contents(paths.CURRENT_SETTINGS_FOLDER)

    def get_plugin_version(self):
        return self._plugin_version

    def get_settings_defaults(self):
        printer_types = [{'type': key, **self.SUPPORTED_PRINTER_TYPES[key]} for key in self.SUPPORTED_PRINTER_TYPES]
        #printer_types = [{'type': key, **self.supported_printer_types[key]} for key in self.supported_printer_types]
        self._logger.debug('3DPrinterOS supported types:\n' + pprint.pformat(printer_types))
        return dict(
            url="cli-cloud.3dprinteros.com",
            site_url="cloud.3dprinteros.com",
            printer_type="OCTOPRINT",
            verbose=False,
            registered=False,
            serial=True,
            printer_types_json=json.dumps(printer_types),
            camera_enabled=False
        )

    def get_assets(self):
        return dict(
            js=["js/3dprinteros.js"],
            css=["css/3dprinteros.css"],
            less=["less/3dprinteros.less"]
        )

    def get_template_vars(self):
        return dict(url='https://'+self._settings.get(['site_url']))

    def get_octo_printer(self):
        return self._printer

    def get_octo_file_manager(self):
        return self._file_manager

    def settings_load(self):
        printeros_settings = config.get_settings()
        verbose = printeros_settings['verbose']
        self._logger.setLevel(logging.DEBUG if verbose else logging.NOTSET)
        self._settings.set(['verbose'], verbose)
        self._settings.set(['camera_enabled'], printeros_settings['camera']['enabled'])
        self._settings.set(['registered'], bool(self.app.user_login.auth_tokens))
        cli_server_url = printeros_settings['URL']
        self._settings.set(['url'], cli_server_url)

    def settings_save(self):
        verbose = self._settings.get(['verbose'])
        self._logger.setLevel(logging.DEBUG if verbose else logging.NOTSET)
        initial_settings = config.get_settings()
        updated_settings = copy.deepcopy(initial_settings)
        updated_settings['verbose'] = verbose
        updated_settings['URL'] = self._settings.get(['verbose'])
        if self._settings.get(['camera_enabled']):
            if not updated_settings['camera']['enabled']:
                updated_settings['camera']['enabled'] = True
                self.app.camera_controller.enabled = True
                self.app.camera_controller.start_camera_process()
        else:
            if updated_settings['camera']['enabled']:
                updated_settings['camera']['enabled'] = False
                self.app.camera_controller.enabled = False
                self.app.camera_controller.stop_camera_process()
        cli_server_url = self._settings.get(['url'])
        updated_settings['URL'] = cli_server_url
        self._settings.set(['site_url'], cli_server_url.lstrip('cli-'))
        if initial_settings != updated_settings:
            config.Config.instance().save_settings(updated_settings)

    def get_update_information(self, *args, **kwargs):
        return dict(
            c3dprinteros={
                "displayName": "3DPrinterOS Plugin",
                "displayVersion": self.get_plugin_version(),
                # version check: github repository
                "type": "github_release",
                "user": "3dprinteros",
                "repo": "OctoPrint-3DPrinterOS",
                "current": self.get_plugin_version(),
                # update method: pip
                "pip": "https://github.com/3dprinteros/OctoPrint-3DPrinterOS/archive/{target_version}.zip"
            }
        )

    def initialize(self):
        if self.app:
            try:
                self.app.stop_flag = True
                self.app.join(10)
            except:
                pass
        self.app = SlaveApp(owner=self)
        #self.supported_printer_types = self.profiles_to_printer_types([profile for profile in self.app.user_login.profiles if profile['vids_pids'] and profile['vids_pids'][0] == 'OCTO'])
        self.app.start()
        self.app.wait_for_printer()
        self.settings_load()

    def stop_printer_interface(self):
        if self.app:
            pi = self.app.get_printer_interface()
            if pi:
                pi.close()
                pi.join()

    def start_printer_interface(self):
        if self.app.user_login.auth_tokens and self.app.init_ok:
            pi = self.app.get_printer_interface()
            if not pi or not pi.is_alive():
                #self._printer.connect()
                printer_type = self._settings.get(['printer_type'])
                if printer_type:
                    for profile in self.app.user_login.profiles:
                        if profile['alias'] == printer_type:
                            printer_info = self.profile_to_printer_info(profile)
                            break
                    self.app.detectors['StaticDetector'].add_printer(printer_info, to_config=True)

    def on_event(self, event, payload):
        self._logger.info("------------------------------------------------------------------------------")
        self._logger.info("on_event %s: %s" % (event, str(payload)))
        if event == 'plugin_pluginmanager_enable_plugin':
            if payload.get('id') == 'c3dprinteros':
                self.initialize()
        elif self.app and self.app.is_alive():
            if event == 'plugin_pluginmanager_disable_plugin':
                if payload.get('id') == 'c3dprinteros':
                    self.on_shutdown()
            elif event == Events.SETTINGS_UPDATED:
                self._logger.info('OctoPrint settings update:')
                self.settings_save()
            elif event == Events.CONNECTED:
                self._logger.info('OctoPrint connect')
                self.start_printer_interface()
            elif event == self.EVENT_START_PRINTER_INTERFACE:
                self._logger.info('OctoPrint start printer interface')
                self.start_printer_interface()
            elif event == Events.SHUTDOWN:
                self.app.stop_flag = True
            else:
                pi = self.app.get_printer_interface()
                if pi:
                    if event == Events.DISCONNECTED:
                        self._logger.info('OctoPrint disconnect')
                        self.stop_printer_interface()
                    elif event == Events.PRINT_CANCELLED:
                        self._logger.info('OctoPrint cancel')
                        pi.cancel_locally()
                    elif event == Events.PRINT_FAILED:
                        self._logger.info('OctoPrint print fail')
                        pi.register_error(610, "OctoPrint print failed", is_blocking=True)
                        if pi.sender:
                            pi.sender.remove_print_file()
                    elif event == Events.PRINT_DONE:
                        self._logger.info('OctoPrint done')
                        if pi.sender:
                            pi.sender.set_percent(100.0)
                            pi.sender.remove_print_file()
                    elif event == Events.ERROR:
                        self._logger.info('OctoPrint error')
                        pi.register_error(611, "OctoPrint error: %s" % payload.get('error', 'unknown error'))

    def get_api_commands(self, *args, **kwargs):
        return dict(
            register=['printer_type'],
            unregister=[]
        )

    def is_api_adminonly(self, *args, **kwargs):
        return True

    def error_response(self, message, code=400):
        self._logger.debug(f'_error_response: {message}')
        response = flask.jsonify(message=message)
        response.status_code = code
        return response

    def on_api_command(self, command, data):
        self._logger.debug(f'on_api_command {command} {data}')
        if command == 'unregister':
            self._settings.set(['registered'], False)
            self.app.user_login.forget_auth_tokens()
            if self.app.camera_controller.enabled:
                self.app.camera_controller.stop_camera_process()
            self.app.detectors['StaticDetector'].remove_all(save=True)
            pi = self.app.get_printer_interface()
            if pi:
                pi.close()
            return flask.jsonify({})
        elif command == 'register':
            printer_type = data.get('printer_type')
            printer_info = self.SUPPORTED_PRINTER_TYPES.get(data.get('printer_type'))
            if not printer_info:
                return self.error_response('Non supported printer type' + str(printer_type))
            self._settings.set(['printer_type'], printer_type)
            pi = self.app.get_printer_interface()
            if pi and pi.is_alive():
                if pi.usb_info['VID'] != printer_info['VID'] or \
                    pi.usb_info['PID'] != printer_info['PID']:
                    self._logger.debug('Stopping previous 3DPrinterOS printer interface')
                    self.app.detectors['StaticDetector'].remove_all(save=True)
                    try:
                        self.pi.close()
                        self.pi.join()
                    except (AttributeError, RuntimeError):
                        pass
            self.app.detectors['StaticDetector'].add_printer(printer_info, to_config=True, save=False)
            pi = None
            time_left = self.PRINTER_TIMEOUT
            self._logger.debug('Waiting for new 3DPrinterOS printer interface')
            while not pi:
                time.sleep(0.01)
                time_left -= 0.01
                pi = self.app.get_printer_interface()
                if not time_left:
                    self.app.detectors['StaticDetector'].remove_printer(printer_info, allow_remove_from_config=True)
                    return self.error_response('Error. Try again')
            self._logger.debug('Waiting for 3DPrinterOS printer registration code or its acceptance')
            while not pi.printer_token:
                if not pi.is_alive() or not time_left:
                    self.app.detectors['StaticDetector'].remove_printer(printer_info, allow_remove_from_config=True)
                    return self.error_response('Error. Try again')
                if pi.registration_code:
                    return flask.jsonify({'code': pi.registration_code})
                time.sleep(0.01)
                time_left -= 0.01
            self._settings.set(['registered'], True)
            self.app.detectors['StaticDetector'].save_to_config()
            pi.restart_camera()
            return flask.jsonify({'auth_token': 'ok', 'email': pi.registration_email})
        return self.error_response('Unknown command')
