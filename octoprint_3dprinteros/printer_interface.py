# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019.
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
import threading
import collections
import typing

# try:
#     import psutil
# except:
#     psutil = None

import base_sender
import base_detector
import config
import downloader
import forced_settings
import http_client
import log
import printer_settings_and_id
import printer_states

class PrinterInterface(threading.Thread):

    DEFAULT_OPERATIONAL_TIMEOUT = 10
    DEFAULT_LOCAL_MODE_TIMEOUT = 2
    ON_CONNECT_TO_PRINTER_FAIL_SLEEP = 3
    APIPRINTER_REG_PERIOD = 2
    FORGET_ERROR_AFTER = 60
    LOOP_SLEEP_STEPS = 10
    NUM_THREADS_WARNING_THREASHOLD = 60
    # NUM_FILE_OPEN_WARNING_THREASHOLD = 128

    COMMANDS_ALLOWED_IN_ERROR_STATE = ['close', 'reset_offline_printer_type', 'set_printer_type', 'set_connection', 'forget_printer', 'remember_printer', 'set_verbose']

    def __init__(self, app: typing.Any, usb_info: dict, command_request_period: int = 5, offline_mode: bool = False, forced_printer_profile: dict = {}):
        self.command_request_period = command_request_period
        self.app = app
        self.usb_info = usb_info
        self.id_string = printer_settings_and_id.create_id_string(usb_info)
        super().__init__(name="PrinterInterface",  daemon=True)
        if config.get_settings().get('logging', {}).get('per_printer', False):
            self.logger = log.create_logger(self.id_string, subfolder=log.PRINTERS_SUBFOLDER)
        else:
            self.logger = logging.getLogger(self.id_string)
        self.stop_flag = False
        self.disconnect_flag = False
        self.stop_in_next_loop_flag = False
        self.server_connection = None
        self.server_connection_class = http_client.get_printerinterface_protocol_connection()
        self.logger.info("Server connection class: " + self.server_connection_class.__name__)
        self.sender = None
        self.downloader = None
        self.forced_state = "connecting"
        self.printer_token = None
        self.printer_name = ""
        self.groups = []
        self.errors = []
        self.last_operational_time = time.monotonic()
        self.events = collections.deque() # list of printer events that have to be sent to the server
        self.post_answer_hooks = collections.deque() # list of printer events that have to be sent to the server
        self.errors_lock = threading.Lock()
        self.local_mode = False
        self.local_mode_timeout = self.DEFAULT_LOCAL_MODE_TIMEOUT
        self.local_mode_timeout_thread = None
        self.local_mode_lock = threading.Lock()
        self.requests_to_server = {}
        self.requests_lock = threading.Lock()
        self.show_printer_type_selector = False
        self.timeout = self.DEFAULT_OPERATIONAL_TIMEOUT
        self.printer_profile = forced_printer_profile
        self.printer_connection_dict = {}
        self.possible_printer_types = self._get_possible_printer_profiles()
        self.possible_conn_types = None
        if self.app and self.app.camera_controller:
            self.current_camera = self.app.camera_controller.get_current_camera_name()
        else:
            self.current_camera = None
        self.try_load_auth_token()
        self.registration_code = None
        self.registration_email = ""
        self.offline_mode = offline_mode
        self.printer_settings = printer_settings_and_id.load_settings(self.id_string)
        self._load_volatile_settings()
        self.logger.info(f'Loaded printer settings {self.printer_settings}')
        self.connection_id = self.printer_settings.get('connection_id')
        self.connection_profile = {}
        self.cloud_printer_id = None
        self.get_print_estimations_from_cloud = config.get_settings().get('print_estimation', {}).get('by_cloud', False)
        self.was_ready_at_least_once = False
        self.load_printer_type()
        self.force_printer_type()
        self.materials = []
        # self.last_report = {}
        # following fields will be removed from report if their contents will not change between reports
        # but field is special and doing the same, but is checked per item and only changed items got to report
        self.stored_dynamic_fields = {
            'line_number': None,
            'coords': [],
            'material_names': None,
            'material_volumes': None,
            "material_colors_hex": None,
            "material_desc": None,
            'estimated_time': None,
            'filename': None,
            'ext': {
                'status_bin' : "",
                'id_bin' : "",
                'tank_error': "",
                'cartridge_error': "",
                'cartridge_original_volume': 0
                }
        }
        self.logger.info('New printer interface for %s' % str(usb_info))

    def __str__(self):
        return self.id_string

    def _connect_to_server(self) -> bool:
        self.logger.info("Connecting to server with printer: %s" , str(self.usb_info))
        if self.app and self.app.user_login and self.app.user_login.user_token:
            return self._register_with_streamerapi(self.app.user_login.user_token)
        if self.printer_token:
            self.server_connection = self.server_connection_class(self)
            return True
        return self._register_with_apiprinter()

    def _register_with_streamerapi(self, token) -> bool:
        kw_message = {'no_job_fail': True, "profiles_version": "v2"}
        while not self.disconnect_flag and not self.stop_flag and not getattr(self.app, "stop_flag", False):
            if [error for error in self.errors if error['is_blocking'] and not error.get('preconnect')]:
                self.disconnect_flag = True
                return False
            if not self.server_connection:
                self.server_connection = self.server_connection_class(self)
            if self.app and self.app.offline_mode:
                self.offline_mode = True
            if self.offline_mode:
                return True
            # migrate_cloud_printer_id = self.printer_settings.get('migrate_cloud_printer_id')
            # if migrate_cloud_printer_id is not None:
            #     kw_message['printer_id'] = migrate_cloud_printer_id
            #     del self.printer_settings['migrate_cloud_printer_id']
            #     printer_settings_and_id.save_settings(self.id_string, self.printer_settings)
            if self.connection_id:
                kw_message['select_conn_id'] = self.connection_id
            if self.printer_profile:
                kw_message['select_printer_type'] = self.printer_profile['alias']
            message = [http_client.HTTPClient.PRINTER_LOGIN, token, self.usb_info]
            self.logger.info('Printer login request:\n%s\n%s ' % (str(message), str(kw_message)))
            answer = self.server_connection.pack_and_send(*message, **kw_message)
            if answer:
                self.printer_name = answer.get('name', '')
                self.logger.info('Printer name received: ' + str(self.printer_name))
                error = answer.get('error')
                if error:
                    error_code = error.get('code')
                    self.logger.warning(f"Error while login {self.usb_info}: {error_code} {error.get('message')}")
                    if error_code == 8:
                        if self.printer_profile:
                            kw_message['select_printer_type'] = self.printer_profile['alias']
                        else:
                            self.show_printer_type_selector = True
                    elif error_code == 9 or error_code == 7:
                        self.printer_profile = {}
                        self.printer_connection_dict = {}
                        self.printer_settings = {}
                        printer_settings_and_id.reset_printer_settings(self.id_string)
                        self.stop_flag = True
                        return False
                    else:
                        self.register_error(26, "Critical error on printer login: %s" % error)
                        self.stop_flag = True
                        time.sleep(self.command_request_period)
                        return False
                else:
                    groups = answer.get('current_groups')
                    if groups:
                        self.set_groups(groups)
                    self.show_printer_type_selector = False
                    self.logger.info('Successfully connected to server.')
                    try:
                        self.printer_token = answer['printer_token']
                        printer_profile = answer["printer_profile"]
                        if isinstance(printer_profile, dict):
                            self.printer_profile = printer_profile
                        else:
                            self.printer_profile = json.loads(printer_profile)
                        if type(self.printer_profile) != dict:
                            raise ValueError("Profile should have type dict")
                    except (ValueError, TypeError) as e:
                        self.logger.error("Server responded with invalid printer profile. Error: %s" % e)
                    else:
                        self.logger.debug('Setting profile: ' + str(self.printer_profile))
                        camera = answer.get('camera')
                        if camera and camera != self.current_camera:
                            self.switch_camera(camera)
                            if self.app and self.app.camera_controller:
                                self.current_camera = self.app.camera_controller.get_current_camera_name()
                        # cloud_printer_id = answer.get('printer_id')
                        # if cloud_printer_id is not None and cloud_printer_id != self.printer_settings.get('cloud_printer_id'):
                        #     self.printer_settings['cloud_printer_id'] = cloud_printer_id
                        #     self.cloud_printer_id = cloud_printer_id
                        #     if 'original_cloud_printer_id' not in self.printer_settings:
                        #         self.printer_settings['original_cloud_printer_id'] = cloud_printer_id
                        #     printer_settings_and_id.save_settings(self.id_string, self.printer_settings)
                        connection_id = answer.get('conn_id')
                        if connection_id and connection_id != self.connection_id:
                            self.printer_settings['connection_id'] = connection_id
                            printer_settings_and_id.save_settings(self.id_string, self.printer_settings)
                    return True
                self.logger.warning("Error on printer login. No connection or answer from server.")
            time.sleep(self.command_request_period)

    def _register_with_apiprinter(self) -> bool:
        while not self.disconnect_flag and not self.stop_flag and not self.app.stop_flag:
            if [error for error in self.errors if error['is_blocking']]:
                self.disconnect_flag = True
                return False
            if not self.printer_profile:
                self.logger.error("No forced profile for the printer but in APIPrinter mode: %s" , self.usb_info)
                return False
            if not self.server_connection:
                self.server_connection = self.server_connection_class(self)
            if self.app.offline_mode or self.offline_mode:
                self.offline_mode = True
                return True
            kw_message = dict(self.usb_info)
            kw_message['type'] = self.printer_profile['alias']
            if self.registration_code:
                kw_message['registration_code'] = self.registration_code
            self.logger.debug('Printer registration request: %s', kw_message)
            answer = self.server_connection.pack_and_send(http_client.HTTPClientPrinterAPIV1.REGISTER,
                                                         **kw_message)
            if answer:
                error = answer.get('error')
                if error:
                    self.logger.warning("Error on printer registration %s: %s %s",
                                       self.usb_info,
                                       error.get('code'),
                                       error.get("message"))
                registration_code = answer.get('registration_code')
                if registration_code:
                    self.registration_code = registration_code
                    self.logger.info("Registration code received: %s" % str(registration_code))
                auth_token = answer.get('auth_token')
                if auth_token:
                    self.registration_code = None
                    self.registration_email = answer.get('email', 'Unknown')
                    self.logger.info("Auth token received")
                    self.printer_token = auth_token
                    vid_pid_snr_only_usb_info = {}
                    for field_name in ('VID', 'PID', 'SNR'):
                        vid_pid_snr_only_usb_info[field_name] = self.usb_info[field_name]
                    save_result = self.app.user_login.save_printer_auth_token(vid_pid_snr_only_usb_info, auth_token)
                    self.logger.info(f"Auth token save result: {save_result}")
                    camera = answer.get('camera')
                    if camera and camera != self.current_camera:
                        self.switch_camera(camera, auth_token)
                    else:
                        self.restart_camera(auth_token)
                    return True
            time.sleep(self.APIPRINTER_REG_PERIOD)

    def _get_printer_conn_type(self, printer_profile, connection_id=None, printer_id=None):
        v2_profile = printer_profile.get('v2', {})
        for conn_type in v2_profile.get('connections', []):
            if connection_id:
                if conn_type.get('id') == connection_id:
                    self.logger.info(f'Connection type {connection_id} for {printer_profile["name"]}')
                    return conn_type
            elif printer_id:
                for conn_id in conn_type['ids']:
                    if conn_id.get('VID') == printer_id.get('VID') and conn_id.get('PID') == printer_id.get('PID'):
                        self.logger.info(f'Guessing connection type as {connection_id} for {printer_profile["name"]} because no connection type was specified')
                        return conn_type
        self.logger.warning(f'No connection can be found for: {printer_profile["name"]}')

    def _patch_profile_with_v2(self, printer_profile: dict, connection_id: str, printer_id) -> None:
        if 'v2' in self.printer_profile:
            self.possible_conn_types = self.printer_profile['v2'].get('connections', [])
            conn_type = self._get_printer_conn_type(printer_profile, connection_id, printer_id) 
            if conn_type:
                patched_profile = copy.deepcopy(printer_profile)
                module = conn_type.get('module')
                if module:
                    patched_profile['sender'] = module 
                    if self.printer_settings.get('connection_id') != conn_type.get('id'):
                        self.printer_settings.update({'connection_id': conn_type.get('id')})
                        printer_settings_and_id.save_settings(self.id_string, self.printer_settings)
                self.printer_profile = patched_profile

    def _connect_to_printer(self) -> base_sender.BaseSender:
        self._patch_profile_with_v2(self.printer_profile, self.connection_id, self.usb_info)
        sender_name = self.printer_profile.get('sender')
        self.timeout = float(self.printer_profile.get("operational_timeout", self.timeout))
        try:
            if not sender_name:
                raise ImportError()
            printer_sender = __import__(sender_name)
        except ImportError:
            message = "Printer with %s is not supported by this version of 3DPrinterOS client." % sender_name
            self.register_error(128, message, is_blocking=True)
            if not self.offline_mode:
                self.requests_to_server['reset_printer_type'] = True
                message, kw_message = self._form_command_request(None)
                self.server_connection.pack_and_send('command', *message, **kw_message)
            else:
                self.save_printer_type()
        else:
            self.logger.info(f"Connecting to {self.printer_profile.get('name')} with using module {sender_name}")
            try:
                printer = printer_sender.Sender(self, self.usb_info, self.printer_profile)
            except RuntimeError as e:
                message = "Can't connect to %s. Reason: %s" %\
                          (self.printer_profile.get('name'), str(e))
                self.register_error(119, message, is_blocking=True)
            else:
                self.logger.info("Successful connection to %s!" % self.printer_profile['name'])
                return printer
        time.sleep(self.ON_CONNECT_TO_PRINTER_FAIL_SLEEP)

    def _form_command_request(self, acknowledge: typing.Any) -> typing.Tuple[typing.List[dict], dict]:
        new_report = self.status_report()
        # events = printer_states.process_state_change(self.last_report, new_report)
        # self.last_report = new_report
        # if events:
        #     for event in events:
        #         self.register_event(event)
        message = [self.printer_token, new_report, acknowledge]
        kw_message = {}
        errors_to_send = self.get_errors_to_send('CLOUD')
        with self.requests_lock:
            if errors_to_send:
                kw_message['error'] = errors_to_send
            kw_message.update(self.requests_to_server)
            self.requests_to_server = {}
        return message, kw_message

    def _check_operational_status(self) -> bool:
        if self.sender:
            if self.sender.is_operational():
                self.forced_state = None
                self.last_operational_time = time.monotonic()
                return True
            if not self.forced_state:
                message = "Printer is not operational"
                self.forced_state = printer_states.CONNECTING_STATE
                if self.was_ready_at_least_once:
                    self.register_error(77, message, is_blocking=False)
            if self.forced_state != "error" and self.last_operational_time + self.timeout < time.monotonic():
                message = "Not operational timeout reached"
                self.register_error(78, message, is_blocking=True)
        return False

    @log.log_exception
    def run(self) -> None:
        self.logger.info('Printer interface started')
        while not self.app.stop_flag and not self.stop_flag:
            threads_count = threading.active_count()
            self.logger.info('Active threads count: ' + str(threads_count))
            if threads_count > self.NUM_THREADS_WARNING_THREASHOLD:
                self.logger.warning('Warning - too many threads:\n' + pprint.pformat(threading.enumerate()))
            # if psutil:
            #     proc = psutil.Process()
            #     open_files = proc.open_files()
            #     if len(open_files) > self.NUM_FILE_OPEN_WARNING_THREASHOLD:
            #         self.logger.info('Too many open files: ' + pprint.pformat(open_files))
            self._run()
        self.logger.info('Printer interface stopped')

    def _run(self) -> None:
        self.disconnect_flag = False
        self.forced_state = printer_states.CONNECTING_STATE
        for error in self.errors:
            error['preconnect'] = True
        if self.offline_mode:
            while not self.printer_profile:
                time.sleep(0.1)
                if self.disconnect_flag or self.stop_flag or self.app.stop_flag:
                    self.close_printer_sender()
                    return
                self.show_printer_type_selector = True
        elif not self._connect_to_server():
            time.sleep(1)
            return
        self.show_printer_type_selector = False
        self.sender = self._connect_to_printer()
        self.last_operational_time = time.monotonic()
        acknowledge = None
        send_reset_job = not (self.printer_profile.get('self_printing') or (self.connection_profile and self.connection_profile.get('hostless_print')))
        kw_message_prev = {}
        event = None
        post_answer_hook = None
        # close_after_requests = ['reset_printer_type']
        while not self.app.stop_flag and not self.stop_flag and not self.disconnect_flag:
            loop_start_time = time.monotonic()
            for error in self.errors:
                if not error.get('preconnect'):
                    if error.get("cancel"):
                        self.register_event({'state:', printer_states.CANCEL_STATE})
                        break
                    if error["is_blocking"]:
                        self.forced_state = printer_states.ERROR_STATE
                        self.add_post_answer_hook(self.disconnect)
                        break
                    # if not self.forced_state and not error.get('is_info'):
                    #     self.forced_state = printer_states.CONNECTING_STATE
                    #     break
            else:
                self.forced_state = None
            message, kw_message = self._form_command_request(acknowledge)
            sent_on = time.monotonic()
            for key, value in kw_message_prev.items():
                if key not in kw_message:
                    kw_message[key] = value
            if self.events:
                if not event:
                    event = self.events.popleft()
            if event:
                message[1].update(event)
            elif not post_answer_hook and self.post_answer_hooks:
                post_answer_hook = self.post_answer_hooks.popleft()
            if send_reset_job:
                kw_message['reset_job'] = True
                send_reset_job = False
            if self.offline_mode:
                self.logger.info(f"Offline:\n{message}\n{kw_message}")
                self._forget_errors(kw_message.get("error", []), sent_on)
                # for request in close_after_requests:
                #     if kw_message.get(request):
                #         self.stop_flag = True
                #         break
                kw_message_prev = {}
                if event:
                    self.logger.info('Sent event: %s', event)
                    event = None
                if post_answer_hook:
                    self.execute_hook(post_answer_hook)
                    post_answer_hook = None
            else:
                answer = self.server_connection.pack_and_send('command', *message, **kw_message)
                if answer is not None:
                    self._forget_errors(kw_message.get("error", []), sent_on)
                    self._update_stored_dynamic_fields(message)
                    #self.logger.info("Answer: " + str(answer))
                    acknowledge = self.execute_server_command(answer)
                    if event:
                        self.logger.info('Sent event: %s', event)
                        event = None
                    if kw_message_prev:
                        kw_message_prev = {}
                    if post_answer_hook:
                        self.execute_hook(post_answer_hook)
                        post_answer_hook = None
                else:
                    kw_message_prev = copy.deepcopy(kw_message)
            self._check_operational_status()
            sleep_time = loop_start_time - time.monotonic() + self.command_request_period
            if sleep_time > 0:
                steps_left = self.LOOP_SLEEP_STEPS
                while steps_left and not self.disconnect_flag and not self.app.stop_flag:
                    time.sleep(sleep_time/self.LOOP_SLEEP_STEPS)
                    steps_left -= 1
        if self.server_connection:
            self.server_connection.close()
        self.close_printer_sender()
        self.logger.info('Printer interface disconnected')

    def _update_stored_dynamic_fields(self, message: typing.List[dict]) -> None:
        if message and len(message) > 2:
            report = message[1]
            for key, value in self.stored_dynamic_fields.items():
                if key != 'ext':
                    new_value = report.get(key)
                    if new_value is not None:
                        self.stored_dynamic_fields[key] = new_value
                else: # to same for ext level as for upper level # TODO make recursive and remove copy-paste
                    for ext_key in value:
                        new_ext_value = report.get('ext', {}).get(ext_key)
                        if new_ext_value is not None:
                            self.stored_dynamic_fields['ext'][ext_key] = new_ext_value

    def _get_method_by_command_name(self, command: str) -> typing.Any:
        if command:
            method = getattr(self, command, None)
            if not method:
                try:
                    if self.sender:
                        method = getattr(self.sender, command, None)
                        # if printer received a command to sender, then it consider used and is no longer a target for commands from cloud's printer connection wizard
                        if not self.printer_settings.get('used'):
                            self.printer_settings['used'] = True
                            printer_settings_and_id.save_settings(self.id_string, self.printer_settings)
                except:
                    pass
            return method

    def _validate_command(self, server_message: dict) -> bool:
        try:
            command = server_message.get('command')
            if command: #no command is a valid scenario
                try:
                    number = int(server_message.get('number'))
                except (ValueError, TypeError):
                    self.register_error(112, f"Cannot execute server command {command} due to invalid number: {server_message.get('number')}.", is_blocking=False)
                    return False
                if not self.sender and not hasattr(self, command):
                    self.register_error(110, f"Cannot execute server command {command} in printer error state.", is_blocking=False)
                    return False
                # this should the remade, because now we accept commands from cloud and api, so in local mode we still should execute commands from api
                # if self.local_mode:
                #     self.register_error(111, "Can't execute command %s while in local_mode!" % command, is_blocking=False)
                #     return False
                if not command or "__" in command or "." in command or \
                        command.startswith('_') or hasattr(threading.Thread, command):
                    self.register_error(111, f"Cannot execute invalid command: {command}.", is_blocking=False)
                    return False
                method = self._get_method_by_command_name(command)
                if not method:
                    self.register_error(40, f"Unknown command: {command}", is_blocking=False)
                    return False
                # payload = server_message.get('payload')
                # if payload:
                #     arguments = []
                #     keyword_arguments = {}
                #     if isinstance(payload, list):
                #         arguments.extend(payload)
                #     elif isinstance(payload, dict):
                #         keyword_arguments.update(payload)
                #     else:
                #         arguments.append(payload)
                #     arg_index = 0
                #     for arg_name, arg_type in typing.get_type_hints(method).items():
                #         if len(arguments) > arg_index:
                #             if not isinstance(arguments[arg_index], arg_type):
                #                 return False
                #         elif arg_name != 'return' and arg_name not in keyword_arguments:
                #             return False
                #         elif not isinstance(keyword_arguments[arg_name], arg_type):
                #             return False
                #         arg_index += 1
        except Exception as e:
            self.register_error(113, f'Exception on command validation: {server_message}\n{e}', is_blocking=False)
            self.logger.exception(f"Exception on command validation: {server_message}")
            return False
        return True

    def execute_server_command(self, server_message: dict) -> dict:
        is_valid = self._validate_command(server_message)
        result = False
        if not is_valid:
            return {"number": server_message.get('number'), "result": False}
        error = server_message.get('error')
        command, number = server_message.get('command'), int(server_message.get('number', 0))
        if command:
            if error and command not in self.COMMANDS_ALLOWED_IN_ERROR_STATE:
                self.logger.warning("@ Server return error: %s\t%s" % (error.get('code'), error.get('message')))
            if config.get_settings().get('hide_sensitive_log') and server_message.get('is_link'):
                log_message = dict(server_message)
                log_message['payload'] = '__hidden__'
            else:
                log_message = server_message
            self.logger.info(f"Command received: " + pprint.pformat(log_message))
            self.logger.info("Executing command number %s : %s" % (number, str(command)))
            method = self._get_method_by_command_name(command)
            if not method:
                if not self.sender:
                    self.register_error(110, f"Cannot execute server command {command} in printer error state.")
                    return { "number": number, "result": False }
                else:
                    method = getattr(self.sender, command, None)
                if not method:
                    self.register_error(111, f"Cannot execute server's unsupported command: {command}.")
                    return { "number": number, "result": False }
            payload = server_message.get('payload')
            arguments = []
            keyword_arguments = {}
            if payload is not None:
                if isinstance(payload, (list, tuple)):
                    arguments.extend(payload)
                elif isinstance(payload, dict):
                    keyword_arguments.update(payload)
                else:
                    arguments.append(payload)
            if server_message.get('is_link'):
                if self.downloader and self.downloader.is_alive():
                    self.register_error(108, "Can't start new download, because previous download isn't finished.")
                    result = False
                else:
                    self._set_cloud_job_id_and_snr(server_message)
                    if command == 'gcodes':
                        self.sender.set_filename(server_message.get('filename', '3DPrinterOS'))
                        self.sender.filesize = server_message.get('size', 0)
                        print_options = server_message.get('print_options')
                        if print_options:
                            self.sender.set_next_print_options(print_options)
                        print_time = 0
                        if self.get_print_estimations_from_cloud:
                            print_time = server_message.get('printing_duration', 0)
                        self.sender.set_estimated_print_time(print_time)
                    self.downloader = downloader.Downloader(self, server_message.get('payload'), method,\
                                                            is_zip=bool(server_message.get('zip')))
                    self.downloader.start()
                    result = True
            else:
                if command == 'gcodes': # A hck for old protocol overloaded command. Currently only links can be printed
                    if number == -1: #TODO fix this ugly hack without breaking the protocol
                        method = self._get_method_by_command_name('unbuffered_gcodes')
                    else:
                        method = self._get_method_by_command_name('unbuffered_gcodes_base64')
                try:
                    if arguments:
                        result = method(*arguments)
                    elif keyword_arguments:
                        result = method(**keyword_arguments)
                    else:
                        result = method()
                    # NOTE to reduce needless 'return True' in methods
                    # so assume that return of None, that is a success too
                    result = result or result is None
                except Exception as e:
                    message = "! Error while executing command %s, number %s.\t%s\nMessage: %s" % (command, number, str(e), log_message)
                    self.register_error(109, message, is_blocking=False)
                    self.logger.exception(message)
                    result = False
            ack = { "number": number, "result": result }
            return ack

    def get_printer_state(self) -> str:
        if self.forced_state:
            state = self.forced_state
        elif self.disconnect_flag or self.stop_flag or not self.sender or self.sender.stop_flag or not self.sender.is_operational():
            state = printer_states.CONNECTING_STATE
        elif self.sender.is_paused():
            state = printer_states.PAUSED_STATE
        elif self.downloader and self.downloader.is_alive() or self.sender.upload_in_progress:
            state = printer_states.DOWNLOADING_STATE
            if getattr(forced_settings, "HIDE_DOWNLOAD_STATUS", False):
                state = printer_states.PRINTING_STATE
            else:
                state = printer_states.DOWNLOADING_STATE
        elif self.sender.is_printing():
            state = printer_states.PRINTING_STATE
        elif self.local_mode:
            state = printer_states.LOCAL_STATE
        elif self.sender.is_bed_not_clear():
            state = printer_states.BED_CLEAN_STATE
        else:
            state = printer_states.READY_STATE
            self.was_ready_at_least_once = True
        return state

    def status_report(self) -> dict:
        report = {"state": self.get_printer_state()}
        if self.sender:
            try:
                if self.is_downloading() and not getattr(forced_settings, "HIDE_DOWNLOAD_STATUS", False):
                    report["percent"] = self.sender.get_downloading_percent()
                else:
                    report["percent"] = self.sender.get_percent()
                report["temps"] = self.sender.get_temps()
                report["target_temps"] = self.sender.get_target_temps()
                report["line_number"] = self.sender.get_current_line_number()
                report["coords"] = self.sender.get_position()
                report["material_names"] = self.sender.get_material_names()
                report["material_desc"] = self.sender.get_material_desc()
                report["material_volumes"] = self.sender.get_material_volumes()
                report["material_colors_hex"] = self.sender.get_material_colors_hex()
                report["estimated_time"] = self.sender.get_estimated_time()
                report["ext"] = self.sender.get_ext()
                for key in self.stored_dynamic_fields:
                    new_value = report.get(key)
                    if key != 'ext':
                        if new_value is None or new_value == self.stored_dynamic_fields[key]:
                            if key in report:
                                del report[key]
                    else: # to same for ext level as for upper level # TODO make recursive and remove copy-paste
                        for ext_key in self.stored_dynamic_fields['ext']:
                            new_ext_value = report['ext'].get(ext_key)
                            if new_ext_value is None or new_ext_value == self.stored_dynamic_fields['ext'][ext_key]:
                                if ext_key in report['ext']:
                                    del report['ext'][ext_key]
                if not report['ext']:
                    del report['ext']
                if self.sender.responses:
                    report["response"] = self.sender.responses[:]
                    self.sender.responses = []
                report.update(self.sender.get_nonstandart_data())
                if self.is_downloading() or self.sender.is_printing() or self.sender.is_paused():
                    clouds_job_id = self.sender.get_clouds_job_id()
                    printers_job_id = self.sender.get_printers_job_id()
                    if clouds_job_id is not None:
                        report['clouds_job_id'] = clouds_job_id
                    if printers_job_id is not None:
                        report['printers_job_id'] = printers_job_id
                    filename = self.sender.get_filename()
                    if filename:
                        report["filename"] = filename
            except Exception as e:
                # update printer state if in was disconnected during report
                report["state"] = self.get_printer_state()
                self.logger.exception("! Exception while forming printer report: " + str(e))
        return report

    def get_errors_to_send(self, requester_id, only_one=False, persist_for=0) -> list:
        # print('@'*80)
        # pprint.pprint(self.errors)
        errors_to_send = []
        with self.errors_lock:
            for error in self.errors:
                sent_to_list = error.get('sent_to')
                if requester_id not in sent_to_list or time.monotonic() < error.get('when', 0) + persist_for:
                    if requester_id:
                        sent_to_list.add(requester_id)
                    if error.get('is_critical'):
                        level = 50
                    elif error.get('is_blocking'):
                        level = 40
                    elif error.get('is_info'):
                        level = 20
                    elif error.get('is_debug'):
                        level = 10
                    else:
                        level = 0
                    cleaned_error = {'code': error['code'], 'message': error['message'], 'level': level}
                    if cleaned_error not in errors_to_send:
                        if only_one:
                            return cleaned_error
                        errors_to_send.append(cleaned_error)
        return errors_to_send

    def get_last_error_to_display(self, requester_id=None, persist_for=0) -> dict:
        return self.get_errors_to_send(requester_id, only_one=True, persist_for=persist_for)

    def _forget_errors(self, sent_errors: list, sent_on: float) -> None:
        now = time.monotonic()
        with self.errors_lock:
            codes = [ error['code'] for error in sent_errors ]
            for error in self.errors:
                error_time = error.get('when', 0)
                if error_time < sent_on:
                    if now > error_time + self.FORGET_ERROR_AFTER:
                        self.errors.remove(error)

    #TODO refactor all the state change and errors system to use queue
    def register_error(self, code: int, message: str, is_blocking: bool = False, is_info: bool = False, is_critical: bool = False) -> None:
        # critical will cause close of the printer_interface and reconnection
        # blocking will cause error state which will make a cloud job to fail 
        # normal error will cause a state called connecting until it will disappear 
        # info will just show up, without triggering any events or state changes
        now = time.monotonic()
        with self.errors_lock:
            for existing_error in self.errors:
                if not existing_error.get('preconnect'):
                    if code == existing_error['code']:
                        existing_error['when'] = now
                        self.logger.info("Error repeat: N%d. %s" % (code, message))
                        return
        error = {"code": code, "message": message, "is_blocking": is_blocking, "when": now, "sent_to": set()}
        if is_info:
            error['is_info'] = True
        self.errors.append(error)
        if is_critical:
            if self.sender:
                self.add_post_answer_hook(self.close)
            else:
                self.close()
        self.logger.warning("Error N%d. %s" % (code, message))

    def register_event(self, event_dict: dict) -> None:
        if not event_dict in self.events:
            self.events.append(event_dict)

    def _get_possible_v2_profiles(self, vid=None, pid=None):
        if not vid or not pid:
            vid = self.usb_info.get('VID')
            pid = self.usb_info.get('PID')
        possible_profiles = []
        for profile in self.app.user_login.profiles:
            conns = profile.get('v2', {}).get('connections', [])
            for conn in conns:
                for conn_id_dict in conn.get('ids', []):
                    if isinstance(conn_id_dict, dict):
                        if conn_id_dict.get('VID') == vid and conn_id_dict.get('PID') == pid:
                            possible_profiles.append(profile)
                            break
                else:
                    continue # if we hadn't find a equal vid_pid, continue to next connection
                break # otherwise break, otherwise we'll add the profile again
        return possible_profiles

    def _get_possible_v1_profiles(self, vid=None, pid=None) -> list:
        return [profile for profile in self.app.user_login.profiles if [vid, pid] in profile['vids_pids']]

    def _get_possible_printer_profiles(self, vid=None, pid=None) -> list:
        if not vid or not pid:
            vid = self.usb_info.get('VID')
            pid = self.usb_info.get('PID')
        possible_profiles = self._get_possible_v2_profiles(vid, pid)
        possible_profiles_v1 = [profile for profile in self.app.user_login.profiles if [vid, pid] in profile['vids_pids'] and profile not in possible_profiles]
        possible_profiles = list(sorted(possible_profiles + possible_profiles_v1, key=lambda pt: pt['name']))
        self.logger.info("Possible printer types: " + str([profile.get('alias') for profile in possible_profiles]))
        return possible_profiles

    def _get_possible_conn_types(self, profile=None, vid=None, pid=None):
        possible_conns = []
        if not profile and not vid and not pid:
            self.logger.error(f'Unable to get possible connection types for {profile} {vid} {pid}')
        else:
            profiles = []
            if profile:
                profiles.append(profile)
            else:
                profiles.extend(self.app.user_login.profiles)
            for p in profiles:
                for conn in p.get('v2', {}).get('connections', []):
                    for conn_id_dict in conn.get('ids', []):
                        if not vid or (conn_id_dict.get('VID') == vid and conn_id_dict.get('PID') == pid):
                            possible_conns.append(conn)
        return possible_conns

    def request_printer_type_selection(self, printer_profile_or_alias: typing.Union[str, dict]) -> None:
        if type(printer_profile_or_alias) == dict:
            alias = printer_profile_or_alias['alias']
        elif type(printer_profile_or_alias) == str:
            alias = printer_profile_or_alias
        else:
            self.logger.exception("Invalid printer type selection request: %s" % printer_profile_or_alias)
            return
        self.logger.info("Setting printer type: %s" % alias)
        if not self.offline_mode:
            self.logger.info("Requesting printer type selection: %s" % alias)
            with self.requests_lock:
                self.requests_to_server['select_printer_type'] = alias
        self.set_printer_type(alias)

    def request_printer_groups_selection(self, groups: typing.List[dict]) -> None:
        with self.requests_lock:
            self.requests_to_server['selected_groups'] = groups
        self.logger.info("Groups to select: " + str(groups))

    def request_printer_rename(self, name: str) -> None:
        with self.requests_lock:
            self.requests_to_server['select_name'] = name
        self.logger.info("Requesting printer rename: " + str(name))

    def request_reset_printer_type(self) -> None:
        with self.requests_lock:
            if self.offline_mode:
                self.logger.info("Resetting printer type by reloading module")
                self.disconnect_flag = True
            else:
                self.logger.info("Setting up flag to reset printer type in next command request")
                self.requests_to_server['reset_printer_type'] = True
                self.add_post_answer_hook(self.close)
            self.reset_offline_printer_type()

    def _set_printer_name(self, name: str) -> None:
        self.printer_name = name

    def get_groups(self) -> typing.List[dict]:
        return self.groups

    def set_groups(self, groups: typing.List[dict]) -> None:
        if type(groups) == list:
            self.logger.info("Groups are set correctly for %s %s" % (str(self.usb_info), str(groups)))
            self.groups = groups
        else:
            self.register_error(999, "Invalid workgroups format - type must be list: " + str(groups), is_blocking=False)

    def turn_on_local_mode(self) -> None:
        self.local_mode = True

    def turn_off_local_mode(self) -> None:
        if self.sender:
            try:
                self.sender.flush_response_callbacks()
            except:
                pass
        self.local_mode = False
        try:
            self.sender.flush_response_callbacks()
        except (AttributeError, RuntimeError):
            pass

    def upload_logs(self) -> bool:
        self.logger.info("Sending logs")
        return not bool(log.report_problem('logs'))

    def report_camera_change(self, camera_name: str) -> None:
        if self.current_camera != camera_name:
            with self.requests_lock:
                self.requests_to_server['camera_change'] = camera_name
            self.logger.info("Reporting camera change to: " + str(camera_name))
            self.current_camera = camera_name

    def switch_camera(self, module: str, token: str = None) -> bool:
        self.logger.info('Changing camera module to %s due to server request' % module)
        if self.app.camera_controller.current_camera_name != module:
            self.app.camera_controller.switch_camera(module, token)
        return True

    def restart_camera(self, token: str = "") -> None:
        self.logger.info('Executing camera restart command from server')
        self.app.camera_controller.restart_camera(token)

    def update_software(self) -> None:
        self.logger.info('Executing update command from server')
        self.app.updater.update()

    def quit_application(self) -> None:
        self.logger.info('Received quit command from server!')
        self.app.stop_flag = True

    def set_name(self, name: str) -> None:
        self.logger.info("Setting printer name: " + str(name))
        self._set_printer_name(name)

    def is_downloading(self) -> bool:
        return self.downloader and self.downloader.is_alive()

    def cancel_locally(self) -> None:
        cancel_result = self.cancel()
        if cancel_result or cancel_result == None:
            self.register_error(117, "Canceled locally", is_blocking=False)
            self.register_event({'state': 'cancel'})
        else:
            self.register_error(1170, "Failed to canceled locally", is_blocking=False, is_info=True)

    def cancel(self) -> bool:
        if self.is_downloading():
            self.logger.info("Canceling downloading")
            self.downloader.cancel()
            return True
        else:
            self.logger.info("Canceling print")
            if self.sender:
                return self.sender.cancel()

    def set_verbose(self, verbose_enabled: bool) -> bool:
        try:
            if forced_settings.FORCED_SETTINGS.get('verbose') != False:
                if hasattr(self.sender, 'verbose'):
                    self.sender.verbose = bool(verbose_enabled)
                    self.logger.info("Setting sender verbose to %s" % bool(verbose_enabled))
                if hasattr(self.sender, 'connection') and hasattr(self.sender.connection, 'verbose'):
                    self.sender.connection.verbose = bool(verbose_enabled)
                    self.logger.info("Setting connection verbose to %s" % bool(verbose_enabled))
        except AttributeError:
            self.register_error(191, "Can't enable runtime verbose - no connection to printer", is_info=True)
            return False
        else:
            return True

    def send_bed_clear(self) -> bool:
        self.logger.info("Adding bed clear to request")
        with self.requests_lock:
            self.requests_to_server['bed_clear'] = True
        return True

    def close_printer_sender(self) -> None:
        if self.sender and not self.sender.stop_flag:
            self.logger.info('Closing ' + str(self.usb_info))
            self.sender.close()
            self.logger.info('...closed.')

    def close(self) -> None:
        # ensure that we close sender when we got exception in run
        if self.is_alive():
            self.logger.info('Closing printer interface of %s %s' % (getattr(self, "printer_name", "nameless printer"), str(self.usb_info)))
            self.stop_flag = True
        else:
            self.close_printer_sender()
            if self.server_connection:
                self.server_connection.close()

    def disconnect(self) -> None:
        # ensure that we close sender when we got exception in run
        self.logger.info('Reconnecting printer interface of %s %s' % (getattr(self, "printer_name", "nameless printer"), str(self.usb_info)))
        self.disconnect_flag = True

    def report_problem(self, problem_description: str) -> None: # TODO: delete? can't find usage
        log.report_problem(problem_description)

    def try_load_auth_token(self) -> None:
        self.printer_token = None
        for auth_token_usb_info, auth_token_value in self.app.user_login.auth_tokens:
            auth_token_usb_info = dict(auth_token_usb_info)
            for field_name in ('VID', 'PID', 'SNR'):
                if auth_token_usb_info.get(field_name) != self.usb_info.get(field_name):
                    break
            else:
                self.printer_token = auth_token_value
                self.logger.info("Auth token load success: " + str(self.printer_token))
                break

    def get_remaining_print_time(self) -> int:
        try:
            if self.sender:
                return self.sender.get_remaining_print_time()
        except AttributeError:
            pass

    def force_printer_type(self) -> None:
        forced_type = self.usb_info.get('forced_type')
        if forced_type:
            self.logger.info("Forcing type: %s" % forced_type)
            self.set_printer_type(forced_type)
        if not self.printer_profile:
            possible_printer_profiles = self._get_possible_printer_profiles(self.usb_info['VID'], self.usb_info['PID'])
            if len(possible_printer_profiles) == 1:
                self.printer_profile = possible_printer_profiles[0]
                type_alias = self.printer_profile.get('alias', '')
                self.save_printer_type(type_alias)
                self.logger.info("Autoselecting printer profile: %s" +  type_alias)
                self.type_request_in_progress = False
                if not self.connection_id:
                    poss_conns = self._get_possible_conn_types()
                    if poss_conns:
                        conn = poss_conns[0]
                        self.connection_id = conn.get('id')
                        self.connection_profile = conn

    def get_jobs_list(self) -> typing.Union[typing.List[dict], typing.Tuple[list, dict]]:
        self.logger.info("Requesting a jobs list")
        if self.server_connection:
            jobs_list = self.server_connection.get_jobs_list(self.printer_token)
            self.logger.info("Cloud's jobs list:\n" + pprint.pformat(jobs_list))
            return jobs_list
        return [], {"message": "No connection to server", "code": 9}

    def start_job_by_id(self, job_id: str) -> bool:
        if self.server_connection:
            self.logger.info(f"Sending a request to start a job {job_id}")
            return self.server_connection.start_job_by_id(self.printer_token, job_id)
        self.logger.info("No connection to server to start a job")
        return False

    def start_next_job(self, automatic=False) -> bool:
        if self.server_connection:
            self.logger.info("Sending a request to starting the next job")
            try:
                return self.server_connection.start_next_job(self.printer_token, automatic)
            except:
                return False
        self.logger.info("No connection to server to start a job")
        return False

    def set_printer_type(self, printer_profile_or_alias: typing.Union[dict, str] = "") -> None:
        alias = None
        if isinstance(printer_profile_or_alias, dict):
            alias = printer_profile_or_alias['alias']
        elif isinstance(printer_profile_or_alias, str):
            alias = printer_profile_or_alias
        if alias:
            self.type_request_in_progress = False
            for profile in self.app.user_login.profiles:
                if profile['alias'] == alias:
                    self.printer_profile = profile
                    self.save_printer_type(alias)
                    if not self.connection_id:
                        poss_conns = self._get_possible_conn_types(self.printer_profile)
                        if poss_conns:
                            conn = poss_conns[0]
                            self.connection_id = conn.get('id')
                            self.connection_profile = conn
                        self.logger.info("Printer profile set to: %s", profile)
                    break
        else:
            # try to automatically set profile when no selection required due to single vid pid match
            possible_printer_profiles = self._get_possible_printer_profiles(self.usb_info['VID'], self.usb_info['PID'])
            if len(possible_printer_profiles) == 1:
                self.printer_profile = possible_printer_profiles[0]
                type_alias = self.printer_profile.get('alias', '')
                self.save_printer_type(type_alias)
                self.logger.info("Autoselecting printer profile: %s" +  type_alias)
                self.type_request_in_progress = False
                if not self.connection_id:
                    poss_conns = self._get_possible_conn_types()
                    if poss_conns:
                        conn = poss_conns[0]
                        self.connection_id = conn.get('id')
                        self.connection_profile = conn

    def load_printer_type(self) -> None:
        if self.printer_settings:
            printer_type_alias = self.printer_settings.get('type_alias')
            if printer_type_alias:
                self.set_printer_type(printer_type_alias)

    def save_printer_type(self, printer_type_alias: str = "") -> None:
        self.printer_settings['type_alias'] = printer_type_alias
        printer_settings_and_id.save_settings(self.id_string, self.printer_settings)

    def reset_offline_printer_type(self) -> None:
        if 'type_alias' in self.printer_settings:
            printer_settings_and_id.reset_printer_settings(self.id_string)

    def enable_local_mode(self, start_timeout_thread: bool = True) -> None:
        with self.local_mode_lock:
            self.local_mode_enable_time = time.monotonic()
            if not self.local_mode:
                self.local_mode = True
                self.logger.info("Local mode enabled")
                if start_timeout_thread and not self.local_mode_timeout_thread:
                    self.local_mode_timeout_thread = threading.Thread(target=self._local_mode_timeout_tracking)
                    self.local_mode_timeout_thread.start()

    def disable_local_mode(self) -> None:
        with self.local_mode_lock:
            self.logger.info("Local mode disabled")
            self.local_mode = False

    def _local_mode_timeout_tracking(self) -> None:
        while not self.disconnect_flag:
            if not self.local_mode or time.monotonic() > self.local_mode_enable_time + self.local_mode_timeout:
                break
            time.sleep(0.1)
        with self.local_mode_lock:
            if self.local_mode:
                self.logger.info("Local mode disabled")
                self.local_mode = False
                self.local_mode_timeout_thread = None

    def _set_cloud_job_id_and_snr(self, server_message: dict) -> None:
        if server_message:
            clouds_job_id = server_message.get('job_id')
            clouds_job_snr = server_message.get('job_snr', clouds_job_id)
            try:
                if self.sender and self.sender.SUPPORT_JOBS:
                    self.sender.clouds_job_id = clouds_job_id
                    self.sender.clouds_job_snr = clouds_job_snr
            except AttributeError:
                pass

    def remember_printer(self, printer_id_dict={}, printer_type=None, detect_snr=False, auth={}, conn_type=None, conn_id=None):
        self.logger.info(f'Command to remember a printer: {printer_id_dict}, {printer_type}')
        vid = printer_id_dict.get('VID')
        pid = printer_id_dict.get('PID')
        ip = printer_id_dict.get('IP')
        port = printer_id_dict.get('PORT')
        snr = printer_id_dict.get('SNR')
        detect_snr = bool(printer_id_dict.get('detect_snr'))
        profile = None
        if printer_type:
            possible_vids_pids = []
            for profile in self.app.user_login.profiles:
                if profile.get('alias') == printer_type:
                    if conn_type or conn_id:
                        for conn_dict in self._get_possible_conn_types(profile):
                            if conn_id:
                                if conn_id == conn_dict.get('id'):
                                    for id_dict in conn_dict.get('ids', []):
                                        if type(id_dict) == dict:
                                            possible_vids_pids.append([id_dict.get('VID'), id_dict.get('PID')])
                                    conn_type = conn_dict.get('type')
                            elif conn_type and conn_type == conn_dict.get('type'):
                                for id_dict in conn_dict.get('ids', []):
                                    if type(id_dict) == dict:
                                        possible_vids_pids.append([id_dict.get('VID'), id_dict.get('PID')])
                                conn_id = conn_dict.get('id')
                    else:
                        possible_vids_pids.extend(profile.get('vids_pids'))
        else:
            possible_profiles = self._get_possible_printer_profiles(vid, pid)
            possible_vids_pids = [vid, pid]
            if any(possible_profiles):
                profile = possible_profiles[0]
            else:
                self.register_error(222, f"Unknown printer_id and no printer_type: {printer_id_dict}", is_blocking=False)
                return False
        if not auth:
            auth = {} #TODO temp protection against cloud bug of auth : None
        if vid and pid:
            possible_vids_pids = [vid, pid]
        if not ip:
            ip = auth.get('IP')
        password = auth.get('password')
        ssh_password = auth.get('ssh_password')
        force_serial_number = auth.get('serial_number')
        if force_serial_number != None:
            snr = force_serial_number
        if not conn_id and conn_type and printer_type:
            for profile in self.app.user_login.profiles:
                if profile.get('alias') == printer_type:
                    for conn in self._get_possible_conn_types(profile, vid, pid):
                        if conn.get('type') == conn_type:
                            conn_id = conn.get('id')
                            break
        if conn_type == 'LAN' or (conn_type == None and profile and profile.get('network_detect')):
            if not ip:
                self.register_error(223, "No IP provided, but is required. Add it to printer_id_dict or auth or detect_snr should be true", is_blocking=False)
                return False
            if not snr:
                snr = ip
            try:
                network_detector = self.app.detectors['NetworkDetector']
            except AttributeError:
                return False
            if vid and pid and snr:
                settings = {}
                if printer_type:
                    settings['type_alias'] = printer_type
                if conn_id:
                    settings['connection_id'] = conn_id
                if settings:
                    if snr:
                        printer_id_string = printer_settings_and_id.create_id_string({'VID': vid, 'PID': pid, 'SNR': snr}) 
                        printer_settings_and_id.save_settings(printer_id_string, settings)
            success = network_detector.remember_printer(printer_type, ip, port, vid, pid, snr, password, ssh_password, detect_snr, conn_id)
            if success:
                return True
            self.register_error(224, f"Error on attempt to remember a printer {printer_id_dict}, {ip}, {port}, {vid}, {pid}, {snr}, {detect_snr}", is_blocking=False)
        elif conn_id or printer_type:
            conn_settings = {'connection_id': conn_id, 'type_alias': printer_type}
            try:
                for pi in self.app.printer_interfaces:
                    if not pi.printer_settings.get('used') and pi.usb_info and [pi.usb_info.get('VID'),  pi.usb_info.get('PID')] in possible_vids_pids:
                        if snr and snr != pi.usb_info.get('SNR'):
                            continue
                        pi.set_printer_type(printer_type)
                        if conn_id:
                            pi.set_connection(conn_id, apply_restart=True)
                        return True
                else:
                    for vid, pid in possible_vids_pids:
                        if snr:
                            printer_id_string = printer_settings_and_id.create_id_string({'VID': vid, 'PID': pid, 'SNR': snr}) 
                            printer_settings_and_id.save_settings(printer_id_string, conn_settings)
                        else:
                            with self.app.volatile_printer_settings_lock:
                                self.app.volatile_printer_settings[vid + "_" + pid] = conn_settings
                                self.logger.info(f'Creating volatile settings for all new printers with [{vid, pid}]: {conn_settings}')
                    return True
            except Exception:
                self.logger.exception('Exception on restarting a printer profile to apply a connection type')
        return False

    def set_connection(self, conn_id: str, apply_restart=False) -> bool:
        if not self.printer_profile:
            self.register_error(237, f"Can't switch to connection {conn_id} - no printer type selected")
        else:
            for connection in self.printer_profile.get('v2', {}).get('connections', []):
                try:
                    if connection.get('id') == conn_id:
                        self.printer_settings['connection_id'] = conn_id
                        printer_settings_and_id.save_settings(self.id_string, self.printer_settings)
                        self.connection_profile = connection
                        self.logger.info('Connection type set to ' + conn_id)
                        if not conn_id == self.connection_id:
                            self.connection_id = conn_id
                            if apply_restart:
                                self.close()
                        return True
                except:
                    self.logger.error(f'Invalid connection in profile {self.printer_profile["name"]}\t{connection}')
            self.register_error(237, f"Unable switch to connection {conn_id} - no such connection type in the printer's profile")
        return False

    def forget_printer(self, printer_id_dict):
        self.logger.info(f'Command to forget_printer a printer: {printer_id_dict}')
        if self.app:
            network_detector = self.app.detectors.get('NetworkDetector')
            if network_detector:
                try:
                    printer_settings_and_id.reset_printer_settings(printer_id_dict)
                except:
                    pass
                return network_detector.forget_printer(printer_id_dict)
        return False

    def _load_volatile_settings(self):
        volatile_settings_id = self.usb_info['VID'] + "_" +self.usb_info['PID']
        if self.app and getattr(self.app, 'volatile_printer_settings_lock'):
            with self.app.volatile_printer_settings_lock:
                settings = self.app.volatile_printer_settings.get(volatile_settings_id)
                if settings:
                    self.printer_settings.update(settings)
                    printer_settings_and_id.save_settings(self.id_string, settings)
                    self.app.volatile_printer_settings.clear()

    def add_post_answer_hook(self, hook_call, hook_args=None):
        if hook_args:
            hook_tuple = (hook_call, hook_args)
        else:
            hook_tuple = (hook_call,)
        if not hook_tuple in self.post_answer_hooks:
            self.post_answer_hooks.append(hook_tuple)

    def execute_hook(self, hook):
        try:
            if len(hook) > 1:
                hook[0](*hook[1])
            else:
                hook[0]()
        except Exception as e:
            self.logger.exception('Exception on running hook {hook}: ' + str(e))

    def set_disconnect_flag(self, value=True):
        self.disconnect_flag = value

    def reconnect_gracefully(self, reason: str="") -> None:
        if self.sender and not self.disconnect_flag:
            if reason:
                self.logger.info('Reconnection requested ' + reason)
            else:
                self.logger.info('Reconnection requested')
            self.register_event({'state': printer_states.CONNECTING_STATE})
            self.add_post_answer_hook(self.set_disconnect_flag)

    def edit_printer(self, edit_dict=None):
        if not edit_dict: #protection against edit_dict == None
            printer_dict = {}
            printer_dict.update(self.printer_settings)
            printer_dict.update(self.usb_info)
            # if not printer_dict.get('PASS') is None:
            #     printer_dict['PASS'] = base_detector.BaseDetector.HIDDEN_PASSWORD_MASK
            return printer_dict
        else:
            if not isinstance(edit_dict, dict):
                self.logger.error(f'Printer edit argument should be dict not ' + str(type(edit_dict)))
                return False
            if self.get_printer_state() == printer_states.PRINTING_STATE or \
               self.get_printer_state() == printer_states.PAUSED_STATE:
                self.logger.warning('This edit is prohibited when printing or paused')
            else:
                printer_id_changed = False
                printer_settings_changed = False
                for key, new_value in edit_dict.items():
                    # if key in base_detector.BaseDetector.PRINTER_ID_DICT_KEYS:
                    #     if self.usb_info.get(key) != new_value:
                    #         self.logger.warning(f'Edit of {key} forbidden')
                    #         printer_id_changed = True
                    if key in base_detector.BaseDetector.PRINTER_NONID_KEYS:
                        if self.usb_info.get(key) != new_value:
                            self.logger.info(f'Minor printer id keys: {self.usb_info.get(key)} -> {new_value}')
                            # if key != 'PASS' and new_value != base_detector.BaseDetector.HIDDEN_PASSWORD_MASK:
                            #     self.usb_info[key] = new_value
                            #     printer_id_changed = True
                            self.usb_info[key] = new_value
                            printer_id_changed = True
                    else:
                        if self.printer_settings.get(key) != new_value:
                            self.logger.info(f'Printer settings: {self.printer_settings.get(key)} -> {new_value}')
                            self.printer_settings[key] = new_value
                            printer_settings_changed = True
                if printer_settings_changed:
                    printer_settings_and_id.save_settings(self.id_string, self.printer_settings)
                if printer_id_changed:
                    new_printer_id = {}
                    new_printer_id.update(self.usb_info)
                    for key in base_detector.BaseDetector.ALL_KEYS:
                        if key in edit_dict:
                            new_printer_id[key] = edit_dict[key]
                    net_detector = self.app.detectors['NetworkDetector']
                    static_detector = self.app.detectors['StaticDetector']
                    if not net_detector or not net_detector.edit_printer(new_printer_id):
                        if not static_detector.edit_printer(new_printer_id):
                            self.logger.error(f'Unable to find a printer to edit with: ' + str(new_printer_id))
                            return False
                if self.get_printer_state() in (printer_states.READY_STATE, printer_states.ERROR_STATE, printer_states.CONNECTING_STATE):
                    self.register_error(199, 'Reconnecting to apply new settings', is_critical=True)
                return True

    # def save_settings(self):
    #     printer_settings_and_id.save_settings(self.id_string, self.printer_settings)
