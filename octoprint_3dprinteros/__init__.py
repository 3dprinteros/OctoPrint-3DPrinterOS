# coding=utf-8
from __future__ import absolute_import
import os
import flask
import logging
import json

import octoprint.plugin
from octoprint.events import Events
from octoprint_3dprinteros.printer_interface import PrinterInterface
from octoprint_3dprinteros.http_client import HTTPClient


class Cloud3DPrinterOSPlugin(octoprint.plugin.StartupPlugin,
                             octoprint.plugin.TemplatePlugin,
                             octoprint.plugin.SettingsPlugin,
                             octoprint.plugin.AssetPlugin,
                             octoprint.plugin.SimpleApiPlugin,
                             octoprint.plugin.EventHandlerPlugin):
    VERSION = '1.0'

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

    def __init__(self):
        self._port = 80
        self.pi = None
        self.token = None
        self.stop_flag = False
        self.printer_connected = False
        self.printer_type = None
        self.mac = None
        self.local_ip = None
        self.url = None
        self.last_http_client_error = None
        self.printer_types = {
            'ROBO3D': {'name': 'Robo3D R1', 'VID': '03EB', 'PID': '204B'},
            'ROBO3D_R1PLUS': {'name': 'Robo3D R1+', 'VID': '2341', 'PID': '0010'},
            'RR2': {'name': 'Robo3D R2', 'VID': 'OCTO', 'PID': '0RR2'},
            'RC2': {'name': 'Robo3D C2', 'VID': 'OCTO', 'PID': '0RC2'}
        }
        self.printer_types_js = []
        for key in self.printer_types:
            self.printer_types_js.append({'type': key, 'name': self.printer_types[key]['name']})

    ##~~ SettingsPlugin mixin

    def get_settings_defaults(self):
        return dict(
            url="acorn.3dprinteros.com",
            printer_type="RR1P",
            verbose=False,
            registered=False,
            serial=True,
            printer_types_json=json.dumps(self.printer_types_js)
        )

    ##~~ AssetPlugin mixin

    def get_assets(self):
        # Define your plugin's asset files to automatically include in the
        # core UI here.
        return dict(
            js=["js/3dprinteros.js"],
            css=["css/3dprinteros.css"],
            less=["less/3dprinteros.less"]
        )

    # def get_template_configs(self):
    #     return [
    #         dict(type="navbar", custom_bindings=False),
    #         dict(type="settings", custom_bindings=False)
    #     ]

    def get_template_vars(self):
        return dict(
            url='https://'+self.url
        )

    def _update_local_settings(self):
        self._logger.setLevel(logging.DEBUG if self._settings.get(['verbose']) else logging.NOTSET)
        self._logger.debug("_update_local_settings")

    ##~~ Softwareupdate hook
    def get_update_information(self, *args, **kwargs):
        return dict(
            c3dprinteros=dict(
                displayName="3DPrinterOS Plugin",
                displayVersion=self._plugin_version,

                # version check: github repository
                type="github_release",
                user="3dprinteros",
                repo="OctoPrint-3DPrinterOS",
                current=self._plugin_version,

                # update method: pip
                pip="https://github.com/3dprinteros/OctoPrint-3DPrinterOS/archive/{target_version}.zip"
            )
        )

    def on_startup(self, host, port):
        if self._settings.get(['verbose']):
            self._logger.setLevel(logging.DEBUG)
        self._logger.debug("on_startup")
        self._logger.info("Hello World!")
        self._port = port
        self.url = self._settings.get(['url'])
        self.read_token()
        self.printer_type = self._settings.get(['printer_type'])
        self._logger.debug('token %s' % self.token)
        self._settings.set(['registered'], True if self.token else False)

    def read_token(self):
        data_folder = self.get_plugin_data_folder()
        token_file = os.path.join(data_folder, 'token')
        self._logger.debug('Read Token file: %s' % token_file)
        self.token = None
        try:
            with open(token_file) as f:
                token = f.readline().strip()
                if len(token) == 128:
                    self.token = token
        except:
            pass
        if not self.token:
            self._logger.error("Token was not found")
        return self.token

    def set_token(self, token):
        data_folder = self.get_plugin_data_folder()
        token_file = os.path.join(data_folder, 'token')
        self._logger.debug('Save Token file: %s' % token_file)
        if not token:
            try:
                os.remove(token_file)
            except OSError:
                pass
            self.token = None
            return True
        if len(token) != 128:
            self._logger.error("Wrong token size: %i" % len(token))
            return False
        try:
            with open(token_file, mode='w') as f:
                f.write(token)
        except Exception as e:
            self._logger.error("Some problem on token save: %s" % str(e))
            return False
        self.token = token
        return True

    def get_logger(self):
        return self._logger

    # def on_after_startup(self, *args, **kwargs):
    #     self._logger.debug("on_after_startup")
    #     self._logger.info("Hello World!")
    #
    def _stop_printer_interface(self):
        if self.pi:
            self.pi.stop_flag = True
            self.pi = None

    def restart_printer_interface(self):
        self._stop_printer_interface()
        if self.token and self.printer_connected:
            self.pi = PrinterInterface(self, self.token)
            self.pi.start()

    def on_event(self, event, payload):
        self._logger.debug("------------------------------------------------------------------------------")
        self._logger.debug("on_event %s: %s" % (event, str(payload)))
        if event == Events.CONNECTED:
            self.printer_connected = True
            # self._logger.info('Start printer interface %s' % str(self._printer.get_current_connection()))
            # self._logger.info('get_current_data %s' % str(self._printer.get_current_data()))
            # self._logger.info('get_current_job %s' % str(self._printer.get_current_job()))
            # self._logger.info('get_current_temperatures %s' % str(self._printer.get_current_temperatures()))
            # self._logger.info('get_state_id %s' % str(self._printer.get_state_id()))
            self.restart_printer_interface()
        elif event == self.EVENT_START_PRINTER_INTERFACE:
            self.restart_printer_interface()
        elif not self.pi:
            return
        if event == Events.DISCONNECTED:
            self._logger.info('Stop printer interface')
            self.printer_connected = False
            self._stop_printer_interface()
        elif event == Events.PRINT_CANCELLED:
            self.pi.printer.octo_cancel()
        elif event == Events.PRINT_FAILED:
            self.pi.printer.octo_failed()

    def get_printer(self):
        return self._printer

    def get_file_manager(self):
        return self._file_manager

    # ~~ SimpleApiPlugin mixin
    def get_api_commands(self, *args, **kwargs):
        return dict(
            register=['printer_type'],
            unregister=[]
        )

    def is_api_adminonly(self, *args, **kwargs):
        return True

    def _error_response(self, message, code=400):
        response = flask.jsonify(message=message)
        response.status_code = code
        return response

    def register_error(self, code, message, is_blocking=False,
                       send_if_same_as_last=True, is_info=False):
        self._logger.warning("Error N%d. %s" % (code, message))
        self.last_http_client_error = message

    def on_api_command(self, command, data):
        self.get_logger().info('on_api_command_test %s, %s' % (command, data))
        if command == 'unregister':
            if self.set_token(None):
                self._settings.set(['registered'], False)
                self._stop_printer_interface()
            return flask.jsonify({})
        elif command == 'register':
            self.printer_type = data['printer_type']
            self._settings.set(['printer_type'], self.printer_type)
            if self.printer_type not in self.printer_types:
                return self._error_response('Printer type "%s" is not supported by this plugin' % self.printer_type)
            hclient = HTTPClient(self, keep_connection_flag=False, max_send_retry_count=0)
            ptype = self.printer_types[self.printer_type]
            request = {'VID': ptype['VID'], 'PID': ptype['PID'], 'SNR': 'OP', 'mac': self.mac,
                       'type': self.printer_type, 'version': self.VERSION}
            if 'code' in data:
                request['registration_code'] = data['code']
            result = hclient.send(hclient.printer_register_path, json.dumps(request))
            self._logger.info('result %s' % result)
            if result is None:
                msg = self.last_http_client_error if self.last_http_client_error else\
                    'Some error from 3DPrinetrOS Cloud'
                self.last_http_client_error = None
                return self._error_response(msg)
            elif 'auth_token' in result:
                if not self.set_token(result['auth_token']):
                    return self._error_response('Some error on save token')
                self._settings.set(['token'], self.token)
                self._settings.set(['registered'], True if self.token else False)
                self._event_bus.fire(self.EVENT_START_PRINTER_INTERFACE)
                response = {'auth_token': 'ok', 'email': result.get('email', 'Unknown')}
            elif 'registration_code' in result:
                response = {'code': result['registration_code']}
            else:
                response = {}
            return flask.jsonify(response)
        return self._error_response('Unknown command')

__plugin_name__ = "3DPrinterOS"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = Cloud3DPrinterOSPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
    }
