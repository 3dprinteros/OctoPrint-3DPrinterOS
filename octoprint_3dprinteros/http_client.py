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

import re
import sys
import time
import json
import uuid
import httplib
from subprocess import Popen, PIPE

import platforms

class HTTPClient:

    HTTPS_MODE = True
    BASE_TIMEOUT = 6
    MAX_TIMEOUT = BASE_TIMEOUT * 5

    streamer_prefix = "/apiprinter/v1/printer"
    user_login_path = streamer_prefix + "/user_login"
    printer_login_path = streamer_prefix + "/printer_login"
    printer_register_path = streamer_prefix + "/register"
    command_path = streamer_prefix + "/command"
    token_send_logs_path = streamer_prefix + "/sendLogs"
    camera_path = streamer_prefix + "/camera" #json['image': base64_image ]

    def __init__(self, parent, keep_connection_flag = False, debug = True, max_send_retry_count = None):
        if hasattr(parent, "parent"):
            self.app = parent.parent
            if hasattr(self.app, "parent"):
                self.app = self.app.parent
        else:
            self.app = parent
        self.url = self.app.url
        self.logger = self.app.get_logger()
        self.parent = parent
        self.parent_usb_info = getattr(parent, 'usb_info', None)
        self.keep_connection_flag = keep_connection_flag
        self.timeout = self.BASE_TIMEOUT
        self.mac = getattr(self.app, "mac", None)
        self.local_ip = getattr(self.app, "local_ip", None)
        self.connection = self.connect()
        self.max_send_retry_count = max_send_retry_count
        self.debug = debug

    def get_mac_add_or_serial_number(self):
        id = self.get_serial_number()
        if not id:
            id = self.get_mac_for_current_ip()
        if not id:
            time.sleep(1)
            id = self.get_mac_for_current_ip()
        if not id:
            self.logger.error("Warning! Can't get mac address! Using uuid.getnode().")
            id = hex(uuid.getnode())
        return id

    # machine id is mac address, but on RPi we use machine serial
    @staticmethod
    def get_serial_number():
        if sys.platform.startswith('linux'):
            call = Popen(['cat', '/proc/cpuinfo'], stdout=PIPE, stderr=PIPE)
            stdout, stderr = call.communicate()
            stdout = stdout.replace('\t', '').split('\n')
            for item in stdout:
                if 'Serial: ' in item:
                    serial = item.replace('Serial: ', '').strip()
                    if serial and serial == "0" * len(serial):
                        return None
                    return serial

    @staticmethod
    def format_mac_addr(mac):
        mac = mac.replace(":", "")
        mac = mac.replace("-", "")
        mac = mac.lower()
        return "0x" + mac + "L"

    def get_mac_for_current_ip(self):
        if platforms.PLATFORM in ("rpi", 'linux', "mac"):
            process = Popen(['ifconfig'], stdout=PIPE)
            stdout, _ = process.communicate()
            for splitter in ("flags=", "Link"):
                if splitter in stdout:
                    interfaces = stdout.split(splitter) #can get name of interface wrong, but we don't need name
                    break
            else:
                return False
            for interface in interfaces:
                if "inet " + self.local_ip in interface:
                    search = re.search("ether\s([0-9a-f\:]+)", interface)
                    if search:
                        return self.format_mac_addr(search.group(1))
                elif "inet addr:" + self.local_ip in interface:
                        search = re.search("HWaddr\s([0-9a-f\:]+)", interface)
                        if search:
                            return self.format_mac_addr(search.group(1))
        else:
            process = Popen('ipconfig /all', stdout=PIPE, shell=True)
            stdout, _ = process.communicate()
            interfaces = stdout.split("\r\n\r\n")
            for interface in interfaces:
                search = re.search("IP.*:\s(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", interface)
                if search:
                    ip = search.group(1)
                    if ip == self.local_ip:
                        search = re.search("[A-F0-9]{2}\-[A-F0-9]{2}\-[A-F0-9]{2}\-[A-F0-9]{2}\-[A-F0-9]{2}\-[A-F0-9]{2}", interface)
                        if search:
                            return self.format_mac_addr(search.group(0))

    def connect(self):
        self.logger.debug("{ Connecting...")
        while not self.parent.stop_flag:
            try:
                if self.HTTPS_MODE:
                    connection = httplib.HTTPSConnection(self.url, port = 443, timeout = self.timeout)
                else:
                    connection = httplib.HTTPConnection(self.url, port = 80, timeout = self.timeout)
                connection.connect()
                self.local_ip = connection.sock.getsockname()[0]
                self.app.local_ip = self.local_ip
                if not self.mac:
                    self.mac = self.get_mac_add_or_serial_number()
                    self.app.mac = self.mac
            except Exception as e:
                self.parent.register_error(5, "Error during HTTP connection: " + str(e))
                self.logger.debug("...failed }")
                self.logger.warning("Warning: connection to %s failed." % self.url)
                if self.timeout < self.MAX_TIMEOUT:
                    self.timeout += self.BASE_TIMEOUT
                time.sleep(1)
            else:
                self.logger.debug("...success }")
                self.logger.info("Connecting from: %s\t%s" % (self.local_ip, self.mac))
                return connection

    def load_json(self, jdata):
        try:
            data = json.loads(jdata)
        except ValueError:
            self.parent.register_error(2, "Received data is not valid json: " + jdata)
        else:
            if type(data) == list:
                return {}
            return data

    def request(self, method, connection, path, payload, headers=None):
        self.logger.debug("{ Requesting...")
        if headers is None:
            headers = {"Content-Type": "application/json", "Content-Length": str(len(payload))}
            if self.keep_connection_flag:
                headers["Connection"] = "keep-alive"
        try:
            if self.debug:
                self.logger.debug("%s, %s %s %s" % (method, path, payload, headers))
            else:
                self.logger.debug("%s, %s %s" % (method, path, headers))
            connection.request(method, path, payload, headers)
            resp = connection.getresponse()
        except Exception as e:
            self.logger.info("Error during HTTP request:" + str(e))
            self.parent.register_error(6, "Error during HTTP request:" + str(e))
            time.sleep(1)
        else:
            #self.logger.debug("Request status: %s %s" % (resp.status, resp.reason))
            try:
                received = resp.read()
            except Exception as e:
                self.parent.register_error(7, "Error reading response: " + str(e))
            else:
                if resp.status == httplib.OK and resp.reason == "OK":
                    self.logger.debug("...success }")
                    return received
                else:
                    message = "Error: server responded with non 200 OK\nFull response: %s" % received
                    self.parent.register_error(8, message)
        self.logger.debug("...failed }")
        self.logger.warning("Warning: HTTP request failed!")

    def pack_and_send(self, target, *payloads, **kwargs_payloads):
        path, packed_message = self.pack(target, *payloads, **kwargs_payloads)
        return self.send(path, packed_message)

    def send(self, path, data):
        try_count = 0
        while not self.parent.stop_flag:
            if not self.connection:
                self.connection = self.connect()
            answer = self.request("POST", self.connection, path, data)
            if not self.keep_connection_flag or not answer:
                self.connection.close()
                self.connection = None
            if answer:
                return self.load_json(answer)
            try_count += 1
            if isinstance(self.max_send_retry_count, int) and self.max_send_retry_count < try_count:
                return None
            time.sleep(1)

    def pack(self, target, *args, **kwargs):
        if target == 'user_login':
            data = { 'login': {'user': args[0], 'password': args[1]},
                     'platform': platforms.PLATFORM, 'host_mac': self.mac,
                     'local_ip': self.local_ip, 'version': self.app.get_plugin_version()}
            if 'disposable_token' in kwargs:
                data['login']['disposable_token'] = kwargs['disposable_token']
            path = self.user_login_path
            #self.logger.debug(data)
        elif target == 'printer_login':
            data = { 'user_token': args[0], 'printer': args[1], "version": self.app.get_plugin_version(),
                     "data_time": time.ctime(), "camera": None }
                    # "data_time": time.ctime(), "camera": config.get_app().camera_controller.get_current_camera_name()}
            path = self.printer_login_path
        elif target == 'command':
            data = { 'auth_token': args[0], 'report': args[1], 'command_ack': args[2] }
            if not data['command_ack']:
                data.pop('command_ack')
            path = self.command_path
        elif target == 'camera':
            data = { 'auth_token': args[0], 'camera_number': args[1], 'image': args[2]}
            path = self.camera_path
        else:
            self.parent.register_error(4, 'No such target for packaging: ' + target)
            data, path = None, None
        for key, value in kwargs.items():
            data[key] = value
        return path, json.dumps(data)

    def close(self):
        if self.connection:
            self.connection.close()
