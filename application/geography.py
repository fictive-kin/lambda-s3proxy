# -*- coding: utf-8 -*-

from urllib.parse import unquote

from flask import Flask, Blueprint, request, jsonify
from flask_cors import cross_origin

try:
    from haversine import haversine, Unit
    HAS_HAVERSINE = True
except ImportError:
    HAS_HAVERSINE = False


GEOGRAPHY_OPTIONS = [
    "ROUTE",
]

DESIRED_HEADERS = {
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

AS_FLOAT = [
    'cloudfront-viewer-latitude',
    'cloudfront-viewer-longitude',
]

REGION_UNIT = {
    'US': Unit.MILES,
}


class FlaskGeography:

    def __init__(self, app: Flask = None, *, route: str = None):
        if app:
            self.init_app(app, route=route)

    def init_app(self, app: Flask, *, route: str = None):
        self.app = app

        if route is None:
            if app.config.get('GEOGRAPHY_ROUTE'):
                route = app.config['GEOGRAPHY_ROUTE']
            else:
                self.app.logger.warning('Cannot instantiate FlaskGeography without a route defined')
                return

        def cf_to_normal(headers):
            returnable = {}

            for header, value in request.headers:
                if header.lower() in DESIRED_HEADERS:
                    try:
                        return_value = unquote(value)

                    except TypeError as exc:
                        self.app.logger.exception(exc)
                        return_value = value

                    if header.lower() in AS_FLOAT:
                        value = float(value)

                    returnable.update({
                        DESIRED_HEADERS[header.lower()]: return_value
                    })

            if not returnable:
                # When we're not running behind CloudFront, we want valid data,
                # but want to be able to tell easily that it's inaccurate.
                returnable = {
                    'country_code': 'N/A',
                    'city': 'Point Nemo',
                    'country_name': 'N/A',
                    'region_code': 'N/A',
                    'region_name': 'Pacific Ocean',
                    'latitude': -48.8767,
                    'longitude': -123.3933,
                    'metro-code': 'N/A',
                    'postal_code': 'N/A',
                    'timezone': 'Etc/GMT-9',
                }

            return returnable

        @app.route(route)
        @cross_origin(origins=['*'], methods=['GET', 'OPTIONS'])
        def geography():
            return jsonify(cf_to_normal(request.headers))

        @app.route(f'{route}/distances', methods=['POST'])
        def distances():
            if not HAS_HAVERSINE:
                return abort(404)

            localization = cf_to_normal(request.headers)
            data = request.json

            if not data.get('points'):
                raise abort(400)

            unit = data.get('unit', REGION_UNIT.get(localization['country_code'], Unit.KILOMETERS))
            if not isinstance(unit, Unit):
                try:
                    unit = Unit[unit.upper()]
                except IndexError:
                    raise abort(400)

            home = data.get('home', (localization['latitude'], localization['longitude'],))
            closest = None
            points = {}

            for id_,ll in data['points'].items():
                distance = haversine(home, ll, unit=unit)
                if closest is None or distance < closest['distance']:
                    closest = {
                        'id': id_,
                        'distance': distance,
                    }

                points.update({id_: distance})

            return jsonify({
                'unit': unit,
                'closest': closest,
                'points': points,
            })
