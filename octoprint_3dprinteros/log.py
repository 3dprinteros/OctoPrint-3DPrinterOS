#!/usr/bin/env python
# -*- coding: utf-8 -*-
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
import sys
import time
import errno
import zipfile
import logging
import logging.handlers
import traceback
import platform
import os.path
import json
import functools

import paths
from paths import CURRENT_SETTINGS_FOLDER

try:
    import requests
except ImportError: # to prevent a crash due to requests importing pycache with import simplejson in it
    print("Exception on import of module requests:")
    print(sys.exc_info())
    print("Trying to fix by removing cache files and empty folders")
    paths.cleanup_caches()
    sys.exit(1) #dont try to reimport requests

import http_client
import config
import version
import subprocess


MAIN_LOG_NAME = os.path.join(CURRENT_SETTINGS_FOLDER, "3dprinteros_client.log")
CAMERA_LOG_FILE = os.path.join(CURRENT_SETTINGS_FOLDER, "3dprinteros_camera.log")
EXCEPTIONS_LOG_FILE = os.path.join(CURRENT_SETTINGS_FOLDER, 'critical_errors.log')
REPORT_FILE_NAME = 'problem_report.txt'
DETECTION_REPORT_NAME = 'integration_request.txt'
ATTACHMENTS_FOLDER_NAME = 'attachments'
REPORT_FILE = os.path.join(CURRENT_SETTINGS_FOLDER, REPORT_FILE_NAME)
DETECTION_REPORT_FILE = os.path.join(CURRENT_SETTINGS_FOLDER, DETECTION_REPORT_NAME)
PRINTERS_SUBFOLDER = 'printer_logs'
FORBIDDEN_LOG_NAME_CHARS = (".", "/", "\\", ">", "<")

LOG_BACKUPS = config.get_settings()['logging'].get('backup_rolls', 1) #total max long 0 + 1 = 50MB + 50MB = 100MB
LOG_FILE_SIZE = 1024 * 1024 * config.get_settings()['logging'].get('size_mb', 100) / (LOG_BACKUPS + 1) # 100MB by default)
#REQUEST_SKIP = config.get_settings()['logging']['request_skip']
REQUEST_SKIP = 0

TAIL_LINES = 100
AVERAGE_LINE_LENGTH = 200


class SkipRequestsFilter(logging.Filter):

    REQUEST_RECORD_START = "Request:"

    def __init__(self):
        self.counter = 0

    def filter(self, record):
        if record.getMessage().startswith(self.REQUEST_RECORD_START):
            if self.counter < REQUEST_SKIP:
                self.counter += 1
                return False
            else:
                self.counter = 0
        return True


def create_logger(logger_name, log_file_name=None, subfolder=None):
    logger = logging.getLogger(logger_name)
    if logger.propagate: #prevent reattach copies of handlers to the existing logger
        logger.propagate = False
        if config.get_settings()['logging'].get('debug', False):
            level = logging.DEBUG
        else:
            level = logging.INFO
        logger.setLevel(level)
        std_handler = logging.StreamHandler(stream=sys.stdout)
        std_handler.setLevel(level)
        logger.addHandler(std_handler)
        if not logger_name:
            log_file_name=MAIN_LOG_NAME
        elif not log_file_name and not 'Camera' in logger_name: #TODO fix != hacky solution
            log_file_name = logger_name
        if log_file_name and config.get_settings().get('logging', {}).get('enabled'):
            if config.get_settings().get('logging', {}).get('erase_on_start'):
                clear_logs()
            try:
                if subfolder:
                    subfolder = os.path.join(paths.CURRENT_SETTINGS_FOLDER, subfolder)
                    if not os.path.isdir(subfolder):
                        if os.path.isfile(subfolder) or os.path.islink(subfolder):
                            try:
                                os.remove(subfolder)
                            except:
                                pass
                        os.mkdir(subfolder)
                    log_file_name = os.path.join(subfolder, log_file_name + ".log")
                file_handler = logging.handlers.RotatingFileHandler(log_file_name, 'a', LOG_FILE_SIZE, LOG_BACKUPS)
                file_handler.setFormatter(logging.Formatter('%(asctime)s\t%(threadName)s/%(funcName)s\t%(message)s'))
                file_handler.setLevel(level)
                if REQUEST_SKIP:
                    skip_requests_filter = SkipRequestsFilter()
                    file_handler.addFilter(skip_requests_filter)
                logger.addHandler(file_handler)
                print("File logger created: " + log_file_name)
            except Exception as e:
                print('Could not create log file because' + str(e) + '\n.No log mode.')
    return logger


