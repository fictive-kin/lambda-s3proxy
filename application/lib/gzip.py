# -*- coding: utf-8 -*-

from flask import request, current_app
import gzip
import io

class GZipMiddleware(object):

    def __init__(self, app):
        self.app = app
        self.app.before_request(self._before_request)
        self.app.after_request(self._after_request)

    def _before_request(self):
        content_encoding = request.headers.get('Content-Encoding', '')
        if content_encoding == 'gzip':
            gz = request.get_data()
            zb = io.BytesIO(gz)
            zf = gzip.GzipFile(fileobj=zb)
            clear = zf.read()
            request._cached_data = clear

    def _after_request(self, response):
        response.headers['Vary'] = 'Accept-Encoding, Content-Encoding'
        accept_encoding = request.headers.get('Accept-Encoding', '')

        if current_app.config.get('DISABLE_GZIP', False):
            self.app.logger.debug('Not gzipping response as per app config')
            return response

        if 'gzip' not in accept_encoding.lower():
            self.app.logger.debug('Not gzipping response as per request')
            return response

        response.direct_passthrough = False

        if (response.status_code < 200 or
            response.status_code >= 300):
            self.app.logger.debug('Not gzipping response due to status code')
            return response

        gzip_buffer = io.BytesIO()
        gzip_file = gzip.GzipFile(mode='wb', 
                                  fileobj=gzip_buffer)
        gzip_file.write(response.data)
        gzip_file.close()

        response.data = gzip_buffer.getvalue()
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Content-Length'] = len(response.data)

        return response
