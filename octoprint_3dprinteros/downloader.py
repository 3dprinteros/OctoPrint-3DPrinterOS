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
import tempfile
import threading
import time
import io

import requests
import certifi

import config
import log
import paths


class Downloader(threading.Thread):

    CONNECTION_TIMEOUT = 6
    MAX_RETRIES = 5
    DOWNLOAD_CHUNK_SIZE = 128*1024 #128kB

    def __init__(self, parent, url, callback, is_zip):
        self.logger = parent.logger.getChild(self.__class__.__name__)
        self.parent = parent
        self.url = url
        self.callback = callback
        self.in_memory_gcodes = False
        if config.get_settings()['in_memory_gcodes']:
            try:
                self.in_memory_gcodes = getattr(parent.sender, 'print_bytes')
                if self.in_memory_gcodes:
                    self.logger.info('In memory gcodes mode enabled')
            except:
                pass
        self.is_zip = is_zip
        self.cancel_flag = False
        self.download_size = 0
        self.downloaded_bytes = 0
        self.written_bytes = 0 
        self.percent = 0.0
        threading.Thread.__init__(self, name="Downloader", daemon=True)

    @log.log_exception
    def run(self):
        self.logger.info('Starting downloading')
        downloaded = self.download()
        if downloaded and self.callback:
            if self.in_memory_gcodes:
                self.logger.info('In memory gcodes mode enabled. Overriding download complete callback with load_text')
                self.parent.sender.print_bytes(downloaded)
            else:
                self.execule_callback(downloaded)
        if self.cancel_flag:
            self.logger.info('Cancel command was received after printing start in downloading thread')
            try:
                self.parent.sender.cancel()
            except AttributeError:
                pass
        self.logger.info('Downloading finished')

    def execule_callback(self, f):
        if self.cancel_flag:
            self.logger.info('Cancel command received')
            self.cancel_flag = False
        elif not f:
            self.parent.register_error(67, "Unknown download error", is_blocking=True)
        else:
            self.callback(f)

    def download(self):
        if config.get_settings().get('hide_sensitive_log'):
            self.logger.info("Downloading from __hidden__")
        else:
            self.logger.info("Downloading from " + self.url)
        if self.is_zip:
            suffix = ".zip"
        else:
            suffix = ".gcode"
        if self.in_memory_gcodes:
            tmp_file = io.BytesIO()
            filename = None
        else:
            tmp_file = tempfile.NamedTemporaryFile(mode='wb', dir=paths.DOWNLOAD_FOLDER,
                delete=False, prefix='3dprinteros-', suffix=suffix)
            filename = tmp_file.name
        retry = 0
        compression = None
        while retry < self.MAX_RETRIES and not self.parent.stop_flag and not self.cancel_flag:
            if retry:
                self.logger.warning("Download retry/resume N" + str(retry))
            self.logger.info("Connecting to server...")
            headers = { 'Accept-Encoding': 'identity, deflate, compress, gzip',
                     'Accept': '*/*', 'User-Agent': 'python-requests/{requests.__version__}'}
            if self.downloaded_bytes:
                if compression:
                    self.downloaded_bytes = 0
                    self.percent = 0.0
                    self.written_bytes = 0
                    tmp_file.truncate(0)
                    self.logger.info(f'Unable to resume with compression {compression}. Restarting download')
                else:
                    headers['Range'] = 'bytes=%d-' % self.downloaded_bytes
                    self.logger.info(f'Resuming download from {self.downloaded_bytes}')
            try:
                response = requests.get(self.url, headers = headers, stream=True, timeout = self.CONNECTION_TIMEOUT, verify=certifi.where())
            except Exception as e:
                response = None
                self.parent.register_error(65, "Unable to open download link: " + str(e), is_blocking=False)
                time.sleep(self.CONNECTION_TIMEOUT*(retry+1))
            else:
                self.logger.info('Response headers:' + str(response.headers))
                if not response.ok:
                    self.parent.register_error(68, f'Download error: HTTP status not OK, but {response.status_code}', is_blocking=False)
                else:
                    if not self.download_size:
                        self.download_size = int(response.headers.get('content-length', 0))
                        self.logger.info(f"Starting download of {self.download_size}B")
                        compression = response.headers.get('Content-Encoding')
                        if compression:
                            self.logger.info("Download compression encoding: " + str(compression))
                    self.downloaded_bytes += self.download_chunks(response, tmp_file)
                    self.logger.info(f"Downloaded {self.downloaded_bytes}B")
                    if self.downloaded_bytes == self.download_size:
                        self.logger.info(f'Success. Downloaded: {self.download_size}B. Wrote: {self.written_bytes}B')
                        if self.in_memory_gcodes:
                            tmp_file.seek(0)
                            ret = tmp_file.read()
                        else:
                            ret = filename
                        tmp_file.close()
                        return ret
                    elif self.downloaded_bytes > self.download_size:
                        self.parent.register_error(66, "Download error: data is corrupted. Expected: {self.download_size}B. Downloaded: {self.download_size}B. Wrote: {self.written_bytes}B", is_blocking=False)
                        break
                    else: 
                        self.parent.register_error(66, "Download error: connection was lost. Expected: {self.download_size}B. Downloaded: {self.download_size}B. Wrote: {self.written_bytes}B", is_blocking=False)
            finally:
                if response:
                    response.close()
                retry += 1
                time.sleep(1)
        self.parent.register_error(66, 'Download error: unable to complete download', is_blocking=True)
        try:
            tmp_file.close()
        except:
            pass
        try:
            if filename:
                os.remove(filename)
        except:
            pass

    def download_chunks(self, response, tmp_file):
        downloaded_bytes = 0
        prev_percent = 0
        try:
            for chunk in response.iter_content(self.DOWNLOAD_CHUNK_SIZE):
                if self.cancel_flag or self.parent.stop_flag:
                    self.logger.info('Download canceled')
                    return downloaded_bytes
                downloaded_bytes = response.raw.tell()
                if self.download_size:
                    self.percent = round(min((downloaded_bytes + self.downloaded_bytes) / self.download_size, 1.0) * 100, 2)
                    if self.percent > prev_percent:
                        self.logger.info(f'File downloading: {self.percent}%')
                        prev_percent = self.percent
                else:
                    self.logger.info(f"File downloading: {(downloaded_bytes + self.downloaded_bytes) // 1024}kB")
                tmp_file.write(chunk)
                self.written_bytes += len(chunk)
        except Exception as e:
            self.parent.register_error(69, 'Download error: chunk error: ' + str(e), is_blocking=False)
            return downloaded_bytes
        else:
            self.percent = 100
            return downloaded_bytes

    def cancel(self):
        self.cancel_flag = True

    def get_percent(self):
        return self.percent
