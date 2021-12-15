# -*- coding: utf-8 -*-

import json
import time

import boto3
from botocore.exceptions import ClientError
from flask import Flask, abort, Response, redirect, request
import pytz
from slugify import slugify
from wsgiref.handlers import format_date_time


# When working behind APIGateway, we have a hard limit of a 10 MB response payload and when
# running in AWS Lambda, the hard limit is lowered to 6MB. We set it to 5MB so as to be sure
# that no issues will arise
OVERFLOW_SIZE = 5 * 1024 * 1024

REDIRECT_CODE = 302

S3PROXY_OPTIONS = [
    "BUCKET",
    "PREFIX",
    "TRAILING_SLASH_REDIRECTION",
    "REDIRECT_CODE",
]


class FlaskS3Proxy:
    _client = None
    app: Flask = None
    bucket: str = None
    prefix: str = None
    trailing_slash_redirection: bool = True
    redirect_code: int = REDIRECT_CODE

    def __init__(self, app, boto3_client=None, bucket=None, prefix=None, paths=None):
        if boto3_client is None:
            self._client = boto3.client('s3')
        else:
            self._client = boto3_client

        if app:
            self.init_app(app, paths=paths)

    def init_app(self, app, paths=None):
        self.app = app

        option_prefix = 'S3PROXY_'
        for key in S3PROXY_OPTIONS:
            setattr(self, key.lower(), app.config.get(f'{option_prefix}{key}'))

        if not self.bucket:
            self.app.logger.warning(
                'S3 Bucket has not been provided. Cannot proceed with FlaskS3Proxy setup.')
            return

        self.redirect_code = int(self.redirect_code) if self.redirect_code else REDIRECT_CODE
        self.trailing_slash_redirection = bool(self.trailing_slash_redirection)

        try:
            if self.prefix.endswith('/'):
                self.prefix = self.prefix[:-1]
        except Exception:  # pylint: disable=broad-except
            pass

        if paths is not None:
            for path in paths:
                self.add_handled_route(path)

    def add_handled_route(self, rule, **kwargs):
        slug = slugify(rule)
        self.app.add_url_rule(rule, endpoint=slug, view_func=self.proxy_it, **kwargs)

    def proxy_it(self, url=None):
        if url is None:
            return self.retrieve('index.html')

        if url.endswith('/'):
            if self.trailing_slash_redirection:
                return self.redirect_with_querystring(f'/{url[:-1]}')

        # Check for:
        # - /my-page
        # - /my-page.html
        # - /my-page/index.html
        for possible in (url, f'{url}/index.html', f'{url}.html'):
            response = self.retrieve(possible, abort_on_fail=False)
            if response and response.status_code == 200:
                return response

        return abort(404)

    def redirect_with_querystring(self, target):
        if request.query_string:
            target = f"{target}?{request.query_string.decode('utf-8')}"
        return redirect(target, code=self.redirect_code)

    def datetime_to_header(self, dt):
        return format_date_time(time.mktime(dt.replace(tzinfo=pytz.UTC).timetuple()))


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
