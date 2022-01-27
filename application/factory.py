# -*- coding: utf-8 -*-

import json
import os
import logging
import random
import string

import botocore
from dynaconf import FlaskDynaconf
from flask import Flask, abort, request, redirect, Response
from flask_cors import CORS
from slugify import slugify

from application import stripe
from application.redirects import FlaskJSONRedirects
from application.s3proxy import FlaskS3Proxy
from application.localizer import FlaskLocalizer


def create_app(name, log_level=logging.WARN):
    app = Flask(name, static_folder=None)

    FlaskDynaconf(app)

    app.logger.setLevel(log_level)

    CORS(app, origins=app.config['ALLOWED_ORIGINS'], supports_credentials=True)
    stripe.init_app(app)

    app.s3_proxy = FlaskS3Proxy(app)
    app.redirects = FlaskJSONRedirects()

    if app.config.get('S3_REDIRECTS_FILE'):
        try:
            redirects_obj = app.s3_proxy.get_file(app.config['S3_REDIRECTS_FILE'])
            app.redirects.init_app(app, file=redirects_obj['Body'])
        except botocore.exceptions.ClientError as exc:
            if exc.response['Error']['Code'] == 'NoSuchKey':
                app.logger.critical(
                    f"S3_REDIRECTS_FILE does not exist: {app.config['S3_REDIRECTS_FILE']}")
            else:
                raise

    # Due to the redirects possibly using these routes, we are adding these after having
    # instantiated all the redirects. If not for that, we could have used a config value
    app.s3_proxy.add_handled_routes(['/', '/<path:url>'], methods=['GET', 'POST'])

    app.localizer = FlaskLocalizer(app)

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
                    raise Exception()  # This is just to prevent an true 500 from occuring
            return resp, 500
        except Exception:  # pylint: disable=broad-except
            return Response('Internal Server Error', status=500, content_type='text/plain')

    def is_allowed_origin():
        if '*' not in app.config['ALLOWED_ORIGINS']:
            origin = request.headers.get('Origin')

            if not origin:
                app.logger.debug('Origin header not provided')
                return False

            if origin not in app.config['ALLOWED_ORIGINS']:
                if '{}/'.format(origin) not in app.config['ALLOWED_ORIGINS']:
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
