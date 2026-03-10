# -*- coding: utf-8 -*-

import typing as t
import json
import random
import string
from urllib.parse import quote_plus

import botocore.exceptions
from flask import Response, abort, current_app


def str2json(s: t.Any) -> t.Any:
    if not isinstance(s, str):
        return s

    try:
        s = json.loads(s)
    except json.JSONDecodeError as exc:
        current_app.logger.exception(exc)

    return s


def str2bool(s: t.Any) -> bool:
    if isinstance(s, str) and (s.lower() == "false" or s == "0"):
        return False
    return bool(s)


def random_string(length=5):  # pylint: disable=no-self-use
    return "".join(
        random.SystemRandom().choice(
            string.ascii_lowercase + string.ascii_uppercase + string.digits
        )
        for _ in range(length)
    )


def forced_host_redirect(url, **kwargs):
    domain_name = current_app.config.get("DOMAIN_NAME")
    if not url.startswith("http") and domain_name is not None:
        url = f"https://{domain_name}{url}"

    return _redirect(url, **kwargs)


def forced_relative_redirect(url, **kwargs):
    resp = _redirect(url, **kwargs)
    resp.autocorrect_location_header = False
    return resp


def _redirect(url, **kwargs):
    url = quote_plus(url, safe="/:?=&#")
    body = f"""
<!doctype html>
<html>
<head>
<title>Redirecting...</title>
<meta http-equiv="Refresh" content="1;url={url}" />
</head>
<body>
<h1>Redirecting...</h1>
<p>You should be redirected automatically to target URL: <a href="{url}">{url}</a>.  If not click the link.</p>
</body>
</html>
    """

    if "code" in kwargs and "status" not in kwargs:
        kwargs.update({"status": kwargs.pop("code")})

    if "headers" not in kwargs:
        kwargs.update({"headers": {}})

    kwargs["headers"].update(
        {
            "Location": url,
            "Cache-Control": "no-store, no-cache, private, max-age=0",
        }
    )
    resp = Response(
        body,
        **kwargs,
    )
    if resp.headers.get("Location", "").startswith("//"):
        resp.headers["Location"] = resp.headers["Location"][1:]

    return resp


def force_404():
    return abort(404)


def init_extension(app, extension, filename_key):
    # Specifically not passing app to the initial init, since we'll don't want to double run it
    ext = extension()
    config_obj = None
    if app.config.get(filename_key):
        try:
            config_obj = app.extensions["s3_proxy"].get_file(app.config[filename_key])
        except botocore.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                app.logger.warning(
                    f"{filename_key} does not exist: {app.config[filename_key]}"
                )
            else:
                raise

        # We don't want to let the config file get viewed as it's a special file
        app.add_url_rule(
            f"/{app.config[filename_key]}", f"{filename_key}-file-block", force_404
        )

    ext.init_app(app, file=config_obj["Body"] if config_obj else None)

    return ext
