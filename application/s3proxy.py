# -*- coding: utf-8 -*-

from functools import cached_property
import json
import time
import typing

import boto3
from botocore.exceptions import ClientError
from flask import Flask, abort, Blueprint, Response, redirect, request
import pytz
from slugify import slugify

from application.utils import forced_host_redirect, str2bool, str2json


# When working behind APIGateway, we have a hard limit of a 10 MB response payload and when
# running in AWS Lambda, the hard limit is lowered to 6MB. We set it to 4.5MB so as to be sure
# that no issues will arise.
OVERFLOW_SIZE = 4.5 * 1024 * 1024

HTTP_HEADER_DATE_FORMAT = '%a, %d %b %Y %H:%M:%S GMT'


class FlaskS3Proxy:
    _client = None
    app: Flask = None
    _bucket: str = None
    _prefix: str = None
    _trailing_slash_only: bool = None
    _trailing_slash_redirection: bool = None
    _redirect_code: int = None
    _routes: list = None
    _subroutes: typing.Dict[str, str] = None
    _locales: list = None

    def __init__(self, app, *, boto3_client=None, bucket=None, prefix=None, paths=None, **kwargs):
        if boto3_client is None:
            self._client = boto3.client('s3')
        else:
            self._client = boto3_client

        self.locales = []
        if bucket is not None:
            self.bucket = bucket
        if prefix is not None:
            self.prefix = prefix

        if app:
            self.init_app(app, paths=paths, **kwargs)

    def set_options(self, *, bucket=None, prefix=None):
        if bucket is not None:
            self.bucket = bucket
        if prefix is not None:
            self.prefix = prefix

        try:
            if self.prefix.startswith('/'):
                self.prefix = self.prefix[1:]
        except Exception:  # pylint: disable=broad-except
            pass

        try:
            if self.prefix.endswith('/'):
                self.prefix = self.prefix[:-1]
        except Exception:  # pylint: disable=broad-except
            pass

    @property
    def bucket(self):
        if self._bucket is not None:
            return self._bucket

        if self.app is None:
            raise ValueError('FlaskS3Proxy is not fully initialized')

        self._bucket = self.app.config.get('S3PROXY_BUCKET')
        return self._bucket

    @bucket.setter
    def bucket(self, value):
        self._bucket = value

    @property
    def prefix(self):
        if self._prefix is not None:
            return self._prefix

        if self.app is None:
            raise ValueError('FlaskS3Proxy is not fully initialized')

        self._prefix = self.app.config.get('S3PROXY_PREFIX')
        return self._prefix

    @prefix.setter
    def prefix(self, value):
        self._prefix = value

    @property
    def routes(self):
        if self._routes is not None:
            return self._routes

        if self.app is None:
            raise ValueError('FlaskS3Proxy is not fully initialized')

        self._routes = str2json(self.app.config.get('S3PROXY_ROUTES'))
        return self._routes

    @routes.setter
    def routes(self, value):
        self._routes = value

    @property
    def subroutes(self):
        if self._subroutes is not None:
            return self._subroutes

        if self.app is None:
            raise ValueError('FlaskS3Proxy is not fully initialized')

        self._subroutes = str2json(self.app.config.get('S3PROXY_SUBROUTES'))
        return self._subroutes

    @subroutes.setter
    def subroutes(self, value):
        self._subroutes = value

    @property
    def locales(self):
        if self._locales is not None:
            return self._locales

        if self.app is None:
            raise ValueError('FlaskS3Proxy is not fully initialized')

        self._locales = str2json(self.app.config.get('S3PROXY_LOCALES'))
        return self.locales

    @locales.setter
    def locales(self, value):
        self._locales = value

    @property
    def trailing_slash_only(self):
        if self._trailing_slash_only is not None:
            return self._trailing_slash_only

        if self.app is None:
            raise ValueError('FlaskS3Proxy is not fully initialized')

        self._trailing_slash_only = str2bool(
            self.app.config.get('S3PROXY_TRAILING_SLASH_ONLY', True)
        )
        return self._trailing_slash_only

    @trailing_slash_only.setter
    def trailing_slash_only(self, value):
        self._trailing_slash_only = bool(value)

    @property
    def trailing_slash_redirection(self):
        if self._trailing_slash_redirection is not None:
            return self._trailing_slash_redirection

        if self.app is None:
            raise ValueError('FlaskS3Proxy is not fully initialized')

        self._trailing_slash_redirection = str2bool(
            self.app.config.get('S3PROXY_TRAILING_SLASH_REDIRECTION', True)
        )
        return self._trailing_slash_redirection

    @trailing_slash_redirection.setter
    def trailing_slash_redirection(self, value):
        self._trailing_slash_redirection = bool(value)

    @property
    def redirect_code(self):
        if self._redirect_code is not None:
            return self._redirect_code

        if self.app is None:
            raise ValueError('FlaskS3Proxy is not fully initialized')

        value = int(self.app.config.get('S3PROXY_REDIRECT_CODE', 302))
        if not (300 < value < 400):
            self.app.logger.warning(
                f'Ignoring provided redirect code for being outside of range: {value}'
            )
            value = 302

        self._redirect_code = value
        return self._redirect_code

    @redirect_code.setter
    def redirect_code(self, value):
        if 300 < int(value) < 400:
            raise ValueError(f'Redirect code value is outside of range: {int(value)}')
        self._redirect_code = int(value)

    def init_app(self, app, *, bucket=None, prefix=None, paths=None, **kwargs):
        self.app = app
        self.set_options(bucket=bucket, prefix=prefix)

        if not self.bucket:
            self.app.logger.warning(
                'S3 Bucket has not been provided. Cannot proceed with FlaskS3Proxy setup.')
            return

        # Add any passed paths to the handled routes
        self.add_handled_routes(paths, **kwargs)
        # Add any configured paths to the handled routes
        self.add_handled_routes(self.routes, **kwargs)
        # If subroutes are configured, add those:
        self.setup_subroutes()

        @self.app.errorhandler(404)
        def page_not_found(error):
            return self.handle_404(error)

        @self.app.errorhandler(500)
        def server_error(error):
            return self.handle_500(error)

    def handle_404(self, error):
        try:
            resp = self.retrieve('404/index.html', abort_on_fail=False)
            if not resp:
                resp = self.retrieve('404.html', abort_on_fail=False)
                if not resp:
                    raise Exception()  # This is just to prevent a 500 from occuring
            resp.status = 404
            return resp

        except Exception:  # pylint: disable=broad-except
            return Response('Page Not Found', status=404, content_type='text/plain')

    def handle_500(self, error):
        try:
            resp = self.retrieve('500/index.html', abort_on_fail=False)
            if not resp:
                resp = self.retrieve('500.html', abort_on_fail=False)
                if not resp:
                    raise Exception()  # This is just to prevent a true 500 from occuring
            resp.status = 500
            return resp

        except Exception:  # pylint: disable=broad-except
            return Response('Internal Server Error', status=500, content_type='text/plain')

    def add_handled_routes(self, paths, **kwargs):
        if not isinstance(paths, (list, set, tuple,)):
            return

        def path_to_check(path):
            return '/<path:' if path.startswith('/<path:') and '/' not in path[1:] else path

        # Don't overload the routing map if the path we want to set is already present.
        # Handle `/<path:[^/]` specially, since that could have any variable name used within it
        # and it is usually one of our default routes.
        configured_paths = [path_to_check(rule.rule) for rule in self.app.url_map.iter_rules()]

        for path in paths:
            if path_to_check(path) not in configured_paths:
                self.add_handled_route(path, **kwargs)
            else:
                self.app.logger.warning(
                    f"Not using S3 Proxy for '{path}'. It was already defined in the routing map.")

    def add_handled_route(self, path, **kwargs):
        slug = slugify(path)
        if not slug:
            slug = 'index'
        self.app.add_url_rule(path, endpoint=slug, view_func=self.proxy_it, **kwargs)

    def proxy_it(self, url=None):
        if url is None:
            return self.retrieve('index.html')

        def retrieve_from_possibilities(
            possibilities, *,
            check_for_trailing_slash_path=None,
        ):
            for possible in possibilities:
                self.app.logger.info(f'Checking for: {possible}')
                response = self.retrieve(possible, abort_on_fail=False)
                if response and getattr(response, 'status_code', None):
                    return response

            if check_for_trailing_slash_path is not None:
                response = retrieve_from_possibilities((f'{check_for_trailing_slash_path}/index.html',))

                if response is not None:
                    return self.redirect_with_querystring(f'/{check_for_trailing_slash_path}/')

            return None

        has_trailing_slash = url.endswith('/')
        check_for_trailing_slash_path = None

        if not has_trailing_slash:
            self.app.logger.debug(f'Requested URL has no trailing slash: {url}')

            if self.trailing_slash_only:
                # Check for:
                # - /my-page
                # - /my-page.html
                possibilities = (url, f'{url}.html',)
                check_for_trailing_slash_path = url

            else:
                # Check for:
                # - /my-page
                # - /my-page.html
                # - /my-page/index.html
                possibilities = (url, f'{url}/index.html', f'{url}.html',)

        else:
            self.app.logger.debug(f'Requested URL has trailing slash: {url}')
            if self.trailing_slash_redirection:
                return self.redirect_with_querystring(f'/{url[:-1]}')

            else:
                url = url[:-1]

            # Check for:
            # - /my-page
            # - /my-page.html
            # - /my-page/index.html
            possibilities = (url, f'{url}/index.html', f'{url}.html',)

        response = retrieve_from_possibilities(
            possibilities,
            check_for_trailing_slash_path=check_for_trailing_slash_path,
        )

        return abort(404) if response is None else response

    def redirect_with_querystring(self, target, *, code=None):
        if request.query_string:
            target = f"{target}?{request.query_string.decode('utf-8')}"
        return forced_host_redirect(target, code=code if code else self.redirect_code)

    def datetime_to_header(self, dt):
        return time.strftime(
            HTTP_HEADER_DATE_FORMAT,
            dt.replace(tzinfo=pytz.UTC).timetuple(),
        )


    def get_file(self, key):
        return self._client.get_object(Bucket=self.bucket, Key=key)


    def retrieve(self, url, *, abort_on_fail=True):
        s3_url = f'{self.prefix}/{url}' if self.prefix else url

        try:
            s3_obj = self.get_file(s3_url)

            if 'Body' not in s3_obj:
                self.app.logger.info(f'No body was returned from S3 for: {s3_url}')
                return None

            contents = s3_obj['Body'].read()

            if 'ContentLength' not in s3_obj:
                content_length = len(contents)
            else:
                content_length = int(s3_obj['ContentLength'])

            if content_length == 0 and s3_url in ('soap', 'soap/', 'soap.html', 'soap/index.html',):
                self.app.logger.info('Guarding against S3 soap endpoint')
                return None

            if content_length > OVERFLOW_SIZE:
                # URL only works for 60 seconds
                url = self._client.generate_presigned_url('get_object',
                          Params={
                              'Bucket': self.bucket,
                              'Key': s3_url
                          },
                          ExpiresIn=60)

                self.app.logger.warning('Redirecting to S3 contents via signed URL')
                # This cannot redirect with the other status codes because it's an oversize page
                # and the URL will be different on each request. Therefore we use: "303 See Other"
                return redirect(url, 303)

            self.app.logger.info('Returning S3 contents')
            response = Response(response=contents)
            if 'ContentType' in s3_obj:
                response.headers['Content-Type'] = str(s3_obj['ContentType'])
            if 'CacheControl' in s3_obj:
                response.headers['Cache-Control'] = str(s3_obj['CacheControl'])
            if 'Expires' in s3_obj:
                response.headers['Expires'] = self.datetime_to_header(s3_obj['Expires'])
            if 'LastModified' in s3_obj:
                response.headers['Last-Modified'] = self.datetime_to_header(s3_obj['LastModified'])
            return response

        except Exception as exc:  # pylint: disable=broad-except
            self.app.logger.info('Unable to open: {}/{}: {}'.format(self.bucket, s3_url, exc))
            pass

        if abort_on_fail:
            return abort(404)

        return None

    def setup_subroutes(self, *, file=None, subroutes=None):
        if file is not None:
            try:
                file_obj = self.get_file(file)
                self.subroutes = json.loads(file_obj['Body'].read())
                self.app.logger.info('Loaded subroutes from S3')

            except ClientError as exc:
                if exc.response['Error']['Code'] == 'NoSuchKey':
                    self.app.logger.warning(
                        f"Unable to instantiate sub-routes: {file} does not exist in S3")
                    return

                raise

            except json.JSONDecodeError as exc:
                self.app.logger.exception(exc)
                return

        if subroutes is not None:
            if self.subroutes is None:
                self.subroutes = {}

            self.subroutes.update(subroutes)

        if self.subroutes:
            for route,bucket in self.subroutes.items():
                prefix = route.split('/<path:url>')
                self.app.logger.info(f'Instantiating sub-route: {route} -> {bucket}{prefix[0]}')

                subroute_blueprint = FlaskS3ProxyBlueprint(
                    self.app,
                    prefix=prefix[0],
                    paths=[route],
                    bucket=bucket,
                    fallback=self,
                    methods=['GET', 'POST'],
                )

    def setup_locales(self, *, file=None, locales=None, enable_auto_switch=None):

        if file is not None:
            try:
                locales_file_obj = self.get_file(file)
                self.locales = json.loads(locales_file_obj['Body'].read())
                self.app.logger.info('Loaded locales from S3')

            except ClientError as exc:
                if exc.response['Error']['Code'] == 'NoSuchKey':
                    self.app.logger.warning(
                        f"Unable to instantiate multi-locales: {file} does not exist in S3")
                    return

                raise

            except json.JSONDecodeError as exc:
                self.app.logger.exception(exc)
                return

        if isinstance(self.locales, str):
            # Attempt to JSON decode the value, since it might have been a JSON string in an env var
            try:
                self.locales = json.loads(self.locales)
            except json.JSONDecodeError:
                pass

            # In case it loaded to a new string, we want it as a list
            if isinstance(self.locales, str):
                self.locales = [self.locales]

        if locales is not None:
            self.locales = (self.locales + locales) if self.locales else locales

        if self.locales:
            # Use a set to ensure that we only have 1 of each locale
            for locale in set(self.locales):
                self.app.logger.info(f'Instantiating locale-specific blueprint for {locale}')

                locale_specific_proxy = FlaskS3ProxyBlueprint(
                    self.app,
                    prefix=f'/{locale}',
                    paths=[f'/{locale}/', f'/{locale}/<path:url>'],
                    fallback=self,
                    methods=['GET', 'POST'],
                )
                setattr(self.app, f's3_proxy_{locale}', locale_specific_proxy)

            if enable_auto_switch:
                if isinstance(enable_auto_switch, bool):
                    switchable_paths = ['/']
                elif not isinstance(enable_auto_switch, (list, set, tuple,)):
                    switchable_paths = [enable_auto_switch]
                else:
                    switchable_paths = enable_auto_switch

                @self.app.before_request
                def switch_locale():
                    desired_locale = request.cookies.get('locale', False)

                    if desired_locale and desired_locale in self.locales:
                        if request.path in switchable_paths:
                            self.app.logger.info(
                                f'Redirecting due to user cookie: {request.path} -> /{desired_locale}')
                            return self.redirect_with_querystring(f'/{desired_locale}', code=303)

                    return None


