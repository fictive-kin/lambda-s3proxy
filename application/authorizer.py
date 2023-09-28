
import io
import json
import re
import typing

from flask import Flask, Response, request, jsonify
from werkzeug.http import parse_authorization_header

from application.utils import random_string, str2json


class FlaskJSONAuthorizer:
    """A Flask extension to handle an authorizations JSON file to be able to protect routes easily"""

    app: Flask = None
    _default_realm: str = None
    _routes: typing.Dict = None
    _simple: typing.Dict = None
    _regexes: typing.Dict = None

    def __init__(self, app: Flask = None, *, file: typing.Union[str, io.IOBase] = None):

        self._simple = {}
        self._regexes = {}
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

        if file is not None:
            self.process_authorizations_from_file(file)

        if self.routes:
            self.process_authorizations(self.routes)

        self.app.before_request(self.check_authorization)

    @property
    def default_realm(self):
        if self._default_realm:
            return self._default_realm

        if self.app is None:
            raise ValueError('FlaskJSONAuthorizer is not fully initialized')

        self._default_realm = self.app.config.get('AUTHORIZER_DEFAULT_REALM', 'Restricted Access')
        return self._default_realm

    @default_realm.setter
    def default_realm(self, value):
        self._default_realm = value

    @property
    def routes(self):
        if self._routes is not None:
            return self._routes

        if self.app is None:
            raise ValueError('FlaskJSONAuthorizer is not fully initialized')

        self._routes = str2json(self.app.config.get('AUTHORIZER_ROUTES', []))
        return self._routes

    @routes.setter
    def routes(self, value):
        self._routes = value

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
                auth = parse_authorization_header(f'Basic {data}')
                username = auth.username
                password = auth.password
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

        auth_data = {
            'username': username,
            'password': password,
            'realm': realm if realm is not None else self.default_realm,
        }

        if '*' in uri:
            auth_data.update({'pattern': re.compile(uri)})
            self._regexes.update({uri: auth_data})

        else:
            self._simple.update({uri: auth_data})

            if uri[:-1] != '/':
                self._simple.update({f'{uri}/': auth_data})

    def check_authorization(self):
        """Before request handler to check the authorization header"""

        if request.url_rule is None:
            # This means that the request didn't match any app routing rules, therefore, there is
            # nothing to return to a browser, and it won't need to have authorization.
            return

        data = None
        if request.path in self._simple:
            data = self._simple[request.path]
        elif request.url_rule.rule in self._simple:
            data = self._simple[request.url_rule.rule]

        else:
            for potential_data in self._regexes.values():
                if potential_data['pattern'].search(request.path):
                    data = potential_data
                    break

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
