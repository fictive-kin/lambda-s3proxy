# -*- coding: utf-8 -*-

import json
import os
import logging
import random
import string

from flask import Flask, abort, request, redirect
from slugify import slugify

from application import stripe
from application.lib.gzip import GZipMiddleware
from application.lib.s3proxy import S3Proxy
from application.views import core_views, root_view


def str2bool(s):
    if s == 'False' or s == 'false' or s == 'FALSE' or s == '0':
        return False
    return bool(s)


def random_string(length=5):  # pylint: disable=no-self-use
    return ''.join(
        random.SystemRandom().choice(string.ascii_lowercase +
                                     string.ascii_uppercase +
                                     string.digits) for _ in range(length))


def create_app(name):
    app = Flask(name, static_folder=None)

    app.logger.setLevel(logging.DEBUG)

    # Intentionally unguarded. If this doesn't exist, we can't do anything.
    app.config['S3_BUCKET'] = os.environ['S3_BUCKET']
    app.config['S3_PREFIX'] = os.environ.get('S3_PREFIX', None)
    app.config['DISABLE_GZIP'] = str2bool(os.environ.get('DISABLE_GZIP', '0'))
    app.config['WWW_REDIRECTOR'] = str2bool(os.environ.get('ENABLE_ROOT_REDIRECT', '1'))
    app.config['ENABLE_TRAILING_SLASH_REDIRECT'] = str2bool(
        os.environ.get('ENABLE_TRAILING_SLASH_REDIRECT', '0'))
    app.config['DROP_TRAILING_SLASH'] = str2bool(
        os.environ.get('DROP_TRAILING_SLASH', '0'))
    app.config['ADD_CACHE_HEADERS'] = str2bool(os.environ.get('ADD_CACHE_HEADERS', '0'))


    app.s3_proxy = S3Proxy(app)
    stripe.init_app(app)

    @app.errorhandler(404)
    def page_not_found(error):
        resp = app.s3_proxy.retrieve('404/index.html', abort_on_fail=False)
        if not resp:
            resp = app.s3_proxy.retrieve('404.html', abort_on_fail=False)
        return resp, 404

    @app.errorhandler(500)
    def server_error_page(error):
        resp = app.s3_proxy.retrieve('500/index.html', abort_on_fail=False)
        if not resp:
            resp = app.s3_proxy.retrieve('500.html', abort_on_fail=False)
        return resp, 500

    root_redirect = False
    if os.environ.get('S3_REDIRECTS_FILE'):
        redirects_obj = app.s3_proxy.get_file(os.environ['S3_REDIRECTS_FILE'])
        redirects_setup = json.load(redirects_obj['Body'])
        redirects = {}

        use_301s = str2bool(os.environ.get('S3_REDIRECTS_USE_301', False))

        def handle_redirect(redirect_id):
            def redirect_func(**kwargs):
                return redirect(
                    redirects[redirect_id].format(**kwargs),
                    code=301 if use_301s else 302
                )
            return redirect_func

        for item in redirects_setup.keys():
            if item == '/':
                root_redirect = True
            item_slug = 'redirects-{}'.format(slugify(item))
            if item_slug in redirects:
                item_slug = '{}-{}'.format(item_slug, random_string(10))
            redirects.update({item_slug: redirects_setup[item]})
            app.add_url_rule(
                item,
                item_slug,
                handle_redirect(item_slug)
            )
            #if item[:-1] != '/':
            #    app.add_url_rule(
            #        '{}/'.format(item),
            #        '{}-slash'.format(item_slug),
            #        handle_redirect(item_slug)
            #    )
   
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

    if not root_redirect:
        app.register_blueprint(root_view)

    app.register_blueprint(core_views)

    return app