class FlaskS3ProxyBlueprint(FlaskS3Proxy):
    bp: Blueprint = None
    fallback: FlaskS3Proxy = None

    def __init__(self, app, *, boto3_client=None, bucket=None, prefix=None, paths=None, fallback=None, **kwargs):
        # Intentionally not providing app to parent init
        super().__init__(None, boto3_client=boto3_client, bucket=bucket, prefix=prefix)

        self.app = app

        name = 's3proxy'
        if self.prefix:
            name += f'-{slugify(self.prefix)}'
        self.bp = Blueprint(name, __name__)

        if fallback:
            self.fallback = fallback

        if paths:
            self.register_blueprint(paths, **kwargs)

    def register_blueprint(self, paths, *, bucket=None, prefix=None, **kwargs):
        self.set_options(bucket=bucket, prefix=prefix)

        if not self.bucket:
            self.app.logger.warning(
                'S3 Bucket has not been provided. Cannot proceed with FlaskS3ProxyBlueprint setup.')
            return

        # Add any passed paths to the handled routes
        self.add_handled_routes(paths, **kwargs)

        @self.bp.errorhandler(404)
        def page_not_found(error):
            return self.handle_404(error)

        @self.bp.errorhandler(500)
        def server_error(error):
            return self.handle_500(error)

        self.app.register_blueprint(self.bp)

    def add_handled_route(self, path, **kwargs):
        slug = slugify(path)
        self.bp.add_url_rule(path, endpoint=slug, view_func=self.proxy_it, **kwargs)

    def handle_404(self, error):
        resp = super().handle_404(error)
        if resp.content_type == 'text/plain' and self.fallback:
            return self.fallback.handle_404(error)

        return resp

    def handle_500(self, error):
        resp = super().handle_500(error)
        if resp.content_type == 'text/plain' and self.fallback:
            return self.fallback.handle_500(error)

        return resp
