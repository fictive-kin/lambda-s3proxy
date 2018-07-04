# -*- coding: utf-8 -*-

import logging
import os

def setup_logging(name, level=None):
    logs = logging.getLogger(name)
    stream_handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s %(name)s %(levelname)s %(message)s')
    stream_handler.setFormatter(formatter)

    if level is not None:
        stream_handler.setLevel(level)
        logs.setLevel(level)
    if os.environ.get('DEBUG', None):
        stream_handler.setLevel(logging.DEBUG)
        logs.setLevel(logging.DEBUG)
    else:
        stream_handler.setLevel(logging.WARN)
        logs.setLevel(logging.WARN)

    logs.addHandler(stream_handler)
    return logs


logger = setup_logging(__name__)
