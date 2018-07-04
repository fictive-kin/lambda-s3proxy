# -*- coding: utf-8 -*-

from flask import Blueprint, current_app, request, redirect
import json

from application.lib.json import ExtendedEncoder

core_views = Blueprint('core', __name__)

@core_views.route('/', defaults={'url': None}, methods=['GET', 'POST'])
@core_views.route('/<path:url>', methods=['GET', 'POST'])
def proxy_it(url):
    if request.host.startswith('www.'):
        return redirect('{}://{}{}'.format(request.scheme, request.host.replace('www.', ''), request.full_path), 302)

    if url is None:
        url = 'index.html'
    elif isinstance(url, str) and url.endswith('/'):
        url = '{}index.html'.format(url)

    return current_app.s3_proxy.retrieve(url)
