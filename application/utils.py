# -*- coding: utf-8 -*-

import random
import string

from flask import Response


def str2bool(s):
    if s == 'False' or s == 'false' or s == 'FALSE' or s == '0':
        return False
    return bool(s)


def random_string(length=5):  # pylint: disable=no-self-use
    return ''.join(
        random.SystemRandom().choice(string.ascii_lowercase +
                                     string.ascii_uppercase +
                                     string.digits) for _ in range(length))


def forced_relative_redirect(url, **kwargs):
    body = f"""
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<title>Redirecting...</title>
<h1>Redirecting...</h1>
<p>You should be redirected automatically to target URL: <a href="{url}">{url}</a>.  If not click the link.
    """

    if 'code' in kwargs and 'status' not in kwargs:
        kwargs.update({'status': kwargs.pop('code')})

    if 'headers' not in kwargs:
        kwargs.update({'headers': {}})

    kwargs['headers'].update({'Location': url})
    print(f'Redirecting to: {url}')
    print(kwargs)
    resp = Response(
        body,
        **kwargs,
    )
    resp.autocorrect_location_header = False
    return resp
