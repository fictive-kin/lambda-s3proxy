# -*- coding: utf-8 -*-

import json
import time

import boto3
from botocore.exceptions import ClientError
from flask import Flask, abort, Blueprint, Response, redirect, request
import pytz
from slugify import slugify

from application.utils import forced_host_redirect


# When working behind APIGateway, we have a hard limit of a 10 MB response payload and when
# running in AWS Lambda, the hard limit is lowered to 6MB. We set it to 4.5MB so as to be sure
# that no issues will arise.
OVERFLOW_SIZE = 4.5 * 1024 * 1024

REDIRECT_CODE = 302
HTTP_HEADER_DATE_FORMAT = '%a, %d %b %Y %H:%M:%S GMT'

S3PROXY_OPTIONS = [
    "BUCKET",
    "PREFIX",
    "TRAILING_SLASH_REDIRECTION",
    "REDIRECT_CODE",
    "ROUTES",
    "LOCALES",
]


class FlaskS3Proxy:
    _client = None
    app: Flask = None
    bucket: str = None
    prefix: str = None
    trailing_slash_redirection: bool = True
    redirect_code: int = REDIRECT_CODE
    routes: list = None

    def __init__(self, app, boto3_client=None, bucket=None, prefix=None, paths=None, **kwargs):
        if boto3_client is None:
            self._client = boto3.client('s3')
        else:
            self._client = boto3_client

        if bucket is not None:
            self.bucket = bucket
        if prefix is not None:
            self.prefix = prefix

        if app:
            self.init_app(app, paths=paths, **kwargs)

    def set_options(self, bucket=None, prefix=None):
        if bucket is not None:
            self.bucket = bucket
        if prefix is not None:
            self.prefix = prefix

        option_prefix = 'S3PROXY_'
        for key in S3PROXY_OPTIONS:
            if self.app.config.get(f'{option_prefix}{key}'):
                setattr(self, key.lower(), self.app.config.get(f'{option_prefix}{key}'))

        self.redirect_code = int(self.redirect_code) if self.redirect_code else REDIRECT_CODE
        self.trailing_slash_redirection = bool(self.trailing_slash_redirection)

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

    def init_app(self, app, bucket=None, prefix=None, paths=None, **kwargs):
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

        if url.endswith('/'):
            if self.trailing_slash_redirection:
                return self.redirect_with_querystring(f'/{url[:-1]}')
            else:
                url = url[:-1]

        # Check for:
        # - /my-page
        # - /my-page.html
        # - /my-page/index.html
        for possible in (url, f'{url}/index.html', f'{url}.html'):
            response = self.retrieve(possible, abort_on_fail=False)
            if response and getattr(response, 'status_code', None):
                return response

        return abort(404)

    def redirect_with_querystring(self, target):
        if request.query_string:
            target = f"{target}?{request.query_string.decode('utf-8')}"
        return forced_host_redirect(target, code=self.redirect_code)

    def datetime_to_header(self, dt):
        return time.strftime(
            HTTP_HEADER_DATE_FORMAT,
            dt.replace(tzinfo=pytz.UTC).timetuple(),
        )


    def get_file(self, key):
        return self._client.get_object(Bucket=self.bucket, Key=key)


    def retrieve(self, url, abort_on_fail=True):
        s3_url = f'{self.prefix}/{url}' if self.prefix else url

        try:
            s3_obj = self.get_file(s3_url)

            if 'ContentLength' not in s3_obj or int(s3_obj['ContentLength']) > OVERFLOW_SIZE:
                # URL only works for 60 seconds
                url = self._client.generate_presigned_url('get_object',
                          Params={
                              'Bucket': self.bucket,
                              'Key': s3_url
                          },
                          ExpiresIn=60)

                self.app.logger.info('Redirecting to S3 contents via signed URL')
                # This cannot redirect with the other status codes because it's an oversize page
                # and the URL will be different on each request. Therefore we use: "303 See Other"
                return redirect(url, 303)

            self.app.logger.info('Returning S3 contents')
            contents = s3_obj['Body'].read()
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
            self.app.logger.warning('Unable to open: {}/{}: {}'.format(self.bucket, s3_url, exc))
            pass

        if abort_on_fail:
            return abort(404)

        return None

    def setup_locales(self, file=None, locales=None):

        if file is not None:
            try:
                locales_file_obj = self.get_file(file)
                self.locales = json.loads(locales_file_obj['Body'].read())
                self.app.logger.info('Loaded locales from S3')

            except botocore.exceptions.ClientError as exc:
                self.app.logger.exception(exc)
                if exc.response['Error']['Code'] == 'NoSuchKey':
                    self.app.logger.warning(
                        f"Unable to instantiate multi-locales: {file} does not exist in S3")
                else:
                    raise

            except json.JSONDecodeError as exc:
                self.app.logger.exception(exc)

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
            self.locales = self.locales + locales

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


class FlaskS3ProxyBlueprint(FlaskS3Proxy):
    bp: Blueprint = None
    fallback: FlaskS3Proxy = None

    def __init__(self, app, boto3_client=None, bucket=None, prefix=None, paths=None, fallback=None, **kwargs):
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

    def register_blueprint(self, paths, bucket=None, prefix=None, **kwargs):
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
