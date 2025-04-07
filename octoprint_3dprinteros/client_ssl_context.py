import sys

try:
    if 'slave_app' in sys.modules: # for stupid envs and quirky moved like octoplugin we have to disable this
        raise ImportError()
    import certifi
    import ssl
except ImportError:
    ssl = None

import config

VERIFY = True

try:
    if ssl:
        SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
        if config.get_settings()['protocol'].get('no_cert_verify'):
            SSL_CONTEXT = ssl._create_unverified_context()
            VERIFY = False
    else:
        SSL_CONTEXT = None
except:
    SSL_CONTEXT = None
