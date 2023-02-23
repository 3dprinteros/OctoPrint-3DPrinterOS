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
import requests
import tempfile
import threading
import time
import zipfile


class Downloader(threading.Thread):
    CONNECTION_TIMEOUT = 6
    MAX_RETRIES = 5
    DOWNLOAD_CHUNK_SIZE = 128*1024  # 128kB
    ALLOW_UNVERIFIED_STORAGE = False

    def __init__(self, parent, url, callback, is_zip):
        self.logger = parent.logger
        self.parent = parent
        self.url = url
        self.callback = callback
        self.is_zip = is_zip
        self.cancel_flag = False
        self.download_size = 0
        self.downloaded_bytes = 0
        self.written_bytes = 0
        self.percent = 0.0
        self.tmp_file = None
        threading.Thread.__init__(self, name="Downloader")

    def run(self):
        self.logger.info('Starting downloading')
        downloaded_filename = self.download()
        if downloaded_filename:
            try:
                if self.is_zip:
                    with zipfile.ZipFile(downloaded_filename) as zf:
                        filename = zf.namelist()[0]
                        with zf.open(filename, 'r') as f:
                            self.execule_callback(f)
                else:
                    with open(downloaded_filename, 'rb') as f:
                        self.execule_callback(f)
                self.logger.info('Gcodes loaded to memory, deleting temp file')
                os.remove(downloaded_filename)
            except IndexError:
                self.parent.register_error(86, "Empty zipfile. Cancelling...", is_blocking=True)
            except (OSError, IOError):
                self.parent.register_error(87, "Temporary file error. Cancelling...", is_blocking=True)
            except MemoryError:
                self.parent.register_error(88, "Out of memory. Cancelling...", is_blocking=True)
            except zipfile.error:
                self.parent.register_error(89, "Bad zip file. Cancelling...", is_blocking=True)
        if self.cancel_flag:
            self.logger.info('Cancel command was received after printing start in downloading thread')
            try:
                self.parent.printer.cancel()
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
            self.callback(f.read())

    def download(self):
        self.logger.info("Downloading from " + self.url)
        if self.is_zip:
            suffix = ".zip"
        else:
            suffix = ".g"
        self.tmp_file = tempfile.NamedTemporaryFile(mode='wb', delete=False, prefix='3dprinteros-', suffix=suffix)
        filename = self.tmp_file.name
        retry = 0
        compression = None
        while retry < self.MAX_RETRIES and not self.parent.stop_flag and not self.cancel_flag:
            if retry:
                self.logger.warning("Download retry/resume N" + str(retry))
            self.logger.info("Connecting to " + self.url)
            headers = {'Accept-Encoding': 'identity, deflate, compress, gzip',
                       'Accept': '*/*', 'User-Agent': 'python-requests/'+str(requests.__version__)}
            if self.downloaded_bytes:
                if compression:
                    self.downloaded_bytes = 0
                    self.percent = 0.0
                    self.written_bytes = 0
                    self.tmp_file.truncate(0)
                    self.logger.info('Unable to resume with compression '+str(compression)+'. Restarting download')
                else:
                    headers['Range'] = 'bytes=%d-' % self.downloaded_bytes
                    self.logger.info('Resuming download from '+str(self.downloaded_bytes))
            response = None
            try:
                response = requests.get(self.url, headers=headers, stream=True, timeout=self.CONNECTION_TIMEOUT,
                                        verify=not self.ALLOW_UNVERIFIED_STORAGE)
            except Exception as e:
                self.parent.register_error(65, "Unable to open download link: " + str(e), is_blocking=False)
                time.sleep(self.CONNECTION_TIMEOUT*(retry+1))
            else:
                self.logger.info('Response headers:' + str(response.headers))
                if not response.ok:
                    self.parent.register_error(
                        68, 'Download error: HTTP status not OK, but '+str(response.status_code), is_blocking=False)
                else:
                    if not self.download_size:
                        self.download_size = int(response.headers.get('content-length', 0))
                        self.logger.info("Starting download of "+str(self.download_size)+"B")
                        compression = response.headers.get('Content-Encoding')
                        if compression:
                            self.logger.info("Download compression encoding: " + str(compression))
                    self.downloaded_bytes += self.download_chunks(response)
                    self.logger.info("Downloaded "+str(self.downloaded_bytes)+"B")
                    if self.downloaded_bytes == self.download_size:
                        self.logger.info('Downloading finished in '+str(self.download_size) +
                                         'B. Written '+str(self.written_bytes)+'B')
                        self.tmp_file.close()
                        return filename
                    elif self.downloaded_bytes > self.download_size:
                        self.parent.register_error(66, "Download error: data is corrupted", is_blocking=True)
                        break
                    else:
                        self.parent.register_error(66, "Download error: connection lost. Retrying/resuming",
                                                   is_blocking=False)
            finally:
                if response:
                    response.close()
                retry += 1
                time.sleep(1)
        self.parent.register_error(66, 'Download error: unable to finish download', is_blocking=False)
        self.tmp_file.close()
        try:
            os.remove(filename)
        except:
            pass

    def download_chunks(self, response):
        downloaded_bytes = 0
        prev_percent = 0
        try:
            for chunk in response.iter_content(self.DOWNLOAD_CHUNK_SIZE):
                if self.cancel_flag or self.parent.stop_flag:
                    self.logger.info('Download canceled')
                    return downloaded_bytes
                downloaded_bytes = response.raw.tell()
                if self.download_size:
                    self.percent = round(
                        min((downloaded_bytes + self.downloaded_bytes) / self.download_size, 1.0) * 100, 2)
                    if self.percent > prev_percent:
                        self.logger.info('File downloading: '+str(self.percent)+'%')
                        prev_percent = self.percent
                else:
                    self.logger.info("File downloading: " +
                                     str((downloaded_bytes + self.downloaded_bytes) // 1024)+"kB")
                self.tmp_file.write(chunk)
                self.written_bytes += len(chunk)
        except Exception as e:
            self.parent.register_error(69, 'Download error: chunk error: ' + str(e), is_blocking=False)
        else:
            self.percent = 100
        finally:
            return downloaded_bytes

    def cancel(self):
        self.cancel_flag = True

    def get_percent(self):
        return self.percent
