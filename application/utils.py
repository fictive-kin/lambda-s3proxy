# -*- coding: utf-8 -*-

import random
import string

import botocore
from flask import Response, abort, current_app


def str2bool(s):
    if s == 'False' or s == 'false' or s == 'FALSE' or s == '0':
        return False
    return bool(s)


def random_string(length=5):  # pylint: disable=no-self-use
    return ''.join(
        random.SystemRandom().choice(string.ascii_lowercase +
                                     string.ascii_uppercase +
                                     string.digits) for _ in range(length))


def forced_host_redirect(url, **kwargs):
    if not url.startswith('http') and current_app.config.DOMAIN_NAME:
        url = f'https://{current_app.config.DOMAIN_NAME}{url}'

    return _redirect(url, **kwargs)


def forced_relative_redirect(url, **kwargs):
    resp = _redirect(url, **kwargs)
    resp.autocorrect_location_header = False
    return resp


def _redirect(url, **kwargs):
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
    resp = Response(
        body,
        **kwargs,
    )
    return resp


def force_404():
    return abort(404)


def init_extension(app, extension, filename_key):
    # Specifically not passing app to the initial init, since we'll don't want to double run it
    ext = extension()
    if app.config.get(filename_key):
        try:
            config_obj = app.s3_proxy.get_file(app.config[filename_key])
            ext.init_app(app, file=config_obj['Body'])
        except botocore.exceptions.ClientError as exc:
            if exc.response['Error']['Code'] == 'NoSuchKey':
                app.logger.warning(
                    f"{filename_key} does not exist: {app.config[filename_key]}")
            else:
                raise

        # We don't want to let the config file get viewed as it's a special file
        app.add_url_rule(f"/{app.config[filename_key]}", f'{filename_key}-file-block', force_404)

    return ext
