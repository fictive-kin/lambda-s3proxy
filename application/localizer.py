# -*- coding: utf-8 -*-

from flask import Flask, Blueprint, request, jsonify
from flask_cors import cross_origin


LOCALIZER_OPTIONS = [
    "ROUTE",
]


class FlaskLocalizer:

    def __init__(self, app: Flask = None, *, route: str = None):
        if app:
            self.init_app(app, route=route)

    def init_app(self, app: Flask, *, route: str = None):
        self.app = app

        if route is None:
            if app.config.get('LOCALIZER_ROUTE'):
                route = app.config['LOCALIZER_ROUTE']
            else:
                self.app.logger.warning('Cannot instantiate FlaskLocalizer without a route defined')
                return

        @self.app.route(route)
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
