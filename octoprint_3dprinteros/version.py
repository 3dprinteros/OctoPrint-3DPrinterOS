#!/usr/bin/python
# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019.
# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)

version = "7.41.0"
branch = "octoprint"
build = "git"


def full_version_string():
    return version + branch + "_" + build

if __name__ == '__main__':
    print(version)
    print(branch)
    print(build)
