
import io
import json
import typing

from flask import Flask, redirect
from slugify import slugify

from application.utils import random_string, forced_relative_redirect

REDIRECT_CODE = 302

REDIRECT_OPTIONS = [
    "DEFAULT_STATUS_CODE",
    "HANDLE_TRAILING_SLASH",
]


class FlaskJSONRedirects:
    """A Flask extension to handle a redirects JSON file to be able to add redirected routes easily"""

    app: Flask = None
    default_status_code: int = REDIRECT_CODE
    handle_trailing_slash: bool = False
    _data: typing.Dict = None

    def __init__(self, app: Flask = None, *, file: typing.Union[str, io.IOBase] = None):

        self.default_status_code = REDIRECT_CODE
        self.handle_trailing_slash = False
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

        option_prefix = 'REDIRECTS_'
        for key in REDIRECT_OPTIONS:
            setattr(self, key.lower(), app.config.get(f'{option_prefix}{key}'))

        self.default_status_code = int(self.default_status_code) if self.default_status_code else 302
        self.handle_trailing_slash = bool(self.handle_trailing_slash)

        if file is not None:
            self.process_redirects_from_file(file)

    def process_redirects_from_file(self, file: typing.Union[str, io.IOBase], *, encoding: str = None):
        """Process a JSON file of redirects to create them within Flask"""

        if encoding is None:
            encoding = 'utf-8'

        try:
            if isinstance(file, str):
                with open(file, 'r', encoding=encoding) as redirectsfile:
                    redirects = json.load(redirectsfile)
            else:
                redirects = json.load(file)

            self.process_redirects(redirects)

        except (IOError, json.JSONDecodeError) as exc:
            self.app.logger.exception(exc)

    def process_redirects(self, redirects: typing.Dict):
        """Process a dict of redirects to create them within Flask"""

        for uri, data in redirects.items():
            if isinstance(data, str):
                target = data
                handle_trailing_slash = None
                status_code = None

            else:
                target = data['target']
                handle_trailing_slash = data.get('trailing_slash', None)
                status_code = data.get('status', None)

            self.create_redirect(
                uri,
                target,
                handle_trailing_slash=handle_trailing_slash,
                status_code=status_code)

    def create_redirect(self, uri, target, *,
                        handle_trailing_slash: bool = None,
                        status_code: int = None):
        """Create a single redirect within the Flask app"""

        handle_trailing_slash = handle_trailing_slash if handle_trailing_slash is not None else self.handle_trailing_slash
        status_code = int(status_code) if status_code is not None else self.default_status_code

        redirect_id = f'redirects-{slugify(uri)}'
        if redirect_id in self._data:
            redirect_id = f'{redirect_id}-{random_string(10)}'
        self._data.update({redirect_id: target})
        self.app.add_url_rule(
            uri,
            redirect_id,
            self.handle_redirect(redirect_id, status_code)
        )

        if handle_trailing_slash and uri != '/':
            opposite_uri = f'{uri}/' if uri[-1] != '/' else uri[:-1]
            self.app.add_url_rule(
                opposite_uri,
                f'{redirect_id}-slashed',
                self.handle_redirect(redirect_id, status_code)
            )

    def handle_redirect(self, redirect_id, status_code):
        """Return the redirect function with the appropriate response for a Flask routing rule"""

        def redirect_func(**kwargs):
            url = self._data[redirect_id].format(**kwargs)
            if url.startswith('http:') or url.startswith('https:'):
                return redirect(url, code=status_code)

            return forced_relative_redirect(url, code=status_code)

        return redirect_func
