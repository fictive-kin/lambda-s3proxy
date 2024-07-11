# -*- coding: utf-8 -*-

from dataclasses import dataclass
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

    _backwards_compatible: bool = None
    _use_country_code_comparison: bool = None
    _include_absolute_closest: bool = None
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

    @property
    def include_absolute_closest(self):
        if self._include_absolute_closest is not None:
            return self._include_absolute_closest

        self._include_absolute_closest = str2bool(self.app.config.get('GEOGRAPHY_INCLUDE_ABSOLUTE_CLOSEST', True))
        return self._include_absolute_closest

    @include_absolute_closest.setter
    def include_absolute_closest(self, value):
        self._include_absolute_closest = bool(value)

    @property
    def backwards_compatible(self):
        if self._backwards_compatible is not None:
            return self._backwards_compatible

        self._backwards_compatible = str2bool(self.app.config.get('GEOGRAPHY_BACKWARDS_COMPATIBLE', False))
        return self._backwards_compatible

    @backwards_compatible.setter
    def backwards_compatible(self, value):
        self._backwards_compatible = bool(value)

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

            arg_include_absolute_closest = request.args.get('include_absolute_closest')
            if arg_include_absolute_closest:  # Truthy because of strings, regardless of value
                include_absolute_closest = arg_include_absolute_closest not in ['false', '0', '']
            else:
                include_absolute_closest = self.include_absolute_closest

            arg_backwards_compatible = request.args.get('backwards_compatible')
            if arg_backwards_compatible:  # Truthy because of strings, regardless of value
                backwards_compatible = arg_backwards_compatible not in ['false', '0', '']
            else:
                backwards_compatible = self.backwards_compatible

            return FlaskGeographyResponse(
                country_code_comparison=use_country_code,
                include_absolute_closest=include_absolute_closest,
                backwards_compatible=backwards_compatible,
            )

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
    incountry_points = None
    absolute_closest = None
    points = None
    home = None
    country_code_comparison = None
    include_absolute_closest = None
    backwards_compatible = None

    def __init__(
        self,
        *,
        country_code_comparison=True,
        include_absolute_closest=True,
        backwards_compatible=False,
    ):
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

        self.incountry_points = []
        self.absolute_closest = {}
        self.points = {}
        self.home = {}
        self.unit = REGION_UNIT.get(self.cf_headers['country_code'], Unit.KILOMETERS)
        self.country_code_comparison = bool(country_code_comparison)
        self.include_absolute_closest = bool(include_absolute_closest)
        self.backwards_compatible = bool(backwards_compatible)

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
        self.absolute_closest = None
        self.incountry_closest = None
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

        self.incountry_points = sorted(self.incountry_points, key=lambda x: x.distance)

        data = {
            'absolute': self.absolute_closest,
            'incountry': self.incountry_points,
            'points': self.points,
            'unit': self.unit,
        }

        if self.backwards_compatible:
            if self.country_code_comparison:
                try:
                    closest = self.incountry_points[0]
                except IndexError:
                    closest = {}
            else:
                closest = self.absolute_closest

            data.update({'closest': closest})

        return jsonify(data)

    def calculate_distance(self, id_, ll, country_code=None):

        distance = LocationDistance(
            id=id_,
            distance=haversine(self.home, ll, unit=self.unit),
        )
        self.points[id_].update({'distance': distance.distance})

        if not self.absolute_closest or distance.is_closer(self.absolute_closest):
            self.absolute_closest = distance

        if (
            country_code is not None and
            country_code == self.cf_headers['country_code']
        ):
            self.incountry_points.append(distance)


@dataclass
class LocationDistance:
    id: str = None
    distance: float = None

    def is_closer(self, other):
        if isinstance(other, self.__class__):
            return self.distance < other.distance
        if isinstance(other, (int, float,)):
            return self.distance < other

        return False

    def is_equal(self, other):
        if isinstance(other, self.__class__):
            return self.distance == other.distance
        if isinstance(other, (int, float,)):
            return self.distance == other

        return False

    def is_further(self, other):
        if isinstance(other, self.__class__):
            return self.distance > other.distance
        if isinstance(other, (int, float,)):
            return self.distance > other

        return False
