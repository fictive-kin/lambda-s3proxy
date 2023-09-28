
import functools
import io
import json
import os
import re
import typing

import boto3
from flask import Flask, request, Response, render_template
from slugify import slugify

from application.utils import random_string


class LambdaMessageEncoder(json.JSONEncoder):
    def default(self, obj):
        if not type(obj) in [int, float, complex, dict, tuple, list, bool] and obj is not None:
            return str(obj)

        return super().default(self, obj)


def eleventy2flask(uri):
    """Changes an 11ty dynamic route spec to a Flask route spec"""

    return re.sub(r':([a-z0-9\.\-\_]+)', r'<\1>', uri, re.IGNORECASE)


class Flask11tyServerless:
    """A Flask extension to handle a routing to 11ty Serverless dynamic functions"""

    app: Flask = None
    lambda_client = None
    _data: typing.Dict = None

    def __init__(self, app: Flask = None, *, file: typing.Union[str, io.IOBase] = None):

        self._data = {}

        if app:
            self.init_app(app, file=file)

    def init_app(self, app: Flask, *, file: typing.Union[str, io.IOBase] = None):
        """
        Initializes a Flask application for using the integration.
        Currently, the model class supports a single app configuration only.
        Therefore, if there are multiple app configurations for this integration,
            the configuration will be overriden.
        Args:
            app (Flask): The flask application to initialize.
        """

        if not app or not isinstance(app, Flask):
            raise TypeError("Invalid Flask app instance provided.")

        self.app = app

        self.lambda_client = boto3.client('lambda')

        if file is not None:
            self.process_routes_from_file(file)

    def process_routes_from_file(self, file: typing.Union[str, io.IOBase], *, encoding: str = None):
        """Process a JSON file of routes to create them within Flask"""

        if encoding is None:
            encoding = 'utf-8'

        try:
            if isinstance(file, str):
                with open(file, 'r', encoding=encoding) as datafile:
                    data = json.load(datafile)
            else:
                data = json.load(file)

            self.process_routes(data)

        except (IOError, json.JSONDecodeError) as exc:
            self.app.logger.exception(exc)

    def process_routes(self, routes: typing.Dict):
        """Process a dict of routes to create within Flask"""

        for uri, target in routes.items():
            self.create_route(
                uri,
                target)

    def create_route(self, uri, target):
        """Create a single route within the Flask app"""

        route_id = f'routes-{slugify(uri)}'
        if route_id in self._data:
            route_id = f'{route_id}-{random_string(10)}'
        self._data.update({route_id: target})
        uri = eleventy2flask(uri)
        self.app.add_url_rule(
            uri,
            route_id,
            self.handle_route(route_id)
        )

    def handle_route(self, route_id):
        """Return the route function with the appropriate response for a Flask routing rule"""

        def invoke_func(**kwargs):
            upstream_payload = json.dumps(
                sanitize_headers(request.environ['lambda.event']),
                cls=LambdaMessageEncoder,
            )
            upstream_response = self.lambda_client.invoke(
                FunctionName=self._data[route_id].format(**kwargs),
                InvocationType='RequestResponse',
                Payload=upstream_payload.encode('utf-8')
            )

            payload = json.load(upstream_response['Payload'])

            error_partial = functools.partial(
                invoked_function_error_wrapper,
                json.loads(upstream_payload),
                upstream_response,
            )
            try:
                status = payload.get('statusCode', 500)
                if status == 404:
                    return error_partial(json.loads(payload['body']))

                resp_kwargs = {
                    'status': payload.get('statusCode', 500),
                    'response': payload['body'],
                }
                if 'headers' in payload:
                    resp_kwargs.update({'headers': payload['headers']})
            except (KeyError, IndexError, AttributeError) as exc:
                self.app.logger.exception(exc)
                return error_partial(payload)

            return Response(**resp_kwargs)

        return invoke_func


def invoked_function_error_wrapper(upstream_payload, response_metadata, response_payload):

    kwargs = {
        'response': response_payload if response_payload else response_metadata,
    }
    if request.args.get('include_payload') == 'yes':
        kwargs.update({
            'payload': upstream_payload,
        })

    return Response(
        status=503,
        response=render_template(
            "invoked-function-error.html",
            **kwargs
        ),
    )


def sanitize_headers(event_payload):
    headers = {}
    for k,v in event_payload.get('headers', {}).items():
        if 'token' in k.lower() or 'auth' in k.lower():
            continue
        headers.update({k: v})

    multi_headers = {}
    for k,v in event_payload.get('multiValueHeaders', {}).items():
        if 'token' in k.lower() or 'auth' in k.lower():
            continue
        multi_headers.update({k: v})

    event_payload['headers'] = headers
    event_payload['multiValueHeaders'] = multi_headers

    return event_payload