def log_exception(func_or_method=None, close_on_error=True, exit_on_error=True):
    assert callable(func_or_method) or func_or_method == None
    def _decorator(deco_func):
        @functools.wraps(deco_func)
        def wrapper(*args, **kwargs):
            try:
                result = deco_func(*args, **kwargs)
            except SystemExit:
                pass
            except Exception as e:
                if (not isinstance(e, OSError) or \
                   getattr(e, 'errno', None) != errno.EINTR):
                    report_exception()
                    if close_on_error and args and hasattr(args[0], "close"): # args[0] is self for object methods
                        try:
                            args[0].close()
                        except:
                            pass
                    if exit_on_error:
                        sys.exit(1)
            else:
                return result
        return wrapper
    if func_or_method:
        return _decorator(func_or_method)
    else:
        return _decorator


def report_exception():
    trace = traceback.format_exc()
    try:
        logging.getLogger(__name__).error(trace)
    except:
        print(trace)
    repeat_flag = trace in config.get_last_exception()
    if not repeat_flag:
        config.set_last_exception(trace)
    try:
        with open(EXCEPTIONS_LOG_FILE, "a+") as f: 
            if not repeat_flag:
                f.seek(0)
                prev_exceptions = f.read()
                repeat_flag = trace and trace in prev_exceptions
            if repeat_flag:
                last_line = trace.strip().strip("\n").split("\n")[-1]
                f.write("Repeat: %s\n" % last_line)
            else:
                f.write(form_log_title() + time.ctime() + "\n" + trace + "\n")
    except:
        pass
    if not repeat_flag and config.get_settings()['logging']['auto_report_exceptions'] and\
       not getattr(config.get_app(), 'offline_mode', True):
        try:
            send_logs()
        except:
            pass


def compress_logs():
    logger = logging.getLogger(__name__)
    try:
        log_file_names = os.listdir(CURRENT_SETTINGS_FOLDER)
    except:
        logger.warning('No logs to pack')
    else:
        for name in log_file_names[:]:
            if not (name.startswith(os.path.basename(MAIN_LOG_NAME))
                    or name == os.path.basename(EXCEPTIONS_LOG_FILE)
                    or name == REPORT_FILE_NAME
                    or name == os.path.basename(CAMERA_LOG_FILE)
                    or name == DETECTION_REPORT_NAME
                    or name == REPORT_FILE_NAME):
                log_file_names.remove(name)
        for root, dirs, files in os.walk(os.path.join(CURRENT_SETTINGS_FOLDER, ATTACHMENTS_FOLDER_NAME)):
            for filename in files:
                log_file_names.append(os.path.join(root, filename))
        if log_file_names:
            zip_file_name = time.strftime("%Y_%m_%d___%H_%M_%S", time.localtime()) + ".zip"
            zip_file_name_path = os.path.abspath(os.path.join(CURRENT_SETTINGS_FOLDER, zip_file_name))
            logger.info('Creating zip file: %s' % zip_file_name)
            try:
                with zipfile.ZipFile(zip_file_name_path, mode='w') as zf:
                    for name in log_file_names:
                        zf.write(os.path.join(CURRENT_SETTINGS_FOLDER, name), name, compress_type=zipfile.ZIP_DEFLATED)
                    printer_logs_subdir = os.path.join(CURRENT_SETTINGS_FOLDER, PRINTERS_SUBFOLDER)
                    if os.path.isdir(printer_logs_subdir):
                        for name in os.listdir(printer_logs_subdir):
                            zf.write(os.path.join(printer_logs_subdir, name), PRINTERS_SUBFOLDER + "/" + name, compress_type=zipfile.ZIP_DEFLATED)
            except Exception as e:
                logger.warning("Error while creating logs archive " + zip_file_name_path + ': ' + str(e))
                if os.path.isfile(zip_file_name_path):
                    os.remove(zip_file_name_path)
            else:
                return zip_file_name_path


def upload_compressed_logs(zip_file_path):
    # NOTE this upload should not be moved to http_client, because it only works reliably with requests(lib)
    # otherwise you will need to implement chunk uploading using http client
    # WARNING you will need a valid report_file for logs uploading to work!
    logger = logging.getLogger(__name__)
    connection_class = http_client.get_printerinterface_protocol_connection()
    if connection_class.HTTPS_MODE:
        prefix = 'https://'
    else:
        prefix = 'http://'
    url = prefix \
        + connection_class.URL \
        + connection_class.API_PREFIX \
        + connection_class.TOKEN_SEND_LOGS
    try:
        if connection_class == http_client.HTTPClientPrinterAPIV1:
            url = connection_class.patch_api_prefix(url)
            tokens = config.get_app().user_login.auth_tokens
            if tokens: #FIXME not threadsafe and not taking in account multiple printers with apiprinter
                token = config.get_app().user_login.auth_tokens[-1][1]
            else:
                return 'Error logs uploading failed: no auth token'
        else:
            token = config.get_app().user_login.user_token
        data = {connection_class.SEND_LOGS_TOKEN_FIELD_NAME: token}
        logger.info('Sending logs to %s' % url)
        response = None
        with open(zip_file_path, 'rb') as zip_file:
            files = {'file_data': zip_file}
            report_file = None
            if os.path.isfile(REPORT_FILE):
                report_file = open(REPORT_FILE, 'rb')
                files['report_file'] = report_file
            if os.path.isfile(DETECTION_REPORT_FILE):
                integration_file = open(DETECTION_REPORT_FILE, 'rb')
                files['integration_request_file'] = integration_file
            response = requests.post(url, data=data, files=files)
        for f in files:
            if not files[f].closed:
                files[f].close()
    except Exception as e:
        return 'Error while sending logs: ' + str(e)
    else:
        if response != None:
            result = response.text
            logger.debug("Log sending response: " + result)
            if response.status_code != 200:
                return 'Error while uploading logs: response code is not 200 (OK)'
            try:
                answer = json.loads(result)
            except Exception as e:
                return 'Error while uploading logs: ' + str(e)
            if type(answer) == dict and not answer.get('success'):
                return result
    finally:
        if os.path.isfile(zip_file_path):
            os.remove(zip_file_path)


