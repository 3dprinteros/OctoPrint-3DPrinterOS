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

import config
import downloader
import http_client
import log
import os
import paths


class PrinterInterface(threading.Thread):

    DEFAULT_OPERATIONAL_TIMEOUT = 10
    DEFAULT_LOCAL_MODE_TIMEOUT = 2
    ON_CONNECT_TO_PRINTER_FAIL_SLEEP = 3
    APIPRINTER_REG_PERIOD = 2
    FORCE_FORGET_ERROR_AFTER = 12
    LOOP_SLEEP_STEPS = 10

    COMMANDS_ALLOWED_WHEN_ERROR = ""
    ID_SEPARATOR = "_"

    def __init__(self, parent, usb_info, command_request_period = 5, offline_mode=False, forced_printer_profile={}):
        self.command_request_period = command_request_period
        self.parent = parent
        self.usb_info = usb_info
        self.id_string = self.create_id_string()
        super().__init__(name="PrinterInterface",  daemon=True)
        if config.get_settings().get('logging', {}).get('per_printer', False):
            self.logger = log.create_logger(self.id_string, subfolder=log.PRINTERS_SUBFOLDER)
        else:
            self.logger = logging.getLogger(self.id_string)
        self.creation_time = time.monotonic()
        self.stop_flag = False
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
        self.event_reports = collections.deque() # list of printer events that have to be sent to the server
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
        self.possible_printer_types = self.get_possible_printer_types()
        self.current_camera = self.parent.camera_controller.get_current_camera_name()
        self.try_load_auth_token()
        self.registration_code = None
        self.registration_email = ""
        self.offline_mode = offline_mode
        self.get_print_estimations_from_cloud = config.get_settings().get('print_estimation', {}).get('by_cloud', False)
        self.check_request_dumping_flag()
        self.load_printer_type()
        self.force_printer_type()
        self.jobs = []
        self.materials = []
        # following fields will be removed from report if their contents will not change between reports
        # but field is special and doing the same, but is checked per item and only changed items got to report
        self.stored_dynamic_fields = {
            'line_number': None,
            'coords': [],
            'material_names': [],
            'material_volumes': [],
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

    def create_id_string(self):
        unsafe_string = self.usb_info['VID'] + self.ID_SEPARATOR + \
                    self.usb_info['PID'] + self.ID_SEPARATOR + \
                    self.usb_info['SNR']
        id_string = ""
        for char in unsafe_string:
            if char.isalnum():
                id_string += char
            else:
                id_string += self.ID_SEPARATOR
        return id_string

    def connect_to_server(self):
        self.logger.info("Connecting to server with printer: %s" , str(self.usb_info))
        if self.printer_token:
            self.server_connection = self.server_connection_class(self)
            return True
        if self.parent.user_login.user_token:
            return self.register_with_streamerapi()
        return self.register_with_apiprinter()

    def register_with_streamerapi(self):
        kw_message = {}
        while not self.stop_flag and not self.parent.stop_flag:
            if [error for error in self.errors if error['is_blocking']]:
                self.stop_flag = True
                return False
            if not self.server_connection:
                self.server_connection = self.server_connection_class(self)
            if self.parent.offline_mode or self.offline_mode:
                self.offline_mode = True
                return True
            message = [http_client.HTTPClient.PRINTER_LOGIN, self.parent.user_login.user_token, self.usb_info]
            self.logger.info('Printer login request:\n%s\n%s ' % (str(message), str(kw_message)))
            answer = self.server_connection.pack_and_send(*message, **kw_message)
            if answer:
                self.printer_name = answer.get('name', '')
                error = answer.get('error')
                if error:
                    self.logger.warning("Error while login %s: %s %s", self.usb_info,
                                                                       error.get('code'),
                                                                       error.get("message"))
                    if str(error['code']) == '8':
                        if self.printer_profile:
                            kw_message['select_printer_type'] = self.printer_profile['alias']
                        else:
                            self.show_printer_type_selector = True
                    else:
                        self.register_error(26, "Critical error on printer login: %s" % error)
                        time.sleep(self.command_request_period)
                        return False
                else:
                    groups = answer.get('current_groups')
                    if groups:
                        self.set_groups(groups)
                    self.show_printer_type_selector = False
                    self.logger.info('Successfully connected to server.')
                    #self.logger.info('Printer login response: ' + str(answer))
                    try:
                        self.printer_token = answer['printer_token']
                        self.printer_profile = json.loads(answer["printer_profile"])
                        if type(self.printer_profile) != dict:
                            raise ValueError("Profile should have type dict")
                    except (ValueError, TypeError) as e:
                        self.logger.error("Server responded with invalid printer profile. Error: %s" % e)
                    else:
                        self.logger.debug('Setting profile: ' + str(self.printer_profile))
                    return True
                self.logger.warning("Error on printer login. No connection or answer from server.")
            time.sleep(self.command_request_period)

    def register_with_apiprinter(self):
        while not self.stop_flag and not self.parent.stop_flag:
            if [error for error in self.errors if error['is_blocking']]:
                self.stop_flag = True
                return False
            if not self.printer_profile:
                self.logger.error("No forced profile for the printer but in APIPrinter mode: %s" , self.usb_info)
                return False
            if not self.server_connection:
                self.server_connection = self.server_connection_class(self)
            if self.parent.offline_mode or self.offline_mode:
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
                    save_result = self.parent.user_login.save_printer_auth_token(vid_pid_snr_only_usb_info, auth_token)
                    self.logger.info(f"Auth token save result: {save_result}")
                    self.restart_camera(auth_token)
                    return True
            time.sleep(self.APIPRINTER_REG_PERIOD)

    def connect_to_printer(self):
        sender_name = self.printer_profile['sender']
        custom_timeout = self.printer_profile.get("operational_timeout", None)
        if custom_timeout:
            self.timeout = custom_timeout
        try:
            printer_sender = __import__(sender_name)
        except ImportError:
            message = "Printer type %s not supported by this version of 3DPrinterOS client." % sender_name
            self.register_error(128, message, is_blocking=True)
            if not self.offline_mode:
                self.requests_to_server['reset_printer_type'] = True
                message, kw_message = self.form_command_request(None)
                self.server_connection.pack_and_send('command', *message, **kw_message)
            else:
                self.save_printer_type(None)
        else:
            self.logger.info("Connecting with profile: " + str(self.printer_profile))
            try:
                printer = printer_sender.Sender(self, self.usb_info, self.printer_profile)
            except RuntimeError as e:
                message = "Can't connect to printer %s %s\nReason: %s" %\
                          (self.printer_profile.get('name'), str(self.usb_info), str(e))
                self.register_error(119, message, is_blocking=True)
            else:
                self.logger.info("Successful connection to %s!" % self.printer_profile['name'])
                return printer
        time.sleep(self.ON_CONNECT_TO_PRINTER_FAIL_SLEEP)

    def form_command_request(self, acknowledge):
        message = [self.printer_token, self.status_report(), acknowledge]
        kw_message = {}
        errors_to_send = self.get_errors_to_send()
        if errors_to_send:
            kw_message['error'] = errors_to_send
        self.check_camera_name()
        with self.requests_lock:
            kw_message.update(self.requests_to_server)
            self.requests_to_server = {}
        requests_to_stop_after = ['reset_printer_type']
        for stop_after in requests_to_stop_after:
            if stop_after in kw_message:
                self.stop_in_next_loop_flag = True
                break
        if self.offline_mode:
            self.logger.info("Status:\n%s\n%s" % (str(message), str(kw_message)))
        if self.request_dumping:
            dump = {"token": message[0]}
            dump.update({"report": message[1]})
            dump.update({"ack": message[2]})
            dump.update(kw_message)
            self.dump_request(dump, 'out')
        return message, kw_message

    def check_operational_status(self, last_operational_time):
        if self.sender:
            if self.sender.is_operational():
                self.forced_state = None
                return True
            if not self.forced_state:
                message = "Printer is not operational"
                self.register_error(77, message, is_blocking=False)
            elif self.forced_state != "error" and last_operational_time + self.timeout < time.monotonic():
                message = "Not operational timeout reached"
                self.register_error(78, message, is_blocking=True)
        return False

    @log.log_exception
    def run(self):
        if self.offline_mode:
            while not self.printer_profile:
                self.show_printer_type_selector = True
                if self.stop_flag or self.parent.stop_flag:
                    self.close_printer_sender()
                    self.logger.info('Printer interface was stopped')
                    return
                time.sleep(0.1)
        else:
            if not self.connect_to_server():
                return
        self.sender = self.connect_to_printer()
        last_operational_time = time.monotonic()
        acknowledge = None
        send_reset_job = True
        kw_message_prev = {}
        event = None
        stop_in_next_loop_flag = False
        while not self.parent.stop_flag and not self.stop_flag:
            loop_start_time = time.monotonic()
            for error in self.errors:
                if error.get("cancel"):
                    self.forced_state = "cancel"
                elif error["is_blocking"]:
                    self.forced_state = "error"
                    #self.close_printer_sender()
                    stop_in_next_loop_flag = True
                    break
            if self.check_operational_status(last_operational_time):
                last_operational_time = time.monotonic()
            message, kw_message = self.form_command_request(acknowledge)
            for key in kw_message_prev:
                if key not in kw_message:
                    kw_message[key] = kw_message_prev[key]
            if not event and self.event_reports:
                event = self.event_reports.popleft()
                self.logger.info(f'Event to be sent: {event}')
                message[1].update(event)
            if send_reset_job:
                kw_message['reset_job'] = True
                send_reset_job = False
            if self.offline_mode:
                self.forget_errors(kw_message.get("error", []))
                kw_message_prev = {}
                event = None
            else:
                answer = self.server_connection.pack_and_send('command', *message, **kw_message)
                if self.request_dumping:
                    self.dump_request(answer, 'in')
                if answer is not None:
                    self.forget_errors(kw_message.get("error", []))
                    self.update_stored_dynamic_fields(message)
                    #self.logger.info("Answer: " + str(answer))
                    acknowledge = self.execute_server_command(answer)
                    kw_message_prev = {}
                    event = None
                else:
                    kw_message_prev = copy.deepcopy(kw_message)
            if stop_in_next_loop_flag:
                self.stop_flag = True
            sleep_time = loop_start_time - time.monotonic() + self.command_request_period
            if sleep_time > 0:
                steps_left = self.LOOP_SLEEP_STEPS
                while steps_left and not self.stop_flag and not self.parent.stop_flag:
                    time.sleep(sleep_time/self.LOOP_SLEEP_STEPS)
                    steps_left -= 1
        if self.server_connection:
            self.server_connection.close()
        self.close_printer_sender()
        self.logger.info('Printer interface was stopped')

    def update_stored_dynamic_fields(self, message):
        if message and len(message) > 2:
            report = message[1]
            for key, value in self.stored_dynamic_fields.items():
                if key != 'ext':
                    new_value = report.get(key)
                    if new_value or new_value == 0:
                        self.stored_dynamic_fields[key] = new_value
                else: # to same for ext level as for upper level # TODO make recursive and remove copy-paste
                    for ext_key, ext_value in value.items():
                        new_ext_value = report.get('ext', {}).get(ext_key)
                        if new_ext_value or new_value == 0:
                            self.stored_dynamic_fields['ext'][ext_key] = new_ext_value

    def get_method_by_command_name(self, command):
        if command:
            method = getattr(self, command, None)
            if not method:
                try:
                    if self.sender:
                        method = getattr(self.sender, command, None)
                except:
                    pass
            return method

    def validate_command(self, server_message):
        try:
            command = server_message.get('command')
            if command: #no command is a valid scenario
                try:
                    number = int(server_message.get('number'))
                except (ValueError, TypeError):
                    self.register_error(112, f"Cannot execute server command {command} due to invalid number: {server_message.get('number')}.", is_blocking=False)
                    return False
                if not self.sender:
                    self.register_error(110, f"Cannot execute server command {command} in printer error state.", is_blocking=False)
                    return False
                if self.local_mode:
                    self.register_error(111, "Can't execute command %s while in local_mode!" % command, is_blocking=False)
                    return False
                if not command or "__" in command or "." in command or \
                        command.startswith('_') or hasattr(threading.Thread, command):
                    self.register_error(111, f"Cannot execute invalid command: {command}.", is_blocking=False)
                    return False
                method = self.get_method_by_command_name(command)
                if not method:
                    self.register_error(40, f"Unknown command: {command}", is_blocking=False)
                    return False
        except Exception as e:
            self.register_error(113, f'Exception on command validation: {server_message}\n{e}', is_blocking=False)
            self.logger.exception(f"Exception on command validation: {server_message}")
            return False
        return True

    def execute_server_command(self, server_message):
        is_valid = self.validate_command(server_message)
        if not is_valid:
            return {"number": server_message.get('number'), "result": False}
        error = server_message.get('error')
        if error:
            self.logger.warning("@ Server return error: %s\t%s" % (error.get('code'), error.get('message')))
        command, number = server_message.get('command'), int(server_message.get('number', 0))
        if command:
            self.logger.info(f"Command received: " + pprint.pformat(server_message))
            self.logger.info("Executing command number %s : %s" % (number, str(command)))
            method = self.get_method_by_command_name(command)
            payload = server_message.get('payload')
            if not method:
                if not self.sender:
                    self.register_error(110, f"Cannot execute server command {command} in printer error state.")
                    return { "number": number, "result": False }
                else:
                    method = getattr(self.sender, command, None)
                if not method:
                    self.register_error(111, f"Cannot execute server's unsupported command: {command}.")
                    return { "number": number, "result": False }
            if server_message.get('is_link'):
                if self.downloader and self.downloader.is_alive():
                    self.register_error(108, "Can't start new download, because previous download isn't finished.")
                    result = False
                else:
                    self.set_cloud_job_id_and_snr(server_message)
                    if command == 'gcodes':
                        self.sender.set_filename(server_message.get('filename', ''))
                        self.sender.filesize = server_message.get('size', 0)
                        filename = server_message.get('filename')
                        self.sender.set_filename(filename)
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
                    method = self.get_method_by_command_name('unbuffered_gcodes_base64')
                if 'payload' in server_message:
                    if type(payload) is list:
                        arguments = payload
                    else:
                        arguments = [payload]
                else:
                    arguments = []
                try:
                    result = method(*arguments)
                    # to reduce needless 'return True' in methods, we assume that return of None, that is a successful call
                    result = result or result == None
                except Exception as e:
                    message = "! Error while executing command %s, number %s.\t%s" % (command, number, str(e))
                    self.register_error(109, message, is_blocking=False)
                    self.logger.exception(message)
                    result = False
            ack = { "number": number, "result": result }
            return ack

    def get_printer_state(self):
        if self.forced_state:
            state = self.forced_state
        elif self.stop_flag or not self.sender or self.sender.stop_flag:
            state = "connecting"
        elif self.sender.is_paused():
            state = "paused"
        elif self.downloader and self.downloader.is_alive():
            state = "downloading"
        elif self.sender.is_printing():
            state = "printing"
        elif self.local_mode:
            state = "local_mode"
        elif self.sender.is_bed_not_clear():
            state = "bed_not_clear"
        else:
            state = "ready"
        return state

    def status_report(self):
        report = {"state": self.get_printer_state()}
        if self.sender:
            try:
                if self.is_downloading():
                    report["percent"] = self.sender.get_downloading_percent()
                else:
                    report["percent"] = self.sender.get_percent()
                report["temps"] = self.sender.get_temps()
                report["target_temps"] = self.sender.get_target_temps()
                report["line_number"] = self.sender.get_current_line_number()
                report["coords"] = self.sender.get_position()
                report["material_names"] = self.sender.get_material_names()
                report["material_volumes"] = self.sender.get_material_volumes()
                report["estimated_time"] = self.sender.get_estimated_time()
                report["ext"] = self.sender.get_ext()
                for key in self.stored_dynamic_fields:
                    new_value = report.get(key)
                    if key != 'ext':
                        if key in report and (not new_value and new_value != 0) or new_value == self.stored_dynamic_fields[key]:
                            if key in report:
                                del report[key]
                    else: # to same for ext level as for upper level # TODO make recursive and remove copy-paste
                        for ext_key in self.stored_dynamic_fields['ext']:
                            new_ext_value = report['ext'].get(ext_key)
                            if ext_key in report['ext'] \
                                and (not new_ext_value and new_ext_value != 0) \
                                or new_ext_value == self.stored_dynamic_fields['ext'][ext_key]:
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

    def get_errors_to_send(self):
        errors_to_send = []
        with self.errors_lock:
            for error in self.errors:
                if not error.get('sent'):
                    errors_to_send.append(error)
        return errors_to_send

    def get_last_error_to_display(self):
        with self.errors_lock:
            if self.errors:
                try:
                    last_error = [error for error in self.errors if not error.get('displayed')][-1]
                    displayed_error = dict(last_error)
                    last_error['displayed'] = True
                    return displayed_error
                except IndexError:
                    pass

    def forget_errors(self, sent_errors):
        now = time.monotonic()
        with self.errors_lock:
            for error in sent_errors:
                error['sent'] = True
            for error in self.errors:
                if not error.get('displayed'):
                    if now > error['when'] + self.FORCE_FORGET_ERROR_AFTER:
                        error['displayed'] = True
                if error.get('sent') and error.get('displayed'):
                    self.errors.remove(error)

    #TODO refactor all the state change and errors system to use queue
    def register_error(self, code, message, is_blocking=False, static=False, is_info=False, cancel=False):
        with self.errors_lock:
            error = {"code": code, "message": message, "is_blocking": is_blocking, "when": time.monotonic()}
            if is_info:
                error['is_info'] = True
            if static:
                error['static'] = True
            if cancel:
                error['cancel'] = True
            for existing_error in self.errors:
                if error['code'] == existing_error['code']:
                    self.logger.info("Error(repeat) N%d. %s" % (code, message))
                    break
            else:
                self.errors.append(error)
            self.logger.warning("Error N%d. %s" % (code, message))

    def register_event_report(self, event_dict):
        self.event_reports.append(event_dict)

    def check_camera_name(self):
        camera_name = self.parent.camera_controller.get_current_camera_name()
        if self.current_camera != camera_name:
            self.logger.info("Camera change detected")
            with self.requests_lock:
                self.requests_to_server['camera_change'] = camera_name
            self.current_camera = camera_name

    def get_possible_printer_types(self):
        try:
            current_printer_vid_pid = [self.usb_info['VID'], self.usb_info['PID']]
        except (KeyError, TypeError):
            self.logger.error('Invalid printer id: %s' % self.usb_info)
            possible_types = []
        else:
            possible_types = [profile for profile in self.parent.user_login.profiles if current_printer_vid_pid in profile['vids_pids']]
            possible_types = list(sorted(possible_types, key=lambda pt: pt['name']))
            self.logger.info("Possible printer types: " + str(possible_types))
        return possible_types

    def request_printer_type_selection(self, printer_profile_or_alias):
        if type(printer_profile_or_alias) == dict:
            alias = printer_profile_or_alias['alias']
        elif type(printer_profile_or_alias) == str:
            alias = printer_profile_or_alias
        else:
            self.logger.exception("Invalid printer type selection request: %s" % printer_profile_or_alias)
            return
        self.logger.info("Setting printer type: %s" % alias)
        self.set_printer_type(alias)
        if not self.offline_mode:
            self.logger.info("Requesting printer type selection: %s" % alias)
            with self.requests_lock:
                self.requests_to_server['select_printer_type'] = alias

    def request_printer_groups_selection(self, groups):
        with self.requests_lock:
            self.requests_to_server['selected_groups'] = groups
        self.logger.info("Groups to select: " + str(groups))

    def request_printer_rename(self, name):
        with self.requests_lock:
            self.requests_to_server['select_name'] = name
        self.logger.info("Requesting printer rename: " + str(name))

    def request_reset_printer_type(self):
        with self.requests_lock:
            if self.offline_mode:
                self.logger.info("Reseting printer type by reloading module")
                self.stop_flag = True
            else:
                self.logger.info("Setting up flag to reset printer type in next command request")
                self.requests_to_server['reset_printer_type'] = True
            self.reset_offline_printer_type()

    def set_printer_name(self, name):
        self.logger.info("Setting printer name: " + str(name))
        self.printer_name = name

    def get_groups(self):
        return self.groups

    def set_groups(self, groups):
        if type(groups) == list:
            self.logger.info("Groups are set correctly for %s %s" % (str(self.usb_info), str(groups)))
            self.groups = groups
        else:
            self.register_error(999, "Invalid workgroups format - type must be list: " + str(groups), is_blocking=False)

    def turn_on_local_mode(self):
        self.local_mode = True

    def turn_off_local_mode(self):
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

    def upload_logs(self):
        self.logger.info("Sending logs")
        if log.report_problem('logs'):
            return False

    def switch_camera(self, module):
        self.logger.info('Changing camera module to %s due to server request' % module)
        self.parent.camera_controller.switch_camera(module)
        return True

    def restart_camera(self, token=None):
        self.logger.info('Executing camera restart command from server')
        self.parent.camera_controller.restart_camera(token)

    def update_software(self):
        self.logger.info('Executing update command from server')
        self.parent.updater.update()

    def quit_application(self):
        self.logger.info('Received quit command from server!')
        self.parent.stop_flag = True

    def set_name(self, name):
        self.logger.info("Setting printer name: " + str(name))
        self.set_printer_name(name)

    def is_downloading(self):
        return self.downloader and self.downloader.is_alive()

    def cancel_locally(self):
        cancel_result = self.cancel()
        if cancel_result or cancel_result == None:
            self.register_error(117, "Canceled locally", is_blocking=False, cancel=True)
        else:
            self.register_error(1170, "Failed to canceled locally", is_blocking=False, is_info=True)

    def cancel(self):
        if self.is_downloading():
            self.logger.info("Canceling downloading")
            self.downloader.cancel()
            return True
        else:
            self.logger.info("Canceling print")
            if self.sender:
                return self.sender.cancel()

    def set_verbose(self, verbose_enabled):
        try:
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

    def send_bed_clear(self):
        self.logger.info("Adding bed clear to request")
        with self.requests_lock:
            self.requests_to_server['bed_clear'] = True
        return True

    def close_printer_sender(self):
        if self.sender and not self.sender.stop_flag:
            self.logger.info('Closing ' + str(self.usb_info))
            self.sender.close()
            self.logger.info('...closed.')

    def close(self):
        # ensure that we close sender when we got exception in run
        if self.is_alive():
            self.logger.info('Closing printer interface of %s %s' % (getattr(self, "printer_name", "nameless printer"), str(self.usb_info)))
            self.stop_flag = True
        else:
            self.close_printer_sender()
            if self.server_connection:
                self.server_connection.close()

    def report_problem(self, problem_description):
        log.report_problem(problem_description)

    def try_load_auth_token(self):
        self.printer_token = None
        for auth_token_usb_info, auth_token_value in self.parent.user_login.auth_tokens:
            auth_token_usb_info = dict(auth_token_usb_info)
            for field_name in ('VID', 'PID', 'SNR'):
                if auth_token_usb_info.get(field_name) != self.usb_info.get(field_name):
                    break
            else:
                self.printer_token = auth_token_value
                self.logger.info("Auth token load success: " + str(self.printer_token))
                break

    def get_remaining_print_time(self):
        try:
            if self.sender:
                return self.sender.get_remaining_print_time()
        except AttributeError:
            pass

    def force_printer_type(self):
        forced_type = self.usb_info.get('forced_type')
        if forced_type:
            self.logger.info("Forcing type: %s" % forced_type)
        self.set_printer_type(forced_type)

    def get_jobs_list(self):
        self.logger.info("Requesting a jobs list")
        if self.server_connection:
            jobs_list = self.server_connection.get_jobs_list(self.printer_token)
            self.logger.info("Cloud's jobs list:\n" + pprint.pformat(jobs_list))
            return jobs_list
        return [], {"message": "No connection to server", "code": 9}

    def start_job_by_id(self, job_id):
        if self.server_connection:
            self.logger.info(f"Sending a request to start a job {job_id}")
            return self.server_connection.start_job_by_id(self.printer_token, job_id)
        else:
            self.logger.info("No connection to server to start a job")

    def start_next_job(self):
        if self.server_connection:
            self.logger.info("Sending a request to starting the next job")
            return self.server_connection.start_next_job(self.printer_token)
        else:
            self.logger.info("No connection to server to start a job")

    def set_printer_type(self, printer_profile_or_alias=None):
        if type(printer_profile_or_alias) == dict:
            alias = printer_profile_or_alias['alias']
        elif type(printer_profile_or_alias) == str or printer_profile_or_alias is None:
            alias = printer_profile_or_alias
        if alias:
            for profile in self.parent.user_login.profiles:
                    if profile['alias'] == alias:
                        self.printer_profile = profile
                        self.save_printer_type(alias)
                        self.logger.info("Printer profile set to: %s", profile)
                        break
        else:
            # try to automatically set profile when no selection required due to single vid pid match
            profiles_with_vid_pid_found = 0
            vid_pid = [self.usb_info['VID'], self.usb_info['PID']]
            for profile in self.parent.user_login.profiles:
                if vid_pid in profile['vids_pids']:
                    profiles_with_vid_pid_found += 1
                    profile_candidate = profile
            if profiles_with_vid_pid_found == 1:
                self.printer_profile = profile_candidate
                self.save_printer_type(profile_candidate['alias'])
                self.logger.info("Autoselecting printer profile: %s", profile_candidate)

    def load_printer_type(self):
        try:
            with open(os.path.join(paths.OFFLINE_PRINTER_TYPE_FOLDER_PATH, self.id_string)) as f:
                printer_type_alias = f.read()
        except FileNotFoundError:
            pass
        except (OSError, IOError) as e:
            self.logger.error("Error reading offline printer type file:" + str(e))
        else:
            if printer_type_alias:
                self.set_printer_type(printer_type_alias)

    def save_printer_type(self, printer_type_alias):
        try:
            if not os.path.isdir(paths.OFFLINE_PRINTER_TYPE_FOLDER_PATH):
                os.mkdir(paths.OFFLINE_PRINTER_TYPE_FOLDER_PATH)
            filepath = os.path.join(paths.OFFLINE_PRINTER_TYPE_FOLDER_PATH, self.id_string)
            if printer_type_alias:
                with open(filepath, "w") as f:
                    f.write(printer_type_alias)
            else:
                try:
                    if os.path.isfile(filepath):
                        os.remove(filepath)
                except:
                    pass
        except (OSError, IOError) as e:
            self.logger.error("Error saving offline printer type:" + str(e))

    def reset_offline_printer_type(self):
        try:
            filename = os.path.join(paths.OFFLINE_PRINTER_TYPE_FOLDER_PATH, self.id_string)
            os.remove(filename)
        except FileNotFoundError:
            pass
        except (OSError, IOError) as e:
            self.logger.error("Error resetting offline printer type:" + str(e))

    def enable_local_mode(self, start_timeout_thread=True):
        with self.local_mode_lock:
            self.local_mode_enable_time = time.monotonic()
            if not self.local_mode:
                self.local_mode = True
                self.logger.info("Local mode enabled")
                if start_timeout_thread and not self.local_mode_timeout_thread:
                    self.local_mode_timeout_thread = threading.Thread(target=self.local_mode_timeout_tracking)
                    self.local_mode_timeout_thread.start()

    def disable_local_mode(self):
        with self.local_mode_lock:
            self.logger.info("Local mode disabled")
            self.local_mode = False

    def local_mode_timeout_tracking(self):
        while not self.stop_flag:
            if not self.local_mode or time.monotonic() > self.local_mode_enable_time + self.local_mode_timeout:
                break
            time.sleep(0.1)
        with self.local_mode_lock:
            if self.local_mode:
                self.logger.info("Local mode disabled")
                self.local_mode = False
                self.local_mode_timeout_thread = None

    def set_cloud_job_id_and_snr(self, server_message):
        if server_message:
            clouds_job_id = server_message.get('job_id')
            clouds_job_snr = server_message.get('job_snr', clouds_job_id)
            try:
                if self.sender and self.sender.SUPPORT_JOBS:
                    self.sender.clouds_job_id = clouds_job_id
                    self.sender.clouds_job_snr = clouds_job_snr
            except AttributeError:
                pass

    def check_request_dumping_flag(self):
        if config.get_settings().get('request_dumping', False):
            self.start_request_dumping()
        else:
            self.finish_request_dumping()

    def start_request_dumping(self):
        self.request_dumping = True
        self.out_counter = 1
        self.in_counter = 1
        if os.path.isfile(paths.REQUEST_DUMPING_DIR):
            self.logger.error('Unable to create a request dumping folder: ' + paths.REQUEST_DUMPING_DIR)
            self.request_dumping = False
            return
        if not os.path.isdir(paths.REQUEST_DUMPING_DIR):
            os.mkdir(paths.REQUEST_DUMPING_DIR)
        folder_name = self.id_string + "_" + time.strftime("%m_%d__%H_%M_%S")
        self.folder_path = os.path.join(paths.REQUEST_DUMPING_DIR, folder_name)
        os.mkdir(self.folder_path)

    def finish_request_dumping(self):
        self.request_dumping = False

    def dump_request(self, data, prefix):
        try:
            jdata = json.dumps(data)
        except:
            self.logger.warning('Unable to dump data:' + str(data))
        else:
            counter_value = getattr(self, prefix + "_counter", "")
            with open(os.path.join(self.folder_path, prefix + str(counter_value) + ".json"), "w") as f:
                f.write(jdata)
            setattr(self, prefix + "_counter", counter_value + 1)

    def force_cancel_state(self):
        self.forced_state = 'cancel'
