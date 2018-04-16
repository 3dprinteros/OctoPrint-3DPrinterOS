#
# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019. Built in San Francisco.
#
# This software is distributed under commercial non-GPL license for personal, educational,
# corporate or any other use. The software as a whole or any parts of that are prohibited
# for distribution and/or use without obtaining license from 3D Control Systems, Inc.
#
# If you do not have the license to use this software, please delete all software files
# immediately and contact sales to obtain the license: sales@3dprinteros.com.
# If you are unsure about the licensing please contact directly our sales: sales@3dprinteros.com.

import time
import json
import logging
import threading

# import log
import downloader
import http_client
import octoprint_sender
from octo_cam import OctoCamera

class PrinterInterface(threading.Thread):

    DEFAULT_OPERATIONAL_TIMEOUT = 10
    COMMAND_REQUEST_PERIOD = 1.5

    def __init__(self, parent, token):
        self.logger = parent.get_logger()
        self.creation_time = time.time()
        self.stop_flag = False
        self.printer_token = token
        self.stop_in_next_loop_flag = False
        self.parent = parent
        self.printer = None
        self.printer_profile = {'sender': 'octoprint_sender', 'name': 'OctoPrinter'}
        self.usb_info = None
        self.downloader = None
        self.forced_state = "connecting"
        self.printer_name = ""
        self.groups = []
        self.errors = []
        self.last_error = None
        self.local_mode = False
        self.requests_to_server = {}
        self.requests_lock = threading.Lock()
        self.show_printer_type_selector = False
        self.timeout = self.DEFAULT_OPERATIONAL_TIMEOUT
        self.first_request = True
        self.printer = self.connect_to_printer()
        self.http_client = None
        self.camera = None
        self.camera_thread = None
        self.current_camera_enabled = None
        # self.logger.info('New printer interface for %s' % str(usb_info))
        super(PrinterInterface, self).__init__(name="PrinterInterface")

    def connect_to_printer(self):
        self.logger.info("Connecting with profile: " + str(self.printer_profile))
        try:
            printer = octoprint_sender.Sender(self, self.usb_info, self.printer_profile)
        except RuntimeError as e:
            message = "Can't connect to printer %s %s\nReason: %s" %\
                      (self.printer_profile['name'], str(self.usb_info), e.message)
            self.register_error(119, message, is_blocking=True)
        else:
            self.logger.info("Successful connection to %s!" % (self.printer_profile['name']))
            return printer

    def form_command_request(self, acknowledge):
        message = [self.printer_token, self.state_report(), acknowledge]
        kw_message = {}
        if self.errors:
            kw_message['error'] = self.errors[:]
            self.errors = []
        self.check_camera_name()
        if self.first_request:
            self.first_request = False
            kw_message['reset_job'] = 1
        with self.requests_lock:
            kw_message.update(self.requests_to_server)
            self.requests_to_server = {}
        requests_to_stop_after = ['reset_printer_type']
        for stop_after in requests_to_stop_after:
            if stop_after in kw_message:
                self.stop_in_next_loop_flag = True
                break
        self.logger.debug("Request:\n%s\n%s" % (str(message), str(kw_message)))
        return message, kw_message

    def check_operational_status(self):
        if not self.printer or self.stop_flag:
            pass
        elif self.printer.stop_flag:
            pass
        elif self.printer.is_operational():
            self.last_operational_time = time.time()
            self.forced_state = None
            self.last_errors = []
        elif not self.forced_state:
            message = "Printer is not operational"
            self.register_error(77, message, is_blocking=False)
        elif self.forced_state != "error" and self.last_operational_time + self.timeout < time.time():
            message = "Not operational timeout reached"
            self.register_error(78, message, is_blocking=True)
        for error in self.errors:
            if error['is_blocking']:
                self.forced_state = "error"
                self.close_printer_sender()
                self.stop_in_next_loop_flag = True
            elif not self.forced_state and not error.get('is_info'):
                self.forced_state = "connecting"

    # @log.log_exception
    def run(self):
        self.last_operational_time = time.time()
        acknowledge = None
        can_restart = True
        stop_before_new_loop = False
        while not self.parent.stop_flag and (not self.stop_flag or self.errors):
            if self != self.parent.pi or self.printer_token != self.parent.token:
                can_restart = False
                self.stop_flag = True
                if self.errors:
                    stop_before_new_loop = True
                else:
                    break
            if not self.http_client:
                self.http_client = http_client.HTTPClient(self, keep_connection_flag=True)
            if self.parent.camera_enabled:
                self.start_camera()
            else:
                self.stop_camera()
            self.check_operational_status()
            message, kw_message = self.form_command_request(acknowledge)
            answer = self.http_client.pack_and_send('command', *message, **kw_message)
            if answer is not None:
                self.logger.debug("Answer: " + str(answer))
                acknowledge = self.execute_server_command(answer)
            if stop_before_new_loop:
                break
            if not self.stop_flag and not self.parent.stop_flag:
                if self.stop_in_next_loop_flag:
                    self.stop_flag = True
                else:
                    time.sleep(self.COMMAND_REQUEST_PERIOD)
        if self.http_client:
            self.http_client.close()
        self.close_printer_sender()
        self.logger.info('Printer interface was stopped')
        if can_restart and not self.parent.stop_flag:
            self.logger.info('Trying to restart Printer interface')
            self.parent.restart_printer_interface()

    def start_camera(self):
        if not self.camera:
            self.camera = OctoCamera(self)
        if self.camera_thread:
            if self.camera_thread.isAlive():
                return
            del self.camera_thread
        self.camera.stop_flag = False
        self.camera_thread = threading.Thread(target=self.camera.main_loop)
        self.camera_thread.start()

    def stop_camera(self):
        if self.camera_thread and self.camera_thread.isAlive():
            self.camera.stop_flag = True

    def validate_command(self, server_message):
        command = server_message.get('command')
        if command:
            number = server_message.get('number')
            false_ack = {"number": number, "result": False}
            if self.local_mode:
                self.register_error(111, "Can't execute command %s while in local_mode!" % command, is_blocking = False)
                return false_ack
            if type(number) != int:
                message = "Error in number field of server's message: " + str(server_message)
                self.register_error(41, message,  is_blocking=False)
                return false_ack
            if not hasattr(self, command) and not hasattr(self.printer, command):
                self.register_error(40, "Unknown command:'%s' " % str(command), is_blocking=False)
                return false_ack

    def execute_server_command(self, server_message):
        validation_error = self.validate_command(server_message)
        if validation_error:
            if validation_error['number'] is not None:
                return validation_error
        error = server_message.get('error')
        if error:
            self.logger.warning("@ Server return error: %s\t%s" % (error.get('code'), error.get('message')))
        command, number = server_message.get('command'), server_message.get('number')
        if command:
            if self.errors:
                self.logger.error("! Server returns command on request containing an error - this is error.")
                return { "number": number, "result": False }
            self.logger.info("Executing command number %i : %s" % (number, str(command)))
            method = getattr(self, command, None)
            if not method:
                method = getattr(self.printer, command)
            payload = server_message.get('payload')
            if server_message.get('is_link'):
                if self.downloader and self.downloader.is_alive():
                    self.register_error(108, "Can't start new download, because previous download isn't finished.")
                    result = False
                else:
                    if command == 'gcodes':
                        self.printer.set_filename(server_message.get('filename'))
                    self.downloader = downloader.Downloader(self, payload, method, is_zip=bool(server_message.get('zip')))
                    self.downloader.start()
                    result = True
            else:
                if payload:
                    arguments = [payload]
                else:
                    arguments = []
                try:
                    result = method(*arguments)
                    # to reduce needless 'return True' in methods, we assume that return of None, that is a successful call
                    result = result or result == None
                except Exception as e:
                    message = "! Error while executing command %s, number %d.\t%s" % (command, number, e.message)
                    self.register_error(109, message, is_blocking=False)
                    self.logger.exception(message)
                    result = False
            ack = { "number": number, "result": result }
            return ack

    def get_printer_state(self):
        if self.forced_state:
            state = self.forced_state
        elif (self.printer and self.printer.stop_flag) or not self.printer or self.stop_flag:
            state = 'closing'
        elif self.printer.is_paused():
            state = "paused"
        elif self.downloader and self.downloader.is_alive():
            state = "downloading"
        elif self.printer.is_printing():
            state = "printing"
        elif self.local_mode:
            state = 'local_mode'
        else:
            state = "ready"
        return state

    def state_report(self):
        report = {"state": self.get_printer_state()}
        self.logger.debug('get_current_data %s' % str(self.get_octo_printer().get_current_data()))
        self.logger.debug('get_current_job %s' % str(self.get_octo_printer().get_current_job()))
        self.logger.debug('get_current_temperatures %s' % str(self.get_octo_printer().get_current_temperatures()))
        self.logger.debug('get_state_id %s' % str(self.get_octo_printer().get_state_id()))
        if self.printer:
            try:
                if self.is_downloading():
                    report["percent"] = self.printer.get_downloading_percent()
                else:
                    report["percent"] = self.printer.get_percent()
                self.printer.update_temps()
                report["temps"] = self.printer.get_temps()
                report["target_temps"] = self.printer.get_target_temps()
                report["line_number"] = self.printer.get_current_line_number()
                # report["coords"] = self.printer.get_position()
                if self.printer.responses:
                    report["response"] = self.printer.responses[:]
                    self.printer.responses = []
            except Exception as e:
                report["state"] = self.get_printer_state() #update printer state if in was disconnected during report
                self.logger.warning("! Exception while forming printer report: " + str(e))
        return report

    def register_error(self, code, message, is_blocking=False,
                       send_if_same_as_last=True, is_info=False):
        error = {"code": code, "message": message, "is_blocking": is_blocking}
        if is_info:
            error['is_info'] = True
        if send_if_same_as_last or is_blocking or self.last_error != error:
            self.errors.append(error)
        self.last_error = error
        self.logger.warning("Error N%d. %s" % (code, message))

    def check_camera_name(self):
        if self.current_camera_enabled != self.parent.camera_enabled:
            self.logger.info("Camera change detected")
            camera_name = "Dual camera" if self.parent.camera_enabled else "Disable camera"
            with self.requests_lock:
                self.requests_to_server['camera_change'] = camera_name
            self.current_camera_enabled = self.parent.camera_enabled

    def set_printer_name(self, name):
        self.printer_name = name

    # def upload_logs(self):
    #     self.logger.info("Sending logs")
    #     if log.report_problem('logs'):
    #         return False

    def switch_camera(self, module):
        self.parent.set_camera(module != 'Disable camera')
        return True
    #
    # def restart_camera(self):
    #     self.logger.info('Executing camera restart command from server')
    #     self.parent.camera_controller.restart_camera()

    def set_name(self, name):
        self.logger.info("Setting printer name: " + str(name))
        self.set_printer_name(name)

    def is_downloading(self):
        return self.downloader and self.downloader.is_alive()

    def cancel(self):
        if self.is_downloading():
            self.logger.info("Canceling downloading")
            self.downloader.cancel()
        else:
            return self.printer.cancel()

    def close_printer_sender(self):
        if self.printer and not self.printer.stop_flag:
            self.logger.info('Closing ' + str(self.printer_profile))
            self.printer.close()
            self.logger.info('...closed.')

    def close(self):
        # self.logger.info('Closing printer interface: ' + str(self.usb_info))
        self.stop_flag = True

    def get_octo_printer(self):
        return self.parent.get_printer()

    def get_octo_file_manager(self):
        return self.parent.get_file_manager()

    # def report_problem(self, problem_description):
    #     log.report_problem(problem_description)
