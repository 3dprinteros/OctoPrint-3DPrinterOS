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

import platform

def get_platform():
    platform_info = platform.platform().lower()
    if platform_info.startswith('linux') and 'arm' in platform_info:
        return 'rpi'
    elif platform_info.startswith('linux'):
        return 'linux'
    elif platform_info.startswith('darwin'):
        return 'mac'
    elif platform_info.startswith('windows'):
        return 'win'

PLATFORM = get_platform()