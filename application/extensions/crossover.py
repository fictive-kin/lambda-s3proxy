import typing as t
import logging
from random import randint

from flask import current_app, request, Response
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
    bots_on_new: t.Optional[bool] = None
    _cookie_name: t.Optional[str] = None

    def __init__(
        self,
        domain_name: str,
        ips: t.List[IPv4Address | IPv6Address | str],
        *,
        percentage_on_new: t.Optional[int] = None,
        bots_on_new: t.Optional[bool] = None,
        cookie_name: t.Optional[str] = None,
    ):
        if not domain_name or not ips:
            raise ValueError(
                "GradualSwitchingProxy requires both a domain name and the IPs to use for connecting"
            )

        self.fronting_proxy = FrontingProxy(domain_name, ips)
        self.percentage_on_new = percentage_on_new or 100
        self.bots_on_new = bots_on_new
        self.cookie_name = cookie_name

    @property
    def cookie_name(self) -> str:
        """Returns the name of the cookie"""
        return self._cookie_name or "switched-over"

    @cookie_name.setter
    def cookie_name(self, value):
        """Sets the name of the cookie"""
        self._cookie_name = value

    @property
    def logger(self) -> logging.Logger:
        """Returns a logger"""
        try:
            return current_app.logger
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return logging.getLogger("switchover-proxy")

    def selector(self, new: t.Callable, url: t.Optional[str] = None) -> Response:
        """Determine if we should use the new view func provider or the old"""

        if self.percentage_on_new >= 100:
            return new(url)

        if self.bots_on_new is not None:
            user_agent = parse(request.headers.get("User-Agent", ""))
            if user_agent.is_bot:
                self.logger.debug("request is a bot")
                return new(url) if self.bots_on_new else self.proxy_it(url)

        probability = request.cookies.get(self.cookie_name, randint(1, 100), type=int)
        resp = new(url) if probability < self.percentage_on_new else self.proxy_it(url)
        # Cookie expires in 30 days
        resp.set_cookie(self.cookie_name, str(probability), max_age=2592000)
        return resp

    def proxy_it(self, url: t.Optional[str] = None) -> Response:
        self.logger.debug(f"Using old site for: {url or '/'}")
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

        resp = Response(content, headers=headers, content_type=content_type)
        resp.status_code = req.status_code
        return resp
