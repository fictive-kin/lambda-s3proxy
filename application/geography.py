# -*- coding: utf-8 -*-

import json
from urllib.parse import unquote

import botocore
from flask import Flask, Blueprint, abort, request, jsonify
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
    'UK': Unit.MILES,
}

POINT_NEMO_HEADERS = {
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


class FlaskGeography:

    _cache = None

    def __init__(self, app: Flask = None, *, route: str = None):
        self._cache = {}
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
                        return_value = float(return_value)

                    returnable.update({
                        DESIRED_HEADERS[header.lower()]: return_value
                    })

            if not returnable:
                # When we're not running behind CloudFront, we want valid data,
                # but want to be able to tell easily that it's inaccurate.
                returnable = POINT_NEMO_HEADERS

            return returnable

        @app.route(route)
        @cross_origin(origins=['*'], methods=['GET', 'OPTIONS'])
        def geography():
            return jsonify(cf_to_normal(request.headers))

        @app.route(f'{route}/closest-to-user/<path:filename>', methods=['GET'])
        def closest_to_user(filename):
            if not app.s3_proxy:
                return abort(404)

            if filename not in self._cache:
                try:
                    data_file = app.s3_proxy.get_file(f'{filename}.json')

                except botocore.exceptions.ClientError as exc:
                    if exc.response['Error']['Code'] == 'NoSuchKey':
                        app.logger.warning(f"/{filename}.json does not exist!")
                        return abort(404)
                    else:
                        raise

                try:
                    data = {
                        'points': json.load(data_file['Body']),
                    }

                except json.JSONDecodeError as exc:
                    app.logger.error(f'/{filename}.json is not valid json')
                    app.logger.exception(exc)
                    return abort(404)

                self._cache[filename] = data.copy()

            else:
                data = self._cache[filename].copy()

            if request.args.get('unit'):
                data.update({'unit': request.args['unit']})

            return get_distances(data)

        @app.route(f'{route}/closest-to-user', methods=['POST'])
        def distances():
            return get_distances(request.json)

        def get_distances(data):
            """
            data is a dict of:

            {
                'unit': Unit.KILOMETERS,  # Optional, can be any of the supported units of Haversine
                'home': [  # Optional latitude/longitude pair. default is taken from CF headers
                    45.1234,
                    -53.1234,
                ],
                'points': {
                   # Required, list of points to compare against, a unique id + lat/long pair + any desired extra keys
                   'id1': {
                       'latitude': 45.1235,
                       'longitude': -53.1234,
                       ...other ignored keys
                   },
                   ...
                }
            }
            """

            if not HAS_HAVERSINE:
                return abort(404)

            localization = cf_to_normal(request.headers)
            if localization == POINT_NEMO_HEADERS:
                return abort(404)

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

            if isinstance(data['points'], (list, set,)):
                for point in data['points']:
                    if 'id' not in point or 'latitude' not in point or 'longitude' not in point:
                        continue

                    id_ = point['id']
                    ll = (float(point['latitude']), float(point['longitude']),)

                    distance = haversine(home, ll, unit=unit)
                    if closest is None or distance < closest['distance']:
                        closest = {
                            'id': id_,
                            'distance': distance,
                        }

                    points.update({id_: point})
                    points[id_].update({'distance': distance})

            else:
                for id_,ll in data['points'].items():
                    distance = haversine(home, ll, unit=unit)
                    if closest is None or distance < closest['distance']:
                        closest = {
                            'id': id_,
                            'distance': distance,
                        }

                    points.update({id_: point})
                    points[id_].update({'distance': distance})

            return jsonify({
                'unit': unit,
                'closest': closest,
                'points': points,
            })
