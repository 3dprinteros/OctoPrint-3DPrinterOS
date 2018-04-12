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

import os
import time
import zipfile
import tempfile
import requests
import threading

# import log

class Downloader(threading.Thread):

    CONNECTION_TIMEOUT = 6
    MAX_RETRIES = 15

    def __init__(self, parent, url, callback, is_zip):
        self.percent = 0
        self.url = url
        self.is_zip = is_zip
        self.parent = parent
        self.callback = callback
        self.cancel_flag = False
        self.logger = parent.logger
        threading.Thread.__init__(self, name="Downloader")

    # @log.log_exception
    def run(self):
        self.logger.info('Starting downloading')
        downloaded_filename = self.download()
        if downloaded_filename:
            if self.is_zip:
                with zipfile.ZipFile(downloaded_filename) as zf:
                    filename = zf.namelist()[0]
                    with zf.open(filename, 'r') as f:
                        text = f.read()
            else:
                with open(downloaded_filename, 'rb') as f:
                    text = f.read()
            if not self.cancel_flag:
                self.callback(text)
                self.logger.info('Gcodes loaded to memory, deleting temp file')
            else:
                self.logger.info('Cancel command was received, deleting temp file')
                self.cancel_flag = False
            try:
                os.remove(downloaded_filename)
            except (OSError, IOError):
                pass
            if self.cancel_flag:
                self.logger.info('Cancel command was received after printing start in downloading thread')
                self.parent.printer.cancel()
        elif not self.cancel_flag:
            self.parent.register_error(67, "Can't download gcodes", is_blocking=False)
        self.logger.info('Downloading finished')

    def download(self):
        self.logger.info("Downloading from " + self.url)
        self.tmp_file = tempfile.NamedTemporaryFile(mode='wb', delete=False, prefix='3dprinteros-', suffix='.gcode')
        resume_byte_pos = 0
        retry = 0
        while retry < self.MAX_RETRIES and not self.parent.stop_flag and not self.cancel_flag:
            if retry:
                self.logger.warning("Download retry/resume N%d" % retry)
            self.logger.info("Connecting to " + self.url)
            resume_header = {'Range': 'bytes=%d-' % resume_byte_pos}
            try:
                request = requests.get(self.url, headers = resume_header, stream=True, timeout = self.CONNECTION_TIMEOUT)
            except Exception as e:
                request = None
                self.parent.register_error(65, "Unable to open download link: " + str(e), is_blocking=False)
            else:
                self.logger.info("Successful connection to " + self.url)
                download_length = int(request.headers.get('content-length', 0))
                if download_length:
                    downloaded_size = self.download_chunks(request, download_length)
                    resume_byte_pos += downloaded_size
                    self.logger.info("Downloaded %d bytes" % resume_byte_pos)
                    if downloaded_size == download_length:
                        self.tmp_file.close()
                        return self.tmp_file.name
            finally:
                if request:
                    request.close()
                retry += 1
                time.sleep(1)
        self.tmp_file.close()

    def download_chunks(self, request, download_length):
        # Taking +1 byte with each chunk to compensate file length tail less than 100 bytes when dividing by 100
        percent_length = download_length / 100 + 1
        total_size = 0
        try:
            for chunk in request.iter_content(percent_length):
                if self.cancel_flag or self.parent.stop_flag:
                    self.logger.info('Download canceled')
                    break
                self.tmp_file.write(chunk)
                self.percent += 1
                total_size += len(chunk)
                self.logger.info('File downloading : %d%%' % self.percent)
        except Exception as e:
            self.parent.register_error(66, 'Error while downloading: ' + str(e.message), is_blocking=False)
        finally:
            return total_size

    def cancel(self):
        self.cancel_flag = True

    def get_percent(self):
        return self.percent
