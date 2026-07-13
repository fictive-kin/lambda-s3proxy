from datetime import datetime
import json
import logging
import re
import time

from dynaconf import FlaskDynaconf
from flask import Flask, abort, request
from flask_cors import CORS
from flask_cors.core import probably_regex, try_match_any_pattern
from flask_csp import CSP
from sentry_sdk.integrations.flask import FlaskIntegration

from application.exceptions import setup_sentry
from application.extensions import (
    Flask11tyServerless,
    # FlaskAPIGatewayOverflowExtension,
    FlaskEncryptedSession,
    FlaskFormToEmail,
    FlaskGeography,
    FlaskGradualSwitchoverProxy,
    FlaskJSONAuthorizer,
    FlaskJSONRedirects,
    FlaskS3Proxy,
    stripe,
)
from application.utils import forced_host_redirect, init_extension


def origins_list_to_regex(app, origins):
    logging.info(f"Original origins list: {origins}")
    if not isinstance(
        origins,
        (
            list,
            set,
            tuple,
        ),
    ):
        if (
            isinstance(origins, str)
            and origins.startswith("[")
            and origins.endswith("]")
        ):
            try:
                origins = json.loads(origins)
            except json.JSONDecodeError as exc:
                app.logger.exception(exc)
                origins = [".*"]
        else:
            origins = [origins]

    regex_list = []
    for string in origins:
        if not string.startswith("http://") and not string.startswith("https://"):
            string = f"https://{string}"

        if probably_regex(string):
            regex_list.append(re.compile(rf"{string}"))
        else:
            regex_list.append(string)

    return regex_list


def create_app(name, log_level=logging.WARN):

    tries = 0
    app = None
    while app is None:
        tries += 1
        try:
            app = _create_app(name, log_level)
        except Exception as exc:
            logging.exception(exc)

            if tries >= 5:
                logging.critical(
                    "Number of allowed app instantiation retries has been exceeded."
                )
                raise exc

            app = None
            # wait 2 secs before retrying in case it was a transient network error
            time.sleep(2)

    return app


