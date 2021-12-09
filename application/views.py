# -*- coding: utf-8 -*-

import json
import os

from flask import Blueprint, current_app, request, redirect, jsonify
from flask_cors import cross_origin

from application.lib.json import ExtendedEncoder


core_views = Blueprint('core', __name__)
root_view = Blueprint('root', __name__)

global_redirect_url = os.environ.get('GLOBAL_REDIRECT_URL', False)
inject_meta_refresh = os.environ.get('INJECT_META_REFRESH', False)

@root_view.route('/', methods=['GET', 'POST'])
@core_views.route('/<path:url>', methods=['GET', 'POST'])
def proxy_it(url=None):

    original_url = str(url)
    if url is None:
        url = 'index.html'
    elif isinstance(url, str) and url.endswith('/'):
        if current_app.config.get('DROP_TRAILING_SLASH'):
            redirect_target = '/{}'.format(url[:-1])
            if request.query_string:
                redirect_target = '?'.join((redirect_target, request.query_string.decode('utf-8')))
            return redirect(redirect_target, 302)
        else:
            url = '{}index.html'.format(url)

    response = current_app.s3_proxy.retrieve(url, abort_on_fail=False)
    if not response or response.status_code == 404:
        response = current_app.s3_proxy.retrieve('{}/index.html'.format(original_url))
        if not response or response.status_code == 404:
            return abort(404)
        elif response.status_code == 200 and current_app.config.get('ENABLE_TRAILING_SLASH_REDIRECT'):
            # Requires starting slash in the redirect, because the url var
            # is missing it
            return redirect('/{}/'.format(original_url))

    if global_redirect_url:
        return redirect(global_redirect_url)

    if inject_meta_refresh:
        if response.headers.get('Content-Type') == 'text/html':
            response.set_data('<html><head><meta http-equiv="refresh" content="{}" /></head></html>'.format(inject_meta_refresh))

    return response


@core_views.route('/localizer')
@cross_origin(origins=['*'], methods=['GET', 'OPTIONS'])
def localizer():
    desired_headers = {
        'cloudfront-viewer-country': 'country_code',
        'cloudfront-viewer-city': 'city',
        'cloudfront-viewer-country-name': 'country_name',
        'cloudfront-viewer-country-region': 'region_code',
        'cloudfront-viewer-country-region-name': 'region_name',
        'cloudfront-viewer-latitude': 'latitude',
        'cloudfront-viewer-longitude': 'longitude',
        'cloudfront-viewer-metro-code': 'metro_code',
        'cloudfront-viewer-postal-code': 'postal_code',
        'cloudfront-viewer-time-zone': 'timezone'
    }

    returnable = {}

    for header, value in request.headers:
        if header.lower() in desired_headers:
            returnable.update({desired_headers[header.lower()]: value})

    return jsonify(returnable)
