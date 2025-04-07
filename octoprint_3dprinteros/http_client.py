# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019.
# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)


import re
import sys
import time
import json
import uuid
import http.client
import logging
import subprocess
import threading
import ifaddr

import config
import version
import platforms
import client_ssl_context
try:
    from branch_stuff import BRANCH_TOKEN
except:
    BRANCH_TOKEN = None


def get_printerinterface_protocol_connection():
    protocol = config.get_settings()["protocol"]
    user_login = protocol["user_login"]
    if user_login:
        connection = HTTPClient
    else:
        connection = HTTPClientPrinterAPIV1
    return connection


class HTTPClient:

    URL = config.get_settings()['URL']
    HTTPS_MODE = config.get_settings()['protocol']['encryption']
    CUSTOM_PORT = config.get_settings()['protocol'].get('custom_port', 0)
    RESP_TIME_LOGGING = config.get_settings()['protocol'].get('response_time_log', False)
    BASE_TIMEOUT = 6
    MAX_TIMEOUT = BASE_TIMEOUT * 3
    RECONNECTION_ATTEMPT_DELAY = BASE_TIMEOUT / 2
    GET_MAC_MAX_RETRIES = 3
    RECONNECT_AFTER_N_ERRORS = 5
    COMMAND_REQ_SIZE_WARNING_THRS = 2048
    CAMERA_REQ_SIZE_WARNING_THRS = 200*4096
    MAX_RESP_LEN = config.get_settings()['logging'].get('max_record_len', 512)
    API_PREFIX = '/streamerapi/'
    USER_LOGIN =  'user_login'
    PRINTER_LOGIN = 'printer_login'
    COMMAND = 'command'
    TOKEN_SEND_LOGS = 'sendlogs'
    CAMERA = 'camera' #json['image': base64_image ]
    CAMERA_IMAGEJPEG = 'camera_image_jpeg' # body is pure binary jpeg data, but header got id information
    GET_JOBS = 'get_queued_jobs'
    START_JOB = 'start_queued_job'
    DEFAULT_HEADERS = {"Content-Type": "application/json"}
    EMPTY_COMMAND = {"command" : None}
    SEND_LOGS_TOKEN_FIELD_NAME = 'user_token'
    IS_LINK_BYTES = b"is_link"

    def __init__(self, parent, keep_connection_flag = True, logging_level = logging.INFO, exit_on_fail=False):
        self.parent = parent
        self.parent_usb_info = getattr(parent, 'usb_info', None)
        if parent and hasattr(parent, 'logger'):
            self.logger = parent.logger.getChild(self.__class__.__name__)
        else:
            self.logger = logging.getLogger(self.__class__.__name__ + "." + str(self.parent_usb_info))
        self.logger.setLevel(logging_level)
        self.connection_lock = threading.Lock()
        self.keep_connection_flag = keep_connection_flag
        self.exit_on_fail = exit_on_fail
        self.timeout = self.BASE_TIMEOUT
        self.errors_until_reconnect = self.RECONNECT_AFTER_N_ERRORS
        self.hide_sensitive_log = config.get_settings().get('hide_sensitive_log', False)
        if hasattr(parent, 'app'): #TODO refactor mess with non universal mac and local_ip
            app = parent.app
        else:
            app = parent
        self.local_ip = None
        self.host_id = getattr(app, 'host_id', "")
        self.macaddr = getattr(app, 'macaddr', "")
        self.lock = threading.RLock()
        if self.CUSTOM_PORT:
            self.port = self.CUSTOM_PORT
        elif self.HTTPS_MODE:
            self.port = 443
        else:
            self.port = 80
        self.connection = self.connect()

    def get_host_id(self):
        host_id = self.get_serial_number()
        if not host_id:
            host_id = self.get_macaddr(self.local_ip)
            self.macaddr = host_id
        if not host_id:
            self.logger.warning("Warning! Can't get MAC address! Using uuid.getnode()")
            host_id = hex(uuid.getnode()) + "L"
            self.macaddr = host_id
        return host_id

    # machine id is mac address, but on RPi we use machine serial
    @staticmethod
    def get_serial_number():
        if sys.platform.startswith('linux'):
            try:
                with open('/proc/cpuinfo') as f:
                    for line in f:
                        words = line.split()
                        if words and words[0] == 'Serial':
                            serial = words[-1]
                            if serial and serial == '0' * len(serial):
                                return None
                            return serial
            except (OSError, IndexError):
                logging.getLogger('HTTPClient').error('Error on parsing cpuinfo')

    @staticmethod
    def format_mac_addr(macaddr, old_macid_compat=True):
        macddr = macaddr.replace(':', '').replace('-', '').lower()
        if old_macid_compat:
            macddr = '0x' + macddr + 'L'
        return macddr

    @staticmethod
    def get_macaddr(local_ip, retry=0, old_macid_compat=True):
        if local_ip:
            try:
                if platforms.PLATFORM in ("rpi", "linux"):
                    for adapter in ifaddr.get_adapters():
                        for ip in adapter.ips:
                            if ip.ip == local_ip:
                                with open('/sys/class/net/' + ip.nice_name + '/address') as f:
                                    return HTTPClient.format_mac_addr(f.read().strip(), old_macid_compat=old_macid_compat)
                elif platforms.PLATFORM == "win":
                    stdout = subprocess.run(['ipconfig', '/all'], stdout=subprocess.PIPE, universal_newlines=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout
                    interfaces = stdout.split("\n\n")
                    for interface in interfaces:
                        search = re.search(r'IP.*:\s(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', interface)
                        if search:
                            ip = search.group(1)
                            if ip == local_ip:
                                search = re.search(r'[A-F0-9]{2}\-[A-F0-9]{2}\-[A-F0-9]{2}\-[A-F0-9]{2}\-[A-F0-9]{2}\-[A-F0-9]{2}', interface)
                                if search:
                                    return HTTPClient.format_mac_addr(search.group(0), old_macid_compat)
                else:
                    stdout = subprocess.run(['ifconfig'], stdout=subprocess.PIPE, universal_newlines=True).stdout
                    for splitter in ("flags=", "Link"):
                        if splitter in stdout:
                            interfaces = stdout.split(splitter) #can get name of interface wrong, but we don't need name
                            break
                    else:
                        return
                    for interface in interfaces:
                        if 'inet ' + local_ip in interface:
                            search = re.search(r'ether\s([0-9a-f\:]+)', interface)
                            if search:
                                return HTTPClient.format_mac_addr(search.group(1), old_macid_compat=old_macid_compat)
                        elif 'inet addr:' + local_ip in interface:
                            search = re.search(r'HWaddr\s([0-9a-f\:]+)', interface)
                            if search:
                                return HTTPClient.format_mac_addr(search.group(1), old_macid_compat=old_macid_compat)
            except (OSError, IndexError, subprocess.SubprocessError, UnicodeDecodeError):
                logging.getLogger('HTTPClient').exception('Error on getting macaddr:')
            if retry < HTTPClient.GET_MAC_MAX_RETRIES:
                time.sleep(0.1)
                retry += 1
                return HTTPClient.get_macaddr(local_ip, retry, old_macid_compat)

    def connect(self):
        #self.logger.debug('{ Connecting...')
        while not getattr(self.parent, "stop_flag", False) and not getattr(self.parent, "offline_mode", False):
            if self.HTTPS_MODE:
                connection_class = http.client.HTTPSConnection
                kwargs = {'context': client_ssl_context.SSL_CONTEXT}
            else:
                connection_class = http.client.HTTPConnection
                kwargs = {}
            with self.connection_lock:
                try:
                    connection = connection_class(self.URL, port = self.port, timeout = self.timeout, **kwargs)
                    connection.connect()
                    self.local_ip = connection.sock.getsockname()[0]
                    if not self.host_id:
                        self.host_id = self.get_host_id()
                    if not self.macaddr:
                        self.macaddr = self.get_macaddr(self.local_ip, old_macid_compat = False)
                except Exception as e:
                    self.parent.register_error(5, 'Error during HTTP connection: ' + str(e))
                    #self.logger.debug('...failed }')
                    self.logger.warning('Warning: connection to %s failed.' % self.URL)
                    if self.exit_on_fail:
                        return
                    if self.timeout < self.MAX_TIMEOUT:
                        self.timeout += self.BASE_TIMEOUT
                    time.sleep(1)
                else:
                    #self.logger.debug('...success }')
                    if not self.exit_on_fail:
                        self.logger.info('Connected to server from: %s %s' % (self.local_ip, self.host_id))
                    return connection

    def request(self, method, connection, path, payload, headers=None):
        #self.logger.debug('{ Requesting...')
        if headers is None:
            headers = self.DEFAULT_HEADERS
            headers = {"Content-Type": "application/json"}
        headers["Content-Length"] = len(payload)
        if self.keep_connection_flag:
            headers['Connection'] = 'keep-alive'
        try:
            connection.request(method, path, payload, headers)
            resp = connection.getresponse()
        except Exception as e:
            if self.parent:
                self.parent.register_error(6, 'Error during HTTP request:' + str(e), is_info=True)
            time.sleep(1)
        else:
            #self.logger.debug('Response status: %s %s' % (resp.status, resp.reason))
            try:
                received = resp.read()
            except Exception as e:
                if self.parent:
                    self.parent.register_error(7, 'Error reading response: ' + str(e), is_info=True)
            else:
                if resp.status == http.client.OK and resp.reason == "OK":
                    #self.logger.debug("...success }")
                    self.errors_until_reconnect = self.RECONNECT_AFTER_N_ERRORS
                    return received
                message = 'Error: server responded with non 200 OK:\t%s %s %s' %\
                        (resp.status, resp.reason, received)
                if self.parent:
                    self.parent.register_error(8, message, is_info=True)
        #self.logger.debug('...failed }')
        self.logger.warning('Warning: HTTP request failed!')

    def pack_and_send(self, target, *payloads, **kwargs_payloads):
        with self.lock:
            path, packed_message = self.pack(target, *payloads, **kwargs_payloads)
            if target == self.CAMERA or target == self.CAMERA_IMAGEJPEG:
                self.logger.info(f"REQ({target}):\nCamera frame: {len(packed_message)}B")
            else:
                if BRANCH_TOKEN:
                    self.logger.info(f"REQ({target}):\n{packed_message.replace(BRANCH_TOKEN, '__hidden__')}")
                else:
                    self.logger.info(f"REQ({target}):\n{packed_message}")
            return self.send(path, packed_message)

    def send(self, path, data, headers = None):
        while not getattr(self.parent, "stop_flag", False) and not getattr(self.parent, "offline_mode", False):
            if not self.errors_until_reconnect:
                self.errors_until_reconnect = self.RECONNECT_AFTER_N_ERRORS
                self.close()
            if not self.connection:
                self.connection = self.connect()
            if self.connection:
                if self.RESP_TIME_LOGGING:
                    start_time = time.monotonic()
                answer = self.request('POST', self.connection, path, data, headers)
                if self.RESP_TIME_LOGGING:
                    delta = time.monotonic() - start_time
                    self.logger.info(f'Request time: {delta:2f}')
            elif self.exit_on_fail:
                return
            else:
                answer = None
            if answer == None or not self.keep_connection_flag:
                self.close()
                time.sleep(self.RECONNECTION_ATTEMPT_DELAY) # Some delay before retry reconnection
            if answer:
                return self.unpack(answer, path)

    def pack(self, target, *args, **kwargs):
        if target == self.USER_LOGIN:
            message = { 'login': {'user': args[0], 'password': args[1]},
                     'platform': platforms.PLATFORM, 'host_mac': self.host_id,
                     'local_ip': self.local_ip, 'version': version.version + version.branch}
            if BRANCH_TOKEN:
                message['branch_token'] = BRANCH_TOKEN
            if 'disposable_token' in kwargs:
                message['login']['disposable_token'] = kwargs['disposable_token']
                del kwargs['disposable_token']
        elif target == self.PRINTER_LOGIN:
            message = { 'user_token': args[0], 'printer': args[1],
                        'version': version.version + version.branch,
                        'message_time': time.ctime(),
                        'camera': config.get_app().camera_controller.get_current_camera_name(),
                        'verbose': config.get_settings()['verbose'] }
        elif target == self.COMMAND:
            message = { 'printer_token': args[0], 'report': args[1], 'command_ack': args[2] }
            if not message['command_ack']:
                message.pop('command_ack')
        elif target == self.CAMERA:
            message = { 'user_token': args[0], 'camera_number': args[1], 'camera_name': args[2],
                     'file_data': args[3], 'host_mac': self.host_id }
        elif target == self.CAMERA_IMAGEJPEG:
            message = { 'user_token': args[0],
                        'camera_number': args[1],
                        'camera_name': args[2],
                        'host_mac': self.host_id }
        elif target == self.GET_JOBS:
            message = { "printer_token": args[0] }
        elif target == self.START_JOB:
            message = { "printer_token": args[0], "job_id": args[1] }
            if kwargs.get('automatic'):
                message = { "printer_token": args[0], "job_id": args[1], 'automatic': True }
        else:
            if self.parent:
                self.parent.register_error(4, 'No such target for packaging: ' + target)
            message, target = None, None
        for key, value in list(kwargs.items()):
            message[key] = value
        message.update(kwargs)
        #self.logger.info(f"Message: {target} {message}")
        return self.API_PREFIX + target, json.dumps(message)

    def unpack(self, json_text, path):
        if not json_text:
            self.logger.info("RESP(%s):\n%s", path, json_text)
        elif len(json_text) > self.MAX_RESP_LEN:
            self.logger.info("RESP(%s):\n%s", path, json_text[:self.MAX_RESP_LEN] + b"...")
        elif self.hide_sensitive_log and self.IS_LINK_BYTES in json_text:
            self.logger.info("RESP(%s):\n%s", path, '__hidden__')
        else:
            self.logger.info("RESP(%s):\n%s", path, json_text)
        try:
            if json_text:
                data = json.loads(json_text)
            else:
                data = self.EMPTY_COMMAND
        except (ValueError, TypeError):
            if self.parent:
                self.parent.register_error(2, f'Response on {path} is not valid json: {json_text}')
        else:
            if data == []: # this is needed to support '[]' answer that exist in the protocol due to php used in cloud servers
                data = self.EMPTY_COMMAND
            # NOTE == list is used only for printer profiles
            if type(data) == dict or type(data) == list:
                return data
            else:
                message = f'Response on {path} is not dictionary or list. {type(data)} {data}'
                if self.parent:
                    self.parent.register_error(3, message)

    def get_parent_name(self):
        parent_name = "None"
        parent = getattr(self, "parent")
        if parent:
            parent_name = str(parent.__class__.__name__)
        return parent_name

    def get_jobs_list(self, token):
        jobs_list_or_error = self.pack_and_send(self.GET_JOBS, token)
        self.logger.debug(f"Jobs list: {jobs_list_or_error}")
        if not jobs_list_or_error:
            self.logger.debug("Empty jobs queue")
            return [], None
        if isinstance(jobs_list_or_error, dict):
            self.logger.debug(f"Jobs: {jobs_list_or_error}")
            error = jobs_list_or_error.get('error')
            code = jobs_list_or_error.get('code')
            if error:
                return [], jobs_list_or_error
            else:
                self.logger.warning("Empty jobs queue")
                return [], None
        elif isinstance(jobs_list_or_error, list):
            return jobs_list_or_error, None
        self.logger.warning('Unexpected response on get queued jobs list request!\n' + str(jobs_list_or_error))
        return [], None

    def start_job_by_id(self, token, job_id):
        response = self.pack_and_send(self.START_JOB, token, job_id)
        self.logger.debug(f"Start job result: {response}")
        if response == None:
            return False
        elif response == "" or response == []:
            return True
        elif response:
            if isinstance(response, dict):
                if 'error' in response:
                    self.logger.warning('Error stating job: ' + str(response))
                    return False
                else:
                    return True
        self.logger.warning('Unexpected response on start job request!')
        return False

    def start_next_job(self, token, automatic=False):
        jobs_list_or_error = self.pack_and_send(self.GET_JOBS, token)
        if jobs_list_or_error:
            self.logger.info(f"Jobs list: {jobs_list_or_error}\n")
            if isinstance(jobs_list_or_error, dict):
                error = jobs_list_or_error.get('error')
                code = jobs_list_or_error.get('code')
                if not error:
                    self.logger.warning("Empty jobs queue")
                    return False
            elif isinstance(jobs_list_or_error, list):
                first_job_dict = jobs_list_or_error[0]
                first_job_id = first_job_dict.get("id")
                if first_job_id:
                    if self.pack_and_send(self.START_JOB, token, first_job_id, automatic=automatic) != None:
                        return True
                else:
                    self.logger.warning("No job id to send start job requests to the cloud")
        else:
            self.logger.warning("Empty jobs queue")
        return False

    def close(self):
        if not self.exit_on_fail:
            self.logger.info("Closing connection to server")
        with self.connection_lock:
            if self.connection:
                self.connection.close()
                self.connection = None


class HTTPClientPrinterAPIV1(HTTPClient):

    API_PREFIX = '/apiprinter/v1/printer/'
    REGISTER = 'register'
    PRINTER_PROFILES = 'get_printer_profiles'
    SEND_LOGS_TOKEN_FIELD_NAME = 'auth_token'

    @staticmethod
    def patch_api_prefix(url):
        vendor = str(config.get_settings().get('vendor', {}).get('name', '')).lower()
        if vendor:
            return url.replace("/v1", "/v1/" + vendor)
        else:
            return url

    def __init__(self, parent, keep_connection_flag = True, logging_level = logging.INFO, exit_on_fail = False):
        super().__init__(parent, keep_connection_flag, logging_level, exit_on_fail)
        self.API_PREFIX = self.patch_api_prefix(self.API_PREFIX)
        self.logger.info("Switching URL path to " + self.API_PREFIX)

    def pack(self, target, *args, **kwargs):
        if target == self.PRINTER_PROFILES:
            message = {}
        elif target == self.REGISTER:
            message = { 'mac': self.host_id, 'version': version.version + version.branch, 'verbose': config.get_settings()['verbose'] }
            for key in ('VID', 'PID', 'SNR', 'type'):
                message[key] = kwargs[key]
            for key in ('registration_code', 'registration_code_ttl'):
                if key in kwargs:
                    message[key] = kwargs[key]
        elif target == self.COMMAND:
            message = { 'auth_token': args[0], 'report': args[1], 'command_ack': args[2] }
            if not message['command_ack']:
                message.pop('command_ack')
        elif target == self.CAMERA:
            message = { 'auth_token': args[0], 'image': args[3] }
        elif target == self.CAMERA_IMAGEJPEG:
           message = "Binary jpeg camera path is not supported by APIprinter(user_login : false)"
           self.logger.error(message)
           raise RuntimeError(message)
        elif target == self.GET_JOBS:
            message = { "auth_token": args[0] }
        elif target == self.START_JOB:
            message = { "auth_token": args[0], "job_id": args[1] }
        else:
           self.logger.error(f"Invalid http_client pack call:{target}, {args}, {kwargs}")
           return self.COMMAND, {}
        message.update(kwargs)
        #self.logger.info(f"Message: {target} {message}")
        return self.API_PREFIX + target, json.dumps(message)


# class ProtobufPrinterHTTPClient(HTTPClient, protobuf_protocol.ProtobufProtocol):

#     DEFAULT_HEADERS = {"Content-Type": "application/octet-stream"}
#     API_PREFIX = "/protobuf/v1/"

#     def pack(self, target, *args, **kwargs):
#         if target == self.PRINTER_LOGIN:
#             message = self.pack_printer_login(*args, **kwargs)
#         elif target == self.COMMAND:
#             message = self.pack_command_request(*args, **kwargs)
#         for kwarg in kwargs:
#             setattr(message, kwarg, kwargs[kwarg])
#         return self.API_PREFIX + target, message

#     def unpack(self, message, path=None):
#         if path:
#             answer_target = path.replace(self.API_PREFIX, "")
#         if answer_target == self.COMMAND:
#             return self.unpack_command(message)
#         elif answer_target == self.PRINTER_LOGIN:
#             return self.unpack_printer_login(message)
