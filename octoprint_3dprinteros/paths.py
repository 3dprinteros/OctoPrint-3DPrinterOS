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

import ctypes
import os
import platform
import sys
import time
import json

#TODO refactor this module to get rid of CONSTANTS after function and other bad code

APP_FOLDER = os.path.dirname(os.path.abspath(__file__))
SETTINGS_NAME = '.3dprinteros'
STORAGE_NAME = 'user_files'
DOWNLOAD_NAME = 'downloads'
SIZE_UNITS = ['B', 'kB', 'MB', 'GB']
SIZE_OUTPUT_TEMPLATE = "%.1f%s"
FOLDER_TO_CLEANUP_ON_CRASH = ('simplejson', 'requests')


def init_folder(name, parent_folder=None):
    if not parent_folder: 
        if sys.platform.startswith('win'):
            parent_folder = os.getenv('APPDATA')
            name = name.lstrip(".")
        else:
            parent_folder = os.path.expanduser("~")
    path = os.path.join(os.path.abspath(parent_folder), name)
    if os.path.isfile(path):
        os.remove(path)
    if not os.path.exists(path):
        os.mkdir(path)
    return path


# Ability to force user settings(as well as other related user data stuff) folder path in default_settings.json
with open(os.path.join(APP_FOLDER, "default_settings.json")) as f:
    settings = json.load(f)
    custom_dir = settings.get('custom_settings_home')


CURRENT_SETTINGS_FOLDER = init_folder(SETTINGS_NAME, custom_dir)
for folder in (STORAGE_NAME, DOWNLOAD_NAME):
    init_folder(folder, CURRENT_SETTINGS_FOLDER)


UPDATE_FILE_PATH = os.path.join(CURRENT_SETTINGS_FOLDER, '3dprinteros_client_update.zip')
PLUGIN_INSTALL_FILE_PATH = os.path.join(CURRENT_SETTINGS_FOLDER, "plugin_to_install.zip")
STORAGE_FOLDER = os.path.join(CURRENT_SETTINGS_FOLDER, STORAGE_NAME)
DOWNLOAD_FOLDER = os.path.join(CURRENT_SETTINGS_FOLDER, DOWNLOAD_NAME)
AUDIO_FILES_FOLDER = os.path.join(APP_FOLDER, 'audio_files')
OFFLINE_PRINTER_TYPE_FOLDER_PATH = os.path.join(CURRENT_SETTINGS_FOLDER, 'offline_printer_types')
RELEASE_NOTES_FILE_PATH = os.path.join(APP_FOLDER, 'release_notes.txt')
REQUEST_DUMPING_DIR = os.path.join(CURRENT_SETTINGS_FOLDER, 'request_dump')
CAMERA_URLS_FILE = os.path.join(CURRENT_SETTINGS_FOLDER, "camera_urls.txt")
UPDATE_FILE_NAME = '3dprinteros_client_update.zip'
UPDATE_FILE_PATH = os.path.join(CURRENT_SETTINGS_FOLDER, UPDATE_FILE_NAME)
CUSTOM_CACERT_PATH = os.path.join(CURRENT_SETTINGS_FOLDER, 'custom_ca.pem')
CERTIFI_CACERT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), 'certifi/cacert.pem'))
ENABLE_UART_PATH = os.path.join(CURRENT_SETTINGS_FOLDER, "uart")
DISABLE_UART_PATH = os.path.join(CURRENT_SETTINGS_FOLDER, "no-uart")

def get_libusb_path(lib):
    if sys.platform.startswith('win'):
        python_version = platform.architecture()[0]
        if '64' in python_version:
            libusb_name = 'libusb-1.0-64.dll'
        else:
            libusb_name = 'libusb-1.0.dll'
        pythons_folder = os.path.dirname(os.path.abspath(sys.executable))
        embeded_python_backend_path = os.path.join(pythons_folder, libusb_name)
        if os.path.isfile(embeded_python_backend_path):
            return embeded_python_backend_path
        local_backend_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), libusb_name)
        if os.path.isfile(local_backend_path):
            return local_backend_path


def check_and_create_dirs(path):
    while path:
        path = os.path.dirname(path)
        if os.path.exists(path):
            break
        else:
            os.mkdir(path)


def remove_folder_contents(top):
    for root, dirs, files in os.walk(top, topdown=False):
        try:
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        except:
            print(f'Unable to remove {name} in {root}')


def remove_pyc_files():
    folders_to_remove = []
    for root, folder, files in os.walk(APP_FOLDER):
        root_path_list = root.split('/')
        if root_path_list[-1] == folders_to_remove:
            folders_to_remove.append(root)
        for filename in files:
            if filename.endswith(".pyc") or root in folders_to_remove:
                path = os.path.join(root, filename)
                print(f'Removing file: {path}')
                os.remove(path)
    for folder in folders_to_remove:
        print(f'Removing folder: {folder}')
        os.rmdir(folder)


def get_storage_file_list():
    for filename in os.listdir(STORAGE_FOLDER):
        if os.path.isfile(os.path.join(STORAGE_FOLDER, filename)):
            yield filename     


def get_free_space(path):
    size = 0
    if not os.path.isdir(path):
        print("Error getting free size: not directory %s" % path)
    elif platform.system() == 'Windows':
        free_bytes = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(path), None, None, ctypes.pointer(free_bytes))
        size = free_bytes.value
    else:
        st = os.statvfs(path)
        size = st.f_bavail * st.f_frsize
    return size


def humanize_disk_size(size):
    for unit in SIZE_UNITS:
        if abs(size) < 1024.0:
            break
        size /= 1024.0
    return SIZE_OUTPUT_TEMPLATE % (size, unit)


def get_human_free_space(path):
    return humanize_disk_size(get_free_space(path))


def get_human_file_size(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        print("Error getting size for %s" % path)
        print(sys.exc_info())
        size = 0
    return humanize_disk_size(size)


def get_human_access_time(path):
    if os.path.exists(path):
        return time.asctime(time.gmtime(os.path.getatime(path)))
    else:
        print("Error getting access time: %s doesn't exists" % path)
        return ""


def cleanup_caches():
    remove_pyc_files()
    try:
        import shutil
        for folder in FOLDER_TO_CLEANUP_ON_CRASH:
            folder_path = os.path.abspath(os.path.join(APP_FOLDER, folder))
            if os.path.isdir(folder_path):
                print(f'Removing leftovers in: {folder_path}')
                shutil.rmtree(folder_path)
    except (ImportError, OSError):
        pass


def remove_downloaded_files():
    remove_folder_contents(DOWNLOAD_FOLDER)


if __name__ == "__main__":
    cleanup_caches()
