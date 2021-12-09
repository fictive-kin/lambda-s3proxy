# -*- coding: utf-8 -*-

import json

from flask import Blueprint, current_app, request, redirect, jsonify
from flask_cors import cross_origin

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
