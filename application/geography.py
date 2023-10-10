# -*- coding: utf-8 -*-

import json
import typing
from urllib.parse import unquote

import botocore
from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    jsonify,
    request,
)
from flask_cors import cross_origin

try:
    from haversine import haversine, Unit
    HAS_HAVERSINE = True
except ImportError:
    HAS_HAVERSINE = False

from .utils import str2bool


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

TESTING_HEADERS = {
    "city": "Saint-Eugene",
    "country_code": "CA",
    "country_name": "Canada",
    "latitude": 45.8103,
    "longitude": -72.6982,
    "postal_code": "J0C",
    "region_code": "QC",
    "region_name": "Quebec",
    "timezone": "America/Toronto",
}


class FlaskGeography:

    _use_country_code_comparison: bool = None
    _route: str = None
    _cache: typing.Dict = None

    def __init__(self, app: Flask = None, *, route: str = None):
        self._cache = {}
        if app:
            self.init_app(app, route=route)

    @property
    def route(self):
        if self._route is not None:
            return self._route

        self._route = self.app.config.get('GEOGRAPHY_ROUTE')
        return self._route

    @route.setter
    def route(self, value):
        self._route = value

    @property
    def use_country_code_comparison(self):
        if self._use_country_code_comparison is not None:
            return self._use_country_code_comparison

        self._use_country_code_comparison = str2bool(self.app.config.get('GEOGRAPHY_USE_COUNTRY_CODE_COMPARISON', True))
        return self._use_country_code_comparison

    @use_country_code_comparison.setter
    def use_country_code_comparison(self, value):
        self._use_country_code_comparison = bool(value)

    def init_app(self, app: Flask, *, route: str = None):
        self.app = app

        if route is not None:
            self.route = route

        if not self.route:
            self.app.logger.warning('Cannot instantiate FlaskGeography without a route defined')
            return

        def init_response():
            arg_use_country_code = request.args.get('limit_by_country')
            if arg_use_country_code:  # Truthy because of strings, regardless of value
                use_country_code = arg_use_country_code not in ['false', '0', '']
            else:
                use_country_code = self.use_country_code_comparison
            return FlaskGeographyResponse(country_code_comparison=use_country_code)

        @app.route(self.route)
        @cross_origin(origins=['*'], methods=['GET', 'OPTIONS'])
        def geography():
            return init_response().basic()

        @app.route(f'{self.route}/closest-to-user/<path:filename>', methods=['GET'])
        def closest_to_user(filename):
            if not app.s3_proxy:
                return abort(404)

            force_refresh = bool(request.args.get('force_refresh', None))

            if filename not in self._cache or force_refresh:
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

            return init_response().closest_to_user(data)

        @app.route(f'{self.route}/closest-to-user', methods=['POST'])
        def distances():
            return init_response().closest_to_user(request.json)


class FlaskGeographyResponse:

    cf_headers = None
    closest = None
    points = None
    home = None
    country_code_comparison = None

    def __init__(self, *, country_code_comparison=True):
        self.cf_headers = {}

        for header, value in request.headers:
            if header.lower() in DESIRED_HEADERS:
                try:
                    value = unquote(value)

                except TypeError as exc:
                    self.app.logger.exception(exc)

                if header.lower() in AS_FLOAT:
                    value = float(value)

                self.cf_headers.update({
                    DESIRED_HEADERS[header.lower()]: value
                })

        if not self.cf_headers:
            # When we're not running behind CloudFront, we want valid data,
            # but want to be able to tell easily that it's inaccurate.
            self.cf_headers = TESTING_HEADERS if current_app.debug else POINT_NEMO_HEADERS

        self.closest = {}
        self.points = {}
        self.home = {}
        self.unit = REGION_UNIT.get(self.cf_headers['country_code'], Unit.KILOMETERS)
        self.country_code_comparison = bool(country_code_comparison)

    def basic(self):
        return jsonify(self.cf_headers)

    def closest_to_user(self, data):
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

        if self.cf_headers == POINT_NEMO_HEADERS:
            return abort(404)

        if not data.get('points'):
            raise abort(400)

        if data.get('unit'):
            self.unit = data.get('unit')
            if not isinstance(unit, Unit):
                try:
                    self.unit = Unit[self.unit.upper()]
                except IndexError:
                    raise abort(400)

        self.home = data.get('home', (self.cf_headers['latitude'], self.cf_headers['longitude'],))
        self.closest = None
        self.points = {}

        if isinstance(data['points'], (list, set,)):
            for point in data['points']:
                if 'id' not in point or 'latitude' not in point or 'longitude' not in point:
                    continue

                id_ = point['id']
                ll = (float(point['latitude']), float(point['longitude']),)

                self.points.update({id_: point})
                self.calculate_distance(id_, ll, point.get('country_code', None))

        else:
            for id_,ll in data['points'].items():
                self.points.update({id_: {
                    'latitude': ll[0],
                    'longitude': ll[1],
                }})
                self.calculate_distance(id_, ll)

        return jsonify({
            'unit': self.unit,
            'closest': self.closest,
            'points': self.points,
        })

    def calculate_distance(self, id_, ll, country_code=None):

        distance = haversine(self.home, ll, unit=self.unit)
        self.points[id_].update({'distance': distance})

        if (
            self.country_code_comparison and
            country_code is not None and
            country_code != self.cf_headers['country_code']
        ):
                return

        if self.closest is None or distance < self.closest['distance']:
            self.closest = {
                'id': id_,
                'distance': distance,
            }