def send_logs(force=False):
    logger = logging.getLogger(__name__)
    if force or config.get_settings()['logging']['logs_sending']:
        zip_file_path = compress_logs()
        if not zip_file_path:
            return 'Error while packing logs'
        error = upload_compressed_logs(zip_file_path)
        if error:
            logger.warning(error)
            return error
        logger.debug('Logs successfully sent')
        return clear_logs()
    else:
        logger.warning("Can't send logs - disabled in config")


def clear_logs():
    logger = logging.getLogger(__name__)
    for handler in logger.handlers:
        handler.flush()
    remove_old_logs()
    try:
        with open(MAIN_LOG_NAME, 'w') as f:
            f.write(form_log_title() + "\n")
        for filepath in (EXCEPTIONS_LOG_FILE, REPORT_FILE, DETECTION_REPORT_FILE):
            clear_log_file(filepath)
        printer_logs_subdir = os.path.join(CURRENT_SETTINGS_FOLDER, PRINTERS_SUBFOLDER)
        if os.path.isdir(printer_logs_subdir):
            for name in os.listdir(printer_logs_subdir):
               clear_log_file(os.path.join(printer_logs_subdir, name))
    except Exception as e:
        error = 'Error while clearing logs: ' + str(e)
        logger.warning(error)
        return error
    try:
        os.remove(os.path.join(CURRENT_SETTINGS_FOLDER, CAMERA_LOG_FILE))
    except (OSError, IOError):
        pass
    logger.info('Logs successfully cleared')


def clear_log_file(filepath):
    for number in range(1, 10):
        suffix = '.' + str(number)
        if filepath.endswith(suffix):
            try:
                os.remove(filepath)
                return
            except OSError:
                pass
    remove_old_logs()
    try:
        if os.path.exists(filepath):
            with open(filepath, "w") as f:
                f.write('Log clear\n')
    except OSError:
        pass


def remove_old_logs(filename=MAIN_LOG_NAME):
    logger = logging.getLogger(__name__)
    for handler in logger.handlers:
        handler.do_rollover()
    for number in range(1, 10):
        ending = "." + str(number)
        try:
            os.remove(filename + ending)
        except (OSError, IOError):
            break


def get_file_tail(file_path):
    tail_lines_list = []
    if os.path.isfile(file_path):
        with open(file_path) as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            tail_length = min(TAIL_LINES * AVERAGE_LINE_LENGTH, size)
            position = size - tail_length
            f.seek(position)
            tail = f.read(tail_length)
            if tail:
                tail_lines_list = tail.split("\n")[1:]
                tail_lines_list.reverse()
    return tail_lines_list


def form_log_title():
    return f'Version: {version.full_version_string()}\n' + \
           f'Operating system: {platform.system()} {platform.release()}'


def create_report(report_text):
    report_text = form_log_title() + '\n\n' + report_text
    paths.check_and_create_dirs(REPORT_FILE)
    with open(REPORT_FILE, 'w') as f:
        f.write(report_text)


def report_problem(report_text):
    create_report(report_text)
    result = send_logs()
    return result


def open_settings_folder():
    path = os.path.abspath(CURRENT_SETTINGS_FOLDER)
    if sys.platform.startswith('darwin'):
        subprocess.Popen(['open', path], close_fds=True)

    elif sys.platform.startswith('linux'):
        subprocess.Popen(['xdg-open', path], close_fds=True)
    elif sys.platform.startswith('win'):
        subprocess.Popen(['explorer', path], close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)


def get_all_printer_logs_paths():
    printer_logs_subdir = os.path.join(CURRENT_SETTINGS_FOLDER, PRINTERS_SUBFOLDER)
    if os.path.isdir(printer_logs_subdir):
        return os.path.isdir(printer_logs_subdir)
    return []


def get_printer_log_file_path(printer_id):
    return os.path.join(paths.CURRENT_SETTINGS_FOLDER, PRINTERS_SUBFOLDER, printer_id + ".log")
