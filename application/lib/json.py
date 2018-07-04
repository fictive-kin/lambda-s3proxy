# -*- coding: utf-8 -*-

from datetime import datetime, timedelta
import enum
import json
import pytz
from pip._vendor.distlib.version import Version
import uuid

class ExtendedEncoder(json.JSONEncoder):
    """Encoder that supports various additional types that we care about."""

    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.replace(tzinfo=pytz.UTC).isoformat('T')
        if isinstance(obj, timedelta):
            return str(obj)
        if isinstance(obj, Version):
            return str(obj)
        if isinstance(obj, uuid.UUID):
            return unicode(obj)
        if isinstance(obj, enum.Enum):
            return obj.value
        if isinstance(obj, set):
            return list(obj)
        if isinstance(obj, Exception):
            return unicode(obj)

        return json.JSONEncoder.default(self, obj)
