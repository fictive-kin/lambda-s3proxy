import io
import json
import re
import typing as t

from flask import Flask, Response, abort, request
from werkzeug.datastructures import Authorization

from ..utils import add_no_cache, str2json


class FlaskJSONAuthorizer:
    """A Flask extension to handle an authorizations JSON file to be able to protect routes easily"""

    app: Flask = None  # type: ignore
    _default_realm: t.Optional[str] = None
    _routes: t.Dict[str, str] = None  # type: ignore
    _simple: t.Dict[str, t.Dict[str, t.Any]] = None  # type: ignore
    _regexes: t.Dict[str, t.Dict[str, t.Any]] = None  # type: ignore
    _forms: t.Dict[str, t.Dict[str, t.Any]] = None  # type: ignore

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
    def s3proxy(self):
        return self.app.extensions["s3_proxy"]

    @property
    def session(self):
        return self.app.extensions["session"]

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
                self.add_protected_route(
                    uri,
                    {
                        "username": getattr(auth, "username", None),
                        "password": getattr(auth, "password", None),
                    },
                )

            elif data.get("username") or data.get("form"):
                self.add_protected_route(uri, data)

            else:
                self.app.logger.warning(f"Could not create a protected route for {uri}")

    def add_protected_route(
        self,
        uri: str,
        auth_data: t.Dict[str, str | re.Pattern | None],
    ):
        """Create a single protected route within the Flask app"""

        if "*" in uri:
            auth_data.update({"pattern": re.compile(uri)})
            self._regexes.update({uri: auth_data})

        else:
            self._simple.update({uri: auth_data})
            opposite = uri[:-1] if uri.endswith("/") else f"{uri}/"
            self._simple.update({opposite: auth_data})

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

        if self.s3proxy and data.get("form"):
            # We can't retrieve a form, if the s3 proxy extension isn't setup
            return self.form_authenticate(data)

        else:
            return self.basic_authenticate(data)

    def form_authenticate(self, data):
        if (
            data.get("password")
            and self.session.get("authorizer-password") == data["password"]
        ):
            setattr(request, "is_protected_page", True)
            return

        incorrect_password = False
        if request.method.upper() == "POST":
            if request.form.get("password") == data["password"]:
                self.session.update({"authorizer-password": data["password"]})
                setattr(request, "is_protected_page", True)
                return
            else:
                incorrect_password = True

        url = data["form"][1:] if data["form"].startswith("/") else data["form"]
        if url.endswith(".html"):
            possibilities = [url]
        elif url.endswith("/"):
            possibilities = [f"{url[:-1]}.html", f"{url}index.html"]
        else:
            possibilities = (
                url,
                f"{url}.html",
                f"{url}/index.html",
            )
        response = self.s3proxy.retrieve_from_possibilities(possibilities)
        if response is None:
            abort(404)

        response = add_no_cache(response)
        return (
            self.replace_message(
                response, "<!-- ERROR_MESSAGE -->", data.get("error_password", "")
            )
            if incorrect_password
            else response
        )

    def replace_message(
        self, response: Response, needle: str, replace: str
    ) -> Response:
        if not needle or not replace:
            return response

        if response.mimetype and not response.mimetype.startswith("text"):
            return response

        data: str = (
            response.data.decode()
            if isinstance(response.data, bytes)
            else response.data
        )
        response.data = data.replace(needle, replace)
        return response

    def basic_authenticate(self, data):
        auth = Authorization.from_header(request.headers.get("Authorization"))
        if auth is not None and auth.username is not None and auth.password is not None:
            if auth.username == data["username"] and auth.password == data["password"]:
                # The browser provided the correct credentials
                return

        return Response(
            "Authorization is required",
            401,
            {
                "WWW-Authenticate": f'Basic realm="{data.get("realm") or self.default_realm}"'
            },
        )
