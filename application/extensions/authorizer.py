import io
import json
import re
import typing as t

from flask import Flask, Response, request
from werkzeug.datastructures import Authorization

from ..utils import str2json


class FlaskJSONAuthorizer:
    """A Flask extension to handle an authorizations JSON file to be able to protect routes easily"""

    app: Flask = None  # type: ignore
    _default_realm: t.Optional[str] = None
    _routes: t.Dict[str, str] = None  # type: ignore
    _simple: t.Dict[str, t.Dict[str, t.Any]] = None  # type: ignore
    _regexes: t.Dict[str, t.Dict[str, t.Any]] = None  # type: ignore

    def __init__(
        self, app: t.Optional[Flask] = None, *, file: t.Optional[str | io.IOBase] = None
    ):

        self._simple = {}
        self._regexes = {}
        self.routes = {}

        if app:
            self.init_app(app, file=file)

    def init_app(self, app: Flask, *, file: t.Optional[str | io.IOBase] = None):
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
    def default_realm(self) -> str:
        if self._default_realm:
            return self._default_realm

        if self.app is None:
            raise ValueError("FlaskJSONAuthorizer is not fully initialized")

        self._default_realm = self.app.config.get(
            "AUTHORIZER_DEFAULT_REALM", "Restricted Access"
        )
        return self._default_realm  # type: ignore

    @default_realm.setter
    def default_realm(self, value: str | None):
        self._default_realm = value  # type: ignore

    @property
    def routes(self) -> t.Dict:
        if self._routes is not None:
            return self._routes

        if self.app is None:
            raise ValueError("FlaskJSONAuthorizer is not fully initialized")

        self._routes = str2json(self.app.config.get("AUTHORIZER_ROUTES", []))
        return self._routes

    @routes.setter
    def routes(self, value: t.Dict | None):
        self._routes = value  # type: ignore

    def process_authorizations_from_file(
        self, file: str | io.IOBase, *, encoding: t.Optional[str] = None
    ):
        """Process a JSON file of authorizations to protect routes within Flask"""

        if encoding is None:
            encoding = "utf-8"

        try:
            if isinstance(file, str):
                with open(file, "r", encoding=encoding) as authorizationsfile:
                    authorizations = json.load(authorizationsfile)
            else:
                authorizations = json.load(file)

            self.process_authorizations(authorizations)

        except (IOError, json.JSONDecodeError) as exc:
            self.app.logger.exception(exc)

    def process_authorizations(self, authorizations: t.Dict):
        """Process a dict of authorizations to protect routes within Flask"""

        for uri, data in authorizations.items():
            if isinstance(data, str):
                auth = Authorization.from_header(f"Basic {data}")
                username = getattr(auth, "username", None)
                password = getattr(auth, "password", None)
                realm = None

            else:
                username = data["username"]
                password = data["password"]
                realm = data.get("realm", None)

            self.add_protected_route(uri, username, password, realm=realm)

    def add_protected_route(
        self,
        uri: str,
        username: str | None,
        password: str | None,
        *,
        realm: t.Optional[str] = None,
    ):
        """Create a single protected route within the Flask app"""

        auth_data: t.Dict[str, str | re.Pattern | None] = {
            "username": username,
            "password": password,
            "realm": realm if realm is not None else self.default_realm,
        }

        if "*" in uri:
            auth_data.update({"pattern": re.compile(uri)})
            self._regexes.update({uri: auth_data})

        else:
            self._simple.update({uri: auth_data})

            if uri[:-1] != "/":
                self._simple.update({f"{uri}/": auth_data})

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
                if isinstance(potential_data["pattern"], re.Pattern) and potential_data[
                    "pattern"
                ].search(request.path):
                    data = potential_data
                    break

        if data is None:
            # This means that we didn't have any protection rules setup for this route
            return

        auth = Authorization.from_header(request.headers.get("Authorization"))
        if auth is not None and auth.username is not None and auth.password is not None:
            if auth.username == data["username"] and auth.password == data["password"]:
                # The browser provided the correct credentials
                return

        return Response(
            "Authorization is required",
            401,
            {"WWW-Authenticate": f'Basic realm="{data["realm"]}"'},
        )
