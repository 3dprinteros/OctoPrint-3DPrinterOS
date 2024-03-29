# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019.
# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)

import platform


def get_platform():
    platform_name = platform.system().lower()
    platform_details = platform.platform()
    if platform_name.startswith('linux') and ('arm' in platform_details or 'aarch64' in platform_details):
        return 'rpi'
    elif platform_name.startswith('linux'):
        return 'linux'
    elif platform_name.startswith('darwin') or platform_name.startswith('macos'):
        return 'mac'
    elif platform_name.startswith('windows'):
        return 'win'


PLATFORM = get_platform()


if __name__ == "__main__":
    print(PLATFORM)
