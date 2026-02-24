import typing as t
import logging
from urllib.parse import urlparse
from uuid import uuid4

import boto3
from botocore.client import BaseClient, Config
from flask import Flask, redirect, request, Response

from ..utils import forced_host_redirect


class FlaskOverflowExtension:
    app: Flask = None  # type: ignore
    expires_in: int = 60
    _overflow_size: int = None  # type: ignore
    _overflow_bucket: str = None  # type: ignore
    _client: BaseClient = None  # type: ignore

    def __init__(
        self,
        overflow_size: int,
        *,
        app: t.Optional[Flask] = None,
        overflow_bucket: t.Optional[str] = None,
        boto3_client: t.Optional[BaseClient] = None,
    ):
        self.overflow_size = overflow_size
        self.overflow_bucket = overflow_bucket
        self.client = boto3_client

        if app is not None:
            self.init_app(app)

    def init_app(
        self,
        app: Flask,
        *,
        overflow_size: t.Optional[int] = None,
        overflow_bucket: t.Optional[str] = None,
        boto3_client: t.Optional[BaseClient] = None,
    ):
        """Initializes the extension with the flask app"""
        if overflow_bucket is not None:
            self.overflow_bucket = overflow_bucket
        if overflow_size is not None:
            self.overflow_size = overflow_size
        if boto3_client is not None:
            self.client = boto3_client

        self.app = app
        self.register_after_request()

    @property
    def logger(self):
        """Returns the appropriate logger"""
        try:
            return self.app.logger
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return logging.getLogger("flask-overflow")

    @property
    def client(self) -> BaseClient:
        """Returns the client"""
        if self._client is None:
            self._client = boto3.client("s3", config=Config(signature_version="s3v4"))

        return self._client

    @client.setter
    def client(self, value: BaseClient | None):
        """Sets the client"""
        self._client = value  # type: ignore

    @property
    def overflow_size(self) -> int:
        """Returns the overflow size limit"""

        if self._overflow_size is None:
            self._overflow_size = 1024 * 1024 * 500  # 500 MB

        return self._overflow_size

    @overflow_size.setter
    def overflow_size(self, value: int | None):
        """Sets the overflow size"""
        self._overflow_size = value  # type: ignore

    @property
    def overflow_bucket(self) -> str:
        """Returns the overflow bucket name"""

        if self._overflow_bucket is None:
            if self.app is None:
                raise ValueError("FlaskOverflowExtension was not properly initialized")

            self._overflow_bucket = self.app.config.get("OVERFLOW_BUCKET")  # type: ignore

        return self._overflow_bucket

    @overflow_bucket.setter
    def overflow_bucket(self, value: str | None):
        """Sets the overflow bucket"""
        self._overflow_bucket = value  # type:ignore

    def register_after_request(self):
        """Registers the actual after request handler"""

        @self.app.after_request
        def after_request(response: Response):
            if (
                self.overflow_bucket is not None
                and response.content_length > self.overflow_size
            ):

                self.logger.debug(
                    f"Saving to S3. The response was {response.content_length}"
                )

                # the response would be bigger than overflow_size, so instead of trying to serve it,
                # we'll put the resulting body on S3, and redirect to a (temporary, signed) URL
                # this is especially useful because API Gateway has a body size limitation, and
                # some APIs serve *huge* blobs of JSON

                # UUID filename (same suffix as original request if possible)
                u = urlparse(request.url)
                if "." in u.path:
                    filename = str(uuid4()) + "." + u.path.split(".")[-1]
                else:
                    filename = str(uuid4())

                # actually put it in the bucket. beware that boto is really noisy for this in debug log level
                # and we don't need the s3.Object that is returned by `put_bucket`.
                self.client.put_object(
                    Bucket=self.overflow_bucket,
                    Key=filename,
                    Body=response.data,
                    ContentType=response.content_type,
                )

                # URL only works for 60 seconds
                url = self.client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.overflow_bucket, "Key": filename},
                    ExpiresIn=self.expires_in,
                )

                # "see other"
                redirect_response = forced_host_redirect(url, status=303)
                for cookie in response.headers.get_all("Set-Cookie"):
                    redirect_response.headers.add("Set-Cookie", cookie)
                return redirect_response

            # otherwise, just serve it normally
            return response


class FlaskALBOverflowExtension(FlaskOverflowExtension):
    """An overflow handler for Load Balancers that has a limit of a 1 MB payload"""

    def __init__(
        self,
        *,
        app: t.Optional[Flask] = None,
        overflow_bucket: t.Optional[str] = None,
        boto3_client: t.Optional[BaseClient] = None,
    ):
        super().__init__(
            (1024 * 1024),
            app=app,
            overflow_bucket=overflow_bucket,
            boto3_client=boto3_client,
        )


class FlaskAPIGatewayOverflowExtension(FlaskOverflowExtension):
    """An overflow handler for API Gateway that has a limit of a 6 MB payload"""

    def __init__(
        self,
        *,
        app: t.Optional[Flask] = None,
        overflow_bucket: t.Optional[str] = None,
        boto3_client: t.Optional[BaseClient] = None,
    ):
        super().__init__(
            (1024 * 1024 * 6),
            app=app,
            overflow_bucket=overflow_bucket,
            boto3_client=boto3_client,
        )
