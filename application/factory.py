# -*- coding: utf-8 -*-

import os

from flask import Flask, request
from flask_cors import CORS

from application.lib.gzip import GZipMiddleware
from application.lib.s3proxy import S3Proxy
from application.views import core_views


def create_app(name):
    app = Flask(name, static_folder=None)
    # Intentionally unguarded. If this doesn't exist, we can't do anything.
    app.config['S3_BUCKET'] = os.environ['S3_BUCKET']
    app.config['S3_PREFIX'] = os.environ.get('S3_PREFIX', None)

    app.config.CORS_ALWAYS_SEND = True
    CORS(app, origins=['*'])

    app.s3_proxy = S3Proxy(app)

    app.register_blueprint(core_views)

    @app.errorhandler(404)
    def page_not_found(error):
        return app.s3_proxy.retrieve('404/index.html'), 404

    @app.errorhandler(500)
    def server_error_page(error):
        return app.s3_proxy.retrieve('500/index.html'), 500

    return app
