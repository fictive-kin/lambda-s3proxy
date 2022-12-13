# -*- coding: utf-8 -*-

import json
import os
import logging
import random
import re
import string

import botocore
from dynaconf import FlaskDynaconf
from flask import Flask, abort, request, redirect, Response, jsonify
from flask_cors import CORS
from flask_cors.core import probably_regex, try_match_any
from flask_csp import CSP
from slugify import slugify
from sentry_sdk.integrations.flask import FlaskIntegration

from application import stripe
from application.exceptions import setup_sentry
from application.eleventy import Flask11tyServerless, LambdaMessageEncoder
from application.authorizer import FlaskJSONAuthorizer
from application.redirects import FlaskJSONRedirects
from application.s3proxy import FlaskS3Proxy
from application.localizer import FlaskLocalizer


def origins_list_to_regex(origins):
    logging.info(f'Original origins list: {origins}')
    if not isinstance(origins, (list, set, tuple,)):
        if isinstance(origins, str) and origins.startswith('[') and origins.endswith(']'):
            origins = json.loads(origins)
        else:
            origins = [origins]

    regex_list = []
    for string in origins:
        if not string.startswith('http://') and not string.startswith('https://'):
            string = f'https://{string}'

        if probably_regex(string):
            regex_list.append(re.compile(rf'{string}'))
        else:
            regex_list.append(string)

    return regex_list


def create_app(name, log_level=logging.WARN):
    app = Flask(name, static_folder=None)

    FlaskDynaconf(app)

    app.logger.setLevel(log_level)
    app.logger.propagate = True

    if app.config["ENV"].lower() not in ("development", "testing"):
        setup_sentry(
            app.config.get("SENTRY_DSN"),
            debug=app.debug,
            integrations=[
                FlaskIntegration(),
            ],
            environment=app.config["ENV"],
            request_bodies="always",
        )

    stripe.init_app(app)

    app.allowed_origins = origins_list_to_regex(app.config.get('ALLOWED_ORIGINS', ['.*']))
    CORS(app, origins=app.allowed_origins, supports_credentials=True)
    CSP(app)

    app.s3_proxy = FlaskS3Proxy(app)
    app.redirects = FlaskJSONRedirects()

    def force_404():
        raise abort(404)

    if app.config.get('S3_REDIRECTS_FILE'):
        try:
            redirects_obj = app.s3_proxy.get_file(app.config['S3_REDIRECTS_FILE'])
            app.redirects.init_app(app, file=redirects_obj['Body'])
        except botocore.exceptions.ClientError as exc:
            if exc.response['Error']['Code'] == 'NoSuchKey':
                app.logger.warning(
                    f"S3_REDIRECTS_FILE does not exist: {app.config['S3_REDIRECTS_FILE']}")
            else:
                raise

        # We don't want to let the redirects file get viewed as it's a special file
        app.add_url_rule(f"/{app.config['S3_REDIRECTS_FILE']}", 'redirects-file-block', force_404)

    app.eleventy = Flask11tyServerless()

    if app.config.get('S3_ELEVENTY_FILE'):
        try:
            eleventy_obj = app.s3_proxy.get_file(app.config['S3_ELEVENTY_FILE'])
            app.eleventy.init_app(app, file=eleventy_obj['Body'])
        except botocore.exceptions.ClientError as exc:
            if exc.response['Error']['Code'] == 'NoSuchKey':
                app.logger.warning(
                    f"S3_ELEVENTY_FILE does not exist: {app.config['S3_ELEVENTY_FILE']}")
            else:
                raise

        # We don't want to let the 11ty file get viewed as it's a special file
        app.add_url_rule(f"/{app.config['S3_ELEVENTY_FILE']}", 'eleventy-file-block', force_404)

    app.authorizer = FlaskJSONAuthorizer(app)

    if app.config.get('S3_AUTHORIZER_FILE'):
        try:
            authorizer_obj = app.s3_proxy.get_file(app.config['S3_AUTHORIZER_FILE'])
            app.authorizer.init_app(app, file=authorizer_obj['Body'])
        except botocore.exceptions.ClientError as exc:
            if exc.response['Error']['Code'] == 'NoSuchKey':
                app.logger.warning(
                    f"S3_AUTHORIZER_FILE does not exist: {app.config['S3_AUTHORIZER_FILE']}")
            else:
                raise

        # We don't want to let the authorizations file get viewed as it's a special file
        app.add_url_rule(f"/{app.config['S3_AUTHORIZER_FILE']}", 'authorizer-file-block', force_404)

    app.localizer = FlaskLocalizer(app)

    # Due to the redirects possibly using these routes, we are adding these after having
    # instantiated all the redirects. If not for that, we could have used a config value
    app.s3_proxy.add_handled_routes(['/', '/<path:url>'], methods=['GET', 'POST'])

    @app.errorhandler(404)
    def page_not_found(error):
        try:
            resp = app.s3_proxy.retrieve('404/index.html', abort_on_fail=False)
            if not resp:
                resp = app.s3_proxy.retrieve('404.html', abort_on_fail=False)
                if not resp:
                    raise Exception()  # This is just to prevent a 500 from occuring
            return resp, 404
        except Exception:  # pylint: disable=broad-except
            return Response('Page Not Found', status=404, content_type='text/plain')

    @app.errorhandler(500)
    def server_error_page(error):
        try:
            resp = app.s3_proxy.retrieve('500/index.html', abort_on_fail=False)
            if not resp:
                resp = app.s3_proxy.retrieve('500.html', abort_on_fail=False)
                if not resp:
                    raise Exception()  # This is just to prevent a true 500 from occuring
            return resp, 500
        except Exception:  # pylint: disable=broad-except
            return Response('Internal Server Error', status=500, content_type='text/plain')

    def is_allowed_origin():
        if app.allowed_origins:
            origin = request.headers.get('Origin')

            if not origin:
                app.logger.debug('Origin header not provided')
                return False

            if (
                    not try_match_any(origin, app.allowed_origins) and
                    not try_match_any(f'{origin}/', app.allowed_origins)
            ):
                app.logger.debug('Origin header not in allowed list: {}'.format(origin))
                return False

        return True

    @app.before_request
    def chk_shortcircuit():
        if request.method == 'OPTION' and app.config['SHORTCIRCUIT_OPTIONS']:
            app.logger.debug('Shortcircuiting OPTIONS request')
            if is_allowed_origin():
                return '', 200
            return abort(403)

        # If we get here, we're neither shortcircuiting OPTIONS requests, let the view
        # deal with it directly.
        return None

    if app.config['ADD_CACHE_HEADERS']:

        @app.after_request
        def add_cache_headers(response):
            if response.status_code != 200:
                return response

            content_type = response.headers.get('Content-Type')
            if 'text' in content_type or 'application' in content_type:
                return response

            if not response.headers.get('Cache-Control'):
                response.headers['Cache-Control'] = 'public,max-age=2592000,s-maxage=2592000,immutable'
            if not response.headers.get('Vary'):
                response.headers['Vary'] = 'Accept-Encoding,Origin,Access-Control-Request-Headers,Access-Control-Request-Method'

            return response

    return app
