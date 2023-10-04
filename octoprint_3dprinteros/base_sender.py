# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019.
# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)

import base64
import collections
import json
import logging
import os
import re
import sys
import subprocess
import time
import tempfile
import threading
import zipfile

import config
import filename
import platforms
import paths


class BaseSender:

    TEMPERATURE_SIGNS_AFTER_DOT = 2 # rounding done using this
    REPORT_JOBS = False
    BUFFER_CLASS = collections.deque
    MEMORY_STORE_COOF = 1.6
    MEMORY_MARGIN = 60 * 1024 * 1024 # 60MB
    GCODES_PREPROCESS_BUFFER = 20 * 1024 * 1024 # 20MB
    COMMENT_CHARS = [b";"]
    DEFAULT_GCODES_BUFFER_SIZE = 256 #lines

    NATIVE_FILE_EXTENSION = ".gcode"
    UNZIP_SUBPROCESS_LINE = [sys.executable, "-m", "zipfile", "-e"]
    # UGZIP_SUBPROCESS_LINE = [sys.executable, "-m", "gzip", "-d"]

    @classmethod
    def strip_line_form_junk(cls, line):
        line = line.translate(None, b"\r\n").expandtabs(4)
        for comment_char in cls.COMMENT_CHARS:
            line = line.split(comment_char)[0].strip() 
        return line


    def __init__(self, parent=None, usb_info={}, profile={}):
        if parent:
            self.logger = parent.logger.getChild(self.__class__.__name__)
        else:
            self.logger = logging.getLogger(self.__class__.__name__)
        self.stop_flag = False
        if parent: 
            self.parent = parent
        else:
            import _fake_parent
            self.parent = _fake_parent.FakeParent()
        self.usb_info = usb_info
        if not hasattr(self, "temps"):
            self.temps = [0.0, 0.0]
            self.target_temps = [0.0, 0.0]
        self.position = [0.0, 0.0, 0.0, 0.0]  # X, Y, Z, E
        self.profile = profile # empty profile is ok for some senders
        self.estimated_time = None
        self.buffer = None
        self.total_gcodes = 0
        self.current_line_number = 0
        self.percent = 0.0
        self.operational_flag = False
        self.printing_flag = False
        self.pause_flag = False
        self.heating = False #TODO rename to heating_flag after merge
        self.callbacks_lock = threading.Lock()
        self.response_callbacks = [] # functions list to call on printer_response. if not empty it will suppress tempratures requesting.
        self.responses_planned = 0
        self.responses = []
        self.filename = None
        self.filesize = 0
        self.full_filename = ""
        self.displayed_filename = ""
        self.est_print_time = 0 # in seconds
        self.print_time_left = 0 # in seconds
        self.average_printing_speed = 0 # percents per second
        self.clouds_job_id = None
        self.printers_job_id = None
        self.settings = {}
        if not usb_info or not isinstance(usb_info, dict):
            raise RuntimeError("Invalid or empty printer id:" % usb_info)
        self.override_clouds_estimations = config.get_settings().get('print_estimation', {}).get('override_clouds', False)
        self.allow_increase_of_print_time_left = config.get_settings().get('print_estimation', {}).get('allow_rise_time_left', False)
        self.intercept_pause = config.get_settings().get('intercept_pause')
        self.keep_print_files = config.get_settings().get('keep_print_files', False)
        #self.heating_start_time = None
        #self.last_pause_time = None
        #self.print_start_time = 0.0
        #self.sum_pause_and_heating_duration = 0 # in seconds
        #self.pause_and_heating_duration_lock = threading.Lock()
        self.is_base64_re = re.compile(b"^([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{4}|[A-Za-z0-9+/]{3}=|[A-Za-z0-9+/]{2}==)$")

    def set_total_gcodes(self, length):
        self.total_gcodes = length

    def load_printer_settings(self):
        try:
            with open(os.path.join(paths.CURRENT_SETTINGS_FOLDER, str(self.parent) + ".json")) as f:
                settings = json.loads(f.read())
                self.logger.info(f'Loaded printer settings {settings}')
        except FileNotFoundError:
            pass
        except Exception as e:
            self.logger.error('Error on loading settings. Removing corrupted file')
            try:
                os.remove(os.path.join(paths.CURRENT_SETTINGS_FOLDER, str(self.parent) + ".json"))
            except OSError:
                pass
        else:
            self.settings = settings
            profile_overrides = settings.get('profile_overrides')
            if profile_overrides:
                self.profile.update(profile_overrides)

    def update_printer_settings(self, update_dict):
        self.logger.info("Updating settings for with {update_dict}")
        self.settings.update(update_dict)
        self.save_printer_settings()

    def save_printer_settings(self):
        try:
            with open(os.path.join(paths.CURRENT_SETTINGS_FOLDER, str(self.parent) + ".json"), "w") as f:
                f.write(json.dumps(self.settings))
                self.logger.info('Saved printer settings')
        except Exception as e: 
            self.logger.error(f'Unable to save settings. Error: {e}')

    def get_total_gcodes(self):
        return self.total_gcodes

    def load_gcodes(self, gcodes):
        raise NotImplementedError

    def unbuffered_gcodes(self, gcodes):
        raise NotImplementedError

    def unbuffered_gcodes_base64(self, gcodes):
        decoded_gcodes = self.decode_base64(gcodes)
        if decoded_gcodes:
            return self.unbuffered_gcodes(decoded_gcodes)
        return False

    def cancel(self):
        self.register_error(605, "Cancel is not supported for this printer type", is_blocking=False)
        return False

    def process_gcodes_file(self, gcodes_file):
        if self.file_can_fit_memory(gcodes_file):
            gcodes_out = self.BUFFER_CLASS()
            try:
                with open(gcodes_file, "rb") as f:
                    for line in f:
                        line = line.split(b";")[0].strip()
                        if line:
                            gcodes_out.append(line)
            except OSError:
                self.parent.register_error(85, "File loading error. Cancelling...", is_blocking=True)
            return gcodes_out

    def preprocess_gcodes(self, gcodes_in):
        gcodes_out = collections.deque()
        if gcodes_in:
            if type(gcodes_in) in (list, tuple, collections.deque):
                if type(gcodes_in[0]) == str:
                    sep = "\n"
                else:
                    sep = b"\b"
                gcodes_in = sep.join(gcodes_in)
            if type(gcodes_in) == str:
                partline = ""
            else:
                partline = b""
            while gcodes_in:
                if self.parent and self.parent.stop_flag:
                    break 
                buf_len = min(len(gcodes_in), self.GCODES_PREPROCESS_BUFFER)
                buf = partline + gcodes_in[:buf_len]
                gcodes_in = gcodes_in[buf_len:]
                if type(buf) == str:
                    buf = buf.encode("utf-8")
                    partline = ""
                else:
                    partline = b""
                buf = buf.replace(b"\r", b"")
                if buf:
                    lines = buf.split(b'\n')
                    if gcodes_in:
                        partline = lines.pop()
                    else:
                        while lines and not lines[-1]:
                            lines.pop()
                    gcodes_out.extend(lines)
        self.logger.info('Got %d gcodes to execute.' % len(gcodes_out))
        return gcodes_out
    
    def is_enough_memory(self, size):
        free_mem = self.get_free_memory()
        if free_mem:
            return free_mem > (size * self.MEMORY_STORE_COOF + self.MEMORY_MARGIN)
        return True # always try if we can't determine a free memory

    def file_can_fit_memory(self, filepath):
        try:
            size = os.path.getsize(filepath)
            self.logger.info(f'Loading gcodes file of size: {size/1024/1024}MB' )
        except:
            self.logger.exception('Exception on getting file size:')
        else:
            if not self.is_enough_memory(size):
                self.parent.register_error(88, "Not enough memory. Cancelling...", is_blocking=True)
                return False
        return True

    def gcodes(self, filepath, keep_file=False):
        success = False
        start_time = time.monotonic()
        size = 0
        self.logger.debug("Start loading gcodes...")
        if str(filepath).endswith(".zip"):
            gcodes = self.unzip_file(filepath, self.process_gcodes_file)
        else:
            gcodes = self.process_gcodes_file(filepath)
        if gcodes:
            success = self.load_gcodes(gcodes) != False # None is equal to True here
            if success:
                self.print_start_time = time.monotonic()
        else:
            self.logger.error('Error: empty gcodes unpack result')
        if not self.keep_print_files and not keep_file:
            try:
                os.remove(filepath)
            except:
                pass
        return success

    @property
    def filename(self):
        return self.displayed_filename

    @filename.setter
    def filename(self, new_filename):
        self.displayed_filename = new_filename

    def set_filename(self, cloud_filename):
        self.full_filename = ""
        self.displayed_filename = ""
        if cloud_filename:
            try:
                self.full_filename = str(cloud_filename)
                self.displayed_filename = filename.get_filename_ascii(os.path.splitext(os.path.basename(self.full_filename))[0])
            except:
                self.logger.warning("Filename is not str: %s" % cloud_filename)

    def get_filename(self):
        return self.displayed_filename 

    def set_estimated_print_time(self, est_print_time):
        estimation = 0
        try:
            estimation = int(est_print_time)
        except (TypeError, ValueError):
            self.logger.warning(f"Given estimated time can not be converted to integer: {est_print_time}")
        self.est_print_time = estimation
        self.print_time_left = estimation
        self.logger.info(f"Setting estimated print duration to {estimation} seconds")
        #self.sum_pause_and_heating_duration = 0
        #self.print_start_time = time.monotonic()
        #self.last_remaining_print_time_get = time.monotonic()

    def set_average_printing_speed(self, speed):
        self.logger.info(f"Average printing speed: {speed}")
        self.average_printing_speed = speed

    def get_remaining_print_time(self, ignore_state=False):
        time_left = 0
        if self.is_printing() or ignore_state:
            if self.est_print_time:
                time_left = int(self.est_print_time - self.est_print_time * self.get_percent() / 100)
            if self.average_printing_speed:
                if not time_left or self.override_clouds_estimations:
                    time_left = int((100 - self.get_percent()) / self.average_printing_speed)
        if self.print_time_left and time_left > self.print_time_left and not self.allow_increase_of_print_time_left:
            time_left = self.print_time_left
        self.logger.info(f"Time left:{time_left}")
        self.print_time_left = time_left
        return time_left

    def get_remaining_print_time_string(self, seconds=None):
        time_string = ""
        if seconds == None:
            seconds = self.get_remaining_print_time()
        if seconds:
            hours, minutes = divmod(seconds // 60, 60)
            if hours:
                time_string += f"{hours} hour"
                if hours != 1:
                    time_string += "s"
            if minutes:
                if time_string:
                    time_string += " "
                time_string += f"{minutes} minute"
                if minutes != 1:
                    time_string += "s"
            if seconds and not hours and not minutes:
                time_string = "less than a minute"
            self.logger.debug("Remaining print time " + time_string)
        return time_string

    #  def get_remaining_print_time(self, ignore_state=False):
    #      if self.is_printing() or ignore_state:
    #          if self.print_time_left and self.est_print_time:
    #              now = time.monotonic()
    #              with self.pause_and_heating_duration_lock:
    #                  if self.heating_start_time:
    #                      self.sum_pause_and_heating_duration += now - self.heating_start_time
    #                      self.heating_start_time = None
    #                  if self.pause_flag:
    #                      if self.last_pause_time:
    #                          self.update_pause_time_and_duration()
    #                  else:
    #                      if self.last_pause_time:
    #                          self.update_unpause_time_and_duration()
    #              #time_progress_relation = int((self.print_start_time + self.sum_pause_and_heating_duration - ) /  print_time_left)
    #              #time_coef = (((self.estimated_print_time - elapsed_time)/self.estimated_print_time) * (1 - self.get_percent() / 100) ** 0.5
    #              time_relation = (self.est_print_time + self.sum_pause_and_heating_duration - self.print_time_left) / self.est_print_time
    #              progress_relation = (100 - self.get_percent()) / 100
    #              if progress_relation:
    #                  speed_coefficient = time_relation / progress_relation
    #              else:
    #                  speed_coefficient = 1
    #              delta_time = now - self.last_remaining_print_time_get
    #              self.last_remaining_print_time_get = now
    #              self.print_time_left -= int(delta_time * speed_coefficient)
    #              if self.print_time_left < 0:
    #                  self.print_time_left = 0
    #              self.logger.info("Remaining print time %s second" % self.print_time_left)
    #              return self.print_time_left 
    #      return 0

    def get_position(self):
        return self.position

    def get_temps(self):
        return self.temps

    def get_target_temps(self):
        return self.target_temps

    def get_percent(self):
        return self.percent

    def set_percent(self, percent):
        self.percent = percent

    def get_current_line_number(self):
        return self.current_line_number

    #  def update_pause_time_and_duration(self):
    #      with self.pause_and_heating_duration_lock:
    #          now = time.monotonic()
    #          if self.last_pause_time is not None:
    #              self.sum_pause_and_heating_duration += now - self.last_pause_time
    #          self.last_pause_time = now

    #  def update_unpause_time_and_duration(self):
    #      with self.pause_and_heating_duration_lock:
    #          now = time.monotonic()
    #          self.sum_pause_duration = now - self.last_pause_time 
    #          self.last_pause_time = None

    def pause(self):
        self.pause_flag = True
        #self.update_pause_time_and_duration()

    def unpause(self):
        self.pause_flag = False
        #self.update_unpause_time_and_duration()

    def resume(self):
        self.unpause()

    def is_printing(self):
        return self.printing_flag

    def is_paused(self):
        return self.pause_flag

    def is_operational(self):
        return self.operational_flag

    def is_heating(self):
        return self.heating

    def is_bed_not_clear(self):
        return False

    def is_empty(self):
        return False

    def is_maintenance(self):
        return False

    def get_downloading_percent(self):
        return self.parent.downloader.get_percent()

    def get_nonstandart_data(self):
        return {}

    def execute_callback(self, line, success):
        for callback in self.response_callbacks:
            try:
                callback(line, success)
            except:
                self.logger.exception("Exception in callback(%s):" % str(callback))

    def round_temps_list(self, temps_list):
        index = 0
        while index < len(temps_list):
            temps_list[index] = round(temps_list[index], self.TEMPERATURE_SIGNS_AFTER_DOT)
            index += 1
        return temps_list

    def add_response_callback(self, callback_function):
        self.logger.info("Adding callback: %s" % callback_function)
        with self.callbacks_lock:
            if not callback_function in self.response_callbacks:
                self.response_callbacks.append(callback_function)
                self.logger.info("Callback added: %s" % callback_function)

    def del_response_callback(self, callback_function):
        self.logger.info("Removing callback: %s" % callback_function)
        with self.callbacks_lock:
            self.response_callbacks.remove(callback_function)
            self.logger.info("Callback removed: %s" % callback_function)

    def flush_response_callbacks(self):
        with self.callbacks_lock:
            for callback in self.response_callbacks:
                try:
                    self.response_callbacks.remove(callback)
                    self.logger.info("Callback removed: %s" % callback)
                except ValueError:
                    pass

    def init_speed_calculation_thread(self):
        if config.get_settings().get('print_estimation', {}).get('by_print_speed'):
            self.logger.info("Starting print speed calculation thread")
            self.speed_calculation_thread = SpeedCalculationThread(self)
            self.speed_calculation_thread.start()
        else:
            self.logger.info("Print speed calculation is disabled. No thread start")

    def get_jobs(self):
        return {}

    def get_material_names(self):
        return []

    def get_material_volumes(self):
        return []

    def get_material_desc(self):
        """
            primary - default == None
            support
            secondary
            aux - for ink like materials, added to primary(on hp for example)
        """
        return []

    def get_ext(self):
        return {}

    def register_error(self, *args, **kwargs):
        if self.parent:
                self.parent.register_error(*args, **kwargs)
        else:
            self.logger.error(f'No parent to register error: {args} {kwargs}')

    def register_event_report(self, *args, **kwargs):
        if self.parent:
                self.parent.register_event_report(*args, **kwargs)
        else:
            self.logger.error(f'No parent to register event: {args} {kwargs}')

    def get_estimated_time(self):
        return self.estimated_time

    def get_clouds_job_id(self):
        return self.clouds_job_id

    def get_printers_job_id(self):
        return self.printers_job_id

    def close(self):
        self.stop_flag = True
        if self.buffer:
            self.buffer.close()
        if hasattr(self, 'speed_calculation_thread'):
            self.logger.info("Joining estimation thread...")
            self.speed_calculation_thread.join(self.speed_calculation_thread.LOOP_TIME)
            self.logger.info("...estimation thread joined")

    def get_free_memory(self):
        if platforms.PLATFORM in ('rpi', 'linux'):
            try:
                with open('/proc/meminfo') as f:
                    for line in f:
                        if 'MemAvailable' in line:
                            return int(line.split()[1]) * 1024
            except (OSError, IOError, ValueError, IndexError):
                pass

    def unzip_file(self, filepath, processor, remove_after=True):
        # you will need a callback here, since temporary directory always erases on destructor
        try:
            with tempfile.TemporaryDirectory(dir=paths.DOWNLOAD_FOLDER) as unzip_tmpdir_name:
                proc_args = self.UNZIP_SUBPROCESS_LINE + [str(filepath), unzip_tmpdir_name]
                proc = subprocess.Popen(proc_args)
                while not self.parent or not self.parent.stop_flag:
                    exit_code = proc.poll()
                    if exit_code == None:
                        time.sleep(0.1)
                    elif exit_code == 0:
                        files_list = os.listdir(unzip_tmpdir_name)
                        self.logger.info(f'Zip contents: {files_list}')
                        if len(files_list) == 1:
                            filename = files_list[0]
                        else:
                            bigest_file_name = ""
                            bigest_file_size = 0
                            right_extension_files = []
                            for filename in files_list:
                                if filename.endswith(self.NATIVE_FILE_EXTENSION):
                                    right_extension_files.append(filename)
                                try:
                                    size = os.path.getsize(filename)
                                except OSError:
                                    size = 0
                                if size > bigest_file_size:
                                    bigest_file_size = size
                                    bigest_file_name = filename
                            if not right_extension_files or \
                                (len(right_extension_files) > 1 and bigest_file_name in right_extension_files):
                                filename = bigest_file_name
                            else:
                                filename = right_extension_files[0]
                        self.logger.info(f'File to print: {filename}')
                        filename = os.path.join(unzip_tmpdir_name, filename)
                        return processor(filename)
                    else:
                        self.logger.warning(f'Unzip return code {filepath}')
                        raise subprocess.SubprocessError
        except (zipfile.BadZipFile, OSError, IOError, subprocess.SubprocessError):
            self.register_error(87, "Unzip error. Cancelling...", is_blocking=True)
        finally:
            if remove_after:
                try:
                    os.remove(filepath)
                except OSError:
                    pass

    def decode_base64(self, gcodes):
        try:
            return base64.b64decode(gcodes)
        except:
            self.logger.errro("Attempt to decode non base64 gcodes")

    def register_print_finished_event(self):
        self.register_event_report({'state': 'printing', 'percent': 100.0})

    def register_print_cancelled_event(self):
        self.register_event_report({'state': 'cancel'})


class SpeedCalculationThread(threading.Thread):

    LOOP_STEPS = 100
    LOOP_TIME = 6 # seconds
    SPEEDS_QUEUE_LEN = 24

    def __init__(self, sender):
        self.sender = sender
        self.stop_flag = False
        self.speeds_log = collections.deque(maxlen=self.SPEEDS_QUEUE_LEN)
        self.logger = sender.logger.getChild(self.__class__.__name__)
        super().__init__()

    def get_average_speed(self):
        if len(self.speeds_log) == self.SPEEDS_QUEUE_LEN:
            try: #NOTE could use normalize or other formulas instead of average, too increase accuracy
                return sum(self.speeds_log) / self.SPEEDS_QUEUE_LEN
            except IndexError:
                self.logger.exception("Exception while getting average print speed:")

    def run(self):
        printing_counter = 0
        nonprinting_counter = 0
        sleep = self.LOOP_TIME / self.LOOP_STEPS
        last_time = time.monotonic()
        last_percent = 0.0 
        self.logger.info('Entering speed calculation loop')
        while not self.stop_flag and not self.sender.stop_flag:
            if self.sender.is_operational() and self.sender.is_printing():
                printing_counter += 1
            else:
                nonprinting_counter += 1
            if nonprinting_counter >= self.LOOP_STEPS:
                nonprinting_counter = 0
                if self.speeds_log:
                    self.speeds_log.clear()
                    self.sender.set_average_printing_speed(0)
            if printing_counter < self.LOOP_STEPS:
                time.sleep(sleep)
            else:
                printing_counter = 0
                if self.sender.is_printing() and not self.sender.is_paused() and \
                        not self.sender.is_heating():
                    percent = self.sender.get_percent()
                    delta_time = time.monotonic() - last_time
                    if percent and delta_time:
                        speed = (percent - last_percent) / delta_time
                        self.logger.info(f'Print speed: {speed} %/s')
                        self.speeds_log.append(speed)
                        avg_speed = self.get_average_speed()
                        if avg_speed:
                            self.sender.set_average_printing_speed(avg_speed)
                        self.logger.info(f"Delta:{delta_time} Speed:{speed} Avg:{avg_speed}")
                last_percent = self.sender.get_percent()
                last_time = time.monotonic()
