import typing as t
from random import randint

from flask import request, Response
from subnet import IPv4Address, IPv6Address
from user_agents import parse

from ..lib.fronting import FrontingProxy


EXCLUDED_HEADERS = set(
    [
        "Authorization",
        "Connection",
        "Keep-Alive",
        "Proxy-Authenticate",
        "Proxy-Authorization",
        "TE",
        "Trailer",
        "Transfer-Encoding",
        "Upgrade",
        # plus exclude Content-Length because requests calculates it
        "Content-Length",
    ]
)


def headers2dict(headers) -> t.Dict[str, str]:
    return {k: v for (k, v) in headers.items() if k not in EXCLUDED_HEADERS}


class FlaskGradualSwitchoverProxy:
    fronting_proxy: FrontingProxy = None  # type: ignore
    percentage_on_new: int = None  # type: ignore
    _cookie_name: t.Optional[str] = None

    def __init__(
        self,
        domain_name: str,
        ips: t.List[IPv4Address | IPv6Address | str],
        percentage_on_new: t.Optional[int] = None,
        cookie_name: t.Optional[str] = None,
    ):
        if not domain_name or not ips:
            raise ValueError(
                "GradualSwitchingProxy requires both a domain name and the IPs to use for connecting"
            )

        self.fronting_proxy = FrontingProxy(domain_name, ips)
        self.percentage_on_new = percentage_on_new or 100
        self.cookie_name = cookie_name

    @property
    def cookie_name(self) -> str:
        """Returns the name of the cookie"""
        return self._cookie_name or "switched-over"

    @cookie_name.setter
    def cookie_name(self, value):
        """Sets the name of the cookie"""
        self._cookie_name = value

    def selector(self, new: t.Callable, url: str) -> Response:
        """Determine if we should use the new view func provider or the old"""

        if self.percentage_on_new >= 100:
            return new(url)

        user_agent = parse(request.headers.get("User-Agent", ""))
        if user_agent.is_bot():
            return self.proxy_it(url)

        probability = request.cookies.get(self.cookie_name, randint(1, 100), type=int)
        resp = new(url) if probability < self.percentage_on_new else self.proxy_it(url)
        # Cookie expires in 30 days
        resp.set_cookie(self.cookie_name, str(probability), max_age=2592000)
        return resp

    def proxy_it(self, url: t.Optional[str] = None) -> Response:
        method = getattr(self.fronting_proxy, request.method.lower())
        req = method(
            url,
            params=request.args,
            data=request.get_data(),
            headers=headers2dict(request.headers),
            override_host=True,
        )

        content = req.content
        content_type = req.headers.get("Content-Type")
        headers = headers2dict(req.headers)

        # if self.overflow_bucket is not None and len(content) > self.overflow_size:

        #    self.logger.debug('Saving to S3, as the response was huge')

        #    # the response would be bigger than overflow_size, so instead of trying to serve it,
        #    # we'll put the resulting body on S3, and redirect to a (temporary, signed) URL
        #    # this is especially useful because API Gateway has a body size limitation, and
        #    # some APIs serve *huge* blobs of JSON

        #    # UUID filename (same suffix as original request if possible)
        #    u = urlparse(target_url)
        #    if '.' in u.path:
        #        filename = str(uuid4()) + '.' + u.path.split('.')[-1]
        #    else:
        #        filename = str(uuid4())

        #    s3 = boto3.resource('s3')
        #    s3_client = boto3.client('s3', config=Config(signature_version='s3v4'))

        #    bucket = s3.Bucket(self.overflow_bucket)

        #    # actually put it in the bucket. beware that boto is really noisy for this in debug log level
        #    # and we don't need the s3.Object that is returned by `put_bucket`.
        #    bucket.put_object(
        #        Key=filename,
        #        Body=content,
        #        ACL='authenticated-read',
        #        ContentType=content_type
        #    )

        #    # URL only works for 60 seconds
        #    url = s3_client.generate_presigned_url(
        #        'get_object',
        #        Params = {
        #            'Bucket': self.overflow_bucket,
        #            'Key': filename
        #        },
        #        ExpiresIn=CACHE_TTL)

        #    # "see other"
        #    return redirect(url, 303)

        # otherwise, just serve it normally
        resp = Response(content, headers=headers, content_type=content_type)
        resp.status_code = req.status_code
        return resp
