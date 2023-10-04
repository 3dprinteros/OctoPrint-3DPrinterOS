try:
    import certifi
    import ssl
except ImportError:
    ssl = None

try:
    if ssl:
        SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    else:
        SSL_CONTEXT = None
except:
    SSL_CONTEXT = None