def _create_app(name, log_level=logging.WARN):
    app = Flask(name, static_folder=None)
    app.url_map.strict_slashes = False

    FlaskDynaconf(app)

    app.logger.setLevel(log_level)
    app.logger.propagate = True

    if app.config["ENV_FOR_DYNACONF"].lower() not in ("development", "testing"):
        setup_sentry(
            app.config.get("SENTRY_DSN"),
            debug=app.debug,
            integrations=[
                FlaskIntegration(),
            ],
            environment=app.config["ENV_FOR_DYNACONF"],
            # request_bodies="always",
        )

    logging.getLogger("boto3").setLevel(
        app.config.get("BOTO3_LOG_LEVEL", logging.CRITICAL)
    )
    logging.getLogger("botocore").setLevel(
        app.config.get("BOTOCORE_LOG_LEVEL", logging.CRITICAL)
    )
    logging.getLogger("sentry").setLevel(
        app.config.get("SENTRY_LOG_LEVEL", logging.CRITICAL)
    )

    app.extensions["session"] = FlaskEncryptedSession(app)

    if app.config.get("STRIPE_ENABLED", False):
        stripe.init_app(app)

    # Keep a direct reference to the compiled origin patterns. Older flask-cors
    # exposed these via `CORS(...).options["origins"]`, but flask-cors 6.x renamed
    # that to a private `_options`, so we no longer reach into the extension's
    # internals from `is_allowed_origin()` below.
    allowed_origins = origins_list_to_regex(
        app,
        app.config.get(
            "ALLOWED_ORIGINS",
            [".*"],
        ),
    )
    app.extensions["cors"] = CORS(
        app,
        origins=allowed_origins,
        supports_credentials=True,
    )
    app.extensions["csp"] = CSP(app)
    # app.extensions["overflow"] = FlaskAPIGatewayOverflowExtension(app=app)

    switchover_percentage = int(app.config.get("SWITCHOVER_PERCENTAGE_ON_NEW", 100))
    if (
        switchover_percentage < 100
        and app.config.get("SWITCHOVER_DOMAIN")
        and app.config.get("SWITCHOVER_IPS")
    ):
        app.extensions["switchover_proxy"] = FlaskGradualSwitchoverProxy(
            app.config.get("SWITCHOVER_DOMAIN", ""),
            app.config.get("SWITCHOVER_IPS", []),
            percentage_on_new=switchover_percentage,
            bots_on_new=app.config.get("SWITCHOVER_BOTS_ON_NEW"),
        )

    app.extensions["s3_proxy"] = FlaskS3Proxy(
        app, switchover_proxy=app.extensions.get("switchover_proxy")
    )
    app.extensions["geography"] = FlaskGeography(app)

    app.extensions["authorizer"] = init_extension(
        app, FlaskJSONAuthorizer, "S3_AUTHORIZER_FILE"
    )
    app.extensions["eleventy"] = init_extension(
        app, Flask11tyServerless, "S3_ELEVENTY_FILE"
    )
    app.extensions["redirects"] = init_extension(
        app, FlaskJSONRedirects, "S3_REDIRECTS_FILE"
    )

    if app.config.get("MAIL_SERVER"):
        app.extensions["mail"] = init_extension(
            app, FlaskFormToEmail, "S3_FORM2EMAIL_FILE"
        )
        if app.debug:
            app.extensions["mail"].add_test(
                "/form2email", recipient="jared@fictivekin.com"
            )

    # Due to the redirects possibly using these routes, we are adding these after having
    # instantiated all the redirects. If not for that, we could have used a config value
    app.extensions["s3_proxy"].add_handled_routes(
        ["/", "/<path:url>"], methods=["GET", "POST"]
    )
    app.extensions["s3_proxy"].setup_locales(
        file=app.config.get("S3_LOCALES_FILE", None),
        enable_auto_switch=["/"],
    )

    def compile_re_paths(key):

        value = app.config.get(key, [])
        app.logger.debug(f"{key}: {value}")
        paths = []
        if not isinstance(value, list):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                app.logger.exception(exc)
                value = []

        for path in value:
            paths.append(re.compile(rf"{path}"))

        app.logger.debug(f"  -> changing to: {paths}")
        app.config[key] = paths

    compile_re_paths("PATHS_TO_LEAVE_TRAILING_SLASH")
    compile_re_paths("PATTERNS_TO_404")

    def is_allowed_origin():
        if allowed_origins:
            origin = request.headers.get("Origin")

            if not origin:
                app.logger.debug("Origin header not provided")
                return False

            if not try_match_any_pattern(
                origin, allowed_origins, caseSensitive=False
            ) and not try_match_any_pattern(
                f"{origin}/",
                allowed_origins,
                caseSensitive=False,
            ):
                app.logger.debug("Origin header not in allowed list: {}".format(origin))
                return False

        return True

    @app.before_request
    def block_config_patterns():
        rp = request.path

        for path in app.config.get("PATTERNS_TO_404", []):
            if path.search(rp):
                app.logger.debug(f"Forcing a 404 for {rp}")
                return abort(404)

    @app.before_request
    def clear_trailing():
        if not app.config.get("TRAILING_SLASH_REDIRECTION", True):
            return

        rp = request.path
        rq = request.query_string.decode("utf-8") if request.query_string else None

        for path in app.config.get("PATHS_TO_LEAVE_TRAILING_SLASH", []):
            if path.search(rp):
                return

        if rp != "/" and rp.endswith("/"):
            return forced_host_redirect(
                rp[:-1] + (f"?{rq}" if rq else ""),
                code=app.config.get("REDIRECTS_DEFAULT_STATUS_CODE", 302),
            )

    @app.before_request
    def chk_shortcircuit():
        if request.method == "OPTIONS" and app.config["SHORTCIRCUIT_OPTIONS"]:
            app.logger.debug("Shortcircuiting OPTIONS request")
            if is_allowed_origin():
                return "", 200
            return abort(403)

        # If we get here, we're neither shortcircuiting OPTIONS requests, let the view
        # deal with it directly.
        return None

    if app.config["ADD_CACHE_HEADERS"]:

        @app.after_request
        def add_cache_headers(response):
            if response.status_code != 200:
                return response

            content_type = response.headers.get("Content-Type")

            try:
                if (
                    hasattr(response, "is_long_cacheable")
                    and response.is_long_cacheable
                ):
                    if not response.headers.get("Cache-Control"):
                        response.headers["Cache-Control"] = (
                            "public,max-age=2592000,s-maxage=2592000,immutable"
                        )
                    if not response.headers.get("Vary"):
                        response.headers["Vary"] = (
                            "Accept-Encoding,Origin,Access-Control-Request-Headers,Access-Control-Request-Method"
                        )
            except AttributeError as exc:
                if app.debug:
                    app.logger.exception(exc)

            return response

    app.logger.info(app.url_map)

    @app.template_filter()
    def ts_to_iso(timestamp):
        return datetime.fromtimestamp(int(timestamp)).isoformat()

    return app
