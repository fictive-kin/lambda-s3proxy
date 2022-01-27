
import io
import json
import typing

from flask import Flask, Response, request
from slugify import slugify
from werkzeug.http import parse_authorization_header

from application.utils import random_string


AUTHORIZER_OPTIONS = [
    "DEFAULT_REALM",
    "ROUTES",
]


class FlaskJSONAuthorizer:
    """A Flask extension to handle an authorizations JSON file to be able to protect routes easily"""

    app: Flask = None
    default_realm: str = None
    routes: typing.Dict = None
    _data: typing.Dict = None

    def __init__(self, app: Flask = None, *, file: typing.Union[str, io.IOBase] = None):

        self._data = {}
        self.routes = {}

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

        option_prefix = 'AUTHORIZER_'
        for key in AUTHORIZER_OPTIONS:
            setattr(self, key.lower(), app.config.get(f'{option_prefix}{key}'))

        if not self.default_realm:
            # We need something to fallback on if the realm is not specified when we do the
            # authorization check
            self.default_realm = 'Restricted Access'

        if file is not None:
            self.process_authorizations_from_file(file)

        self.app.before_request(self.check_authorization)

    def process_authorizations_from_file(self, file: typing.Union[str, io.IOBase], *, encoding: str = None):
        """Process a JSON file of authorizations to protect routes within Flask"""

        if encoding is None:
            encoding = 'utf-8'

        try:
            if isinstance(file, str):
                with open(file, 'r', encoding=encoding) as authorizationsfile:
                    authorizations = json.load(authorizationsfile)
            else:
                authorizations = json.load(file)

            self.process_authorizations(authorizations)

        except (IOError, json.JSONDecodeError) as exc:
            self.app.logger.exception(exc)

    def process_authorizations(self, authorizations: typing.Dict):
        """Process a dict of authorizations to protect routes within Flask"""

        for uri, data in authorizations.items():
            if isinstance(data, str):
                username, password = parse_authorization_header(f'Basic {data}')
                realm = None

            else:
                username = data['username']
                password = data['password']
                realm = data.get('realm', None)

            self.add_protected_route(
                uri,
                username,
                password,
                realm=realm)

    def add_protected_route(self, uri, username, password, *, realm: str = None):
        """Create a single protected route within the Flask app"""

        realm = realm if realm is not None else self.default_realm

        self._data.update({
            uri: {
                'username': username,
                'password': password,
                'realm': realm,
            }
        })

    def check_authorization(self):
        """Before request handler to check the authorization header"""

        if request.url_rule is None or request.url_rule.rule not in self._data:
            # This means that the request didn't match any app routing rules, therefore, there is
            # nothing to return to a browser, and it won't need to have authorization.
            return

        data = None
        if request.path in self._data:
            data = self._data[request.path]
        elif request.url_rule.rule in self._data:
            data = self._data[request.url_rule.rule]

        # TODO: Figure out how to handle protected subfolders, etc, maybe via regex?

        if data is None:
            # This means that we didn't have any protection rules setup for this route
            return

        auth = parse_authorization_header(request.headers.get('Authorization'))
        if auth is not None and auth.username is not None and auth.password is not None:
            if auth.username == data['username'] and auth.password == data['password']:
                # The browser provided the correct credentials
                return

        return Response(
           'Authorization is required',
            401,
            {'WWW-Authenticate': f'Basic realm="{data["realm"]}"'})
