# -*- coding: utf-8 -*-

from flask import abort, Response, redirect
import boto3
from botocore.exceptions import ClientError
import json
import logging
import os
import pytz
import time
from wsgiref.handlers import format_date_time

from application.lib.logging import setup_logging

OVERFLOW_SIZE = int(os.environ.get("OVERFLOW_SIZE", 5 * 1024 * 1024)) # 5MB

class S3Proxy(object):

    def __init__(self, flask_app, s3_client=None, s3_bucket=None, s3_prefix=None):
        self.app = flask_app
        self.logger = setup_logging(__name__, level=logging.INFO)

        if s3_client is not None:
            self.s3_client = s3_client
        else:
            self.s3_client = boto3.client('s3')

        if s3_bucket is not None:
            self.s3_bucket = s3_bucket
        else:
            self.s3_bucket = self.app.config.get('S3_BUCKET', False)

        if s3_prefix is not None:
            self.s3_prefix = s3_prefix
        else:
            self.s3_prefix = self.app.config.get('S3_PREFIX', None)

        try:
            if self.s3_prefix.endswith('/'):
                self.s3_prefix = self.s3_prefix[:-1]
        except Exception:
            pass


    def datetime_to_header(self, dt):
        return format_date_time(time.mktime(dt.replace(tzinfo=pytz.UTC).timetuple()))


    def get_file(self, key):
        return self.s3_client.get_object(Bucket=self.s3_bucket, Key=key)


    def retrieve(self, url, abort_on_fail=True):
        if self.s3_bucket:
            if url.endswith('/'):
                s3_url = '{}index.html'.format(url)
            else:
                s3_url = url

            if self.s3_prefix:
                s3_url = '{}/{}'.format(self.s3_prefix, s3_url)

            try:
                s3_obj = self.get_file(s3_url)

                if 'ContentLength' not in s3_obj or int(s3_obj['ContentLength']) > OVERFLOW_SIZE:
                    # URL only works for 60 seconds
                    url = self.s3_client.generate_presigned_url('get_object',
                              Params={
                                  'Bucket': self.s3_bucket,
                                  'Key': s3_url
                              },
                              ExpiresIn=60)

                    self.logger.info('Reirecting to S3 contents via signed URL')
                    # 303 = "see other"
                    return redirect(url, 303)

                self.logger.info('Returning S3 contents')
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

            except Exception as e:
                self.logger.debug('Unable to open: {}/{}: {}'.format(self.s3_bucket, s3_url, e))
                pass

        if abort_on_fail:
            return abort(404)

        return None
