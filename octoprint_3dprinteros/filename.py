# Copyright 3D Control Systems, Inc. All Rights Reserved 2017-2019.
# Built in San Francisco.

# This software is distributed under a commercial license for personal,
# educational, corporate or any other use.
# The software as a whole or any parts of it is prohibited for distribution or
# use without obtaining a license from 3D Control Systems, Inc.

# All software licenses are subject to the 3DPrinterOS terms of use
# (available at https://www.3dprinteros.com/terms-and-conditions/),
# and privacy policy (available at https://www.3dprinteros.com/privacy-policy/)

import string
import os

def get_filename_ascii(filename, exclude_chars=''):
    filename = os.path.splitext(os.path.basename(filename))[0]
    output = ''
    available_chars = string.ascii_letters + string.digits + '!"#$%&\'()*+,-.:;<=>?@[]^_`{|}~'
    for c in exclude_chars:
        available_chars = available_chars.replace(c, '')
    for c in filename:
        if c in available_chars:
            output += c
        elif c in '\\/':
            output += '|'
        else:
            output += '_'
    return output
