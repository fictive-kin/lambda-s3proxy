#
# Send a request to an arbitrary IP address and force the
# SNI field and Host HTTP header to a certain value.
#
import typing as t
import http.client
from random import randint
from urllib.parse import SplitResult, urlunsplit

import requests
from requests.structures import CaseInsensitiveDict
import urllib3
from requests.adapters import HTTPAdapter
from subnet import ip_address, IPv4Address, IPv6Address

# http.client.HTTPConnection.debuglevel = 5
# urllib3.add_stderr_logger()


class FrontingAdapter(HTTPAdapter):
    """ "Transport adapter" that allows us to use SSLv3."""

    def __init__(self, fronted_domain: str, **kwargs):
        self.fronted_domain = fronted_domain
        super(FrontingAdapter, self).__init__(**kwargs)

    def send(
        self, request: requests.PreparedRequest, *args, **kwargs
    ) -> requests.Response:
        connection_pool_kwargs = self.poolmanager.connection_pool_kw
        if self.fronted_domain:
            connection_pool_kwargs["assert_hostname"] = self.fronted_domain
        elif "assert_hostname" in connection_pool_kwargs:
            connection_pool_kwargs.pop("assert_hostname", None)
        return super(FrontingAdapter, self).send(request, *args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        server_hostname = None
        if self.fronted_domain:
            server_hostname = self.fronted_domain
        super(FrontingAdapter, self).init_poolmanager(
            server_hostname=server_hostname, *args, **kwargs
        )


class FrontingProxy:
    session: requests.Session = None  # type: ignore
    domain_name: str = None  # type: ignore
    ip_address: t.List[IPv4Address | IPv6Address] = None  # type: ignore

    def __init__(
        self,
        domain_name: str,
        ips: t.List[IPv4Address | IPv6Address | str] | IPv4Address | IPv6Address | str,
    ):
        self.domain_name = domain_name

        if not isinstance(ips, list):
            ips = [ips]

        self.ip_address = []
        for ip in ips:
            self.ip_address.append(ip_address(ip))

        self.session = requests.Session()
        self.session.mount("https://", FrontingAdapter(domain_name))

    def _pick_ip(self) -> str:
        if len(self.ip_address) == 1:
            return str(self.ip_address[0])

        return str(self.ip_address[randint(0, (len(self.ip_address) - 1))])

    def _build_url(self, path: str) -> str:
        return urlunsplit(SplitResult("https", self._pick_ip(), path or "", "", ""))

    def _add_headers(
        self,
        headers: t.Dict | CaseInsensitiveDict,
        override_host: bool = False,
    ) -> CaseInsensitiveDict:
        headers = CaseInsensitiveDict(headers)

        if override_host or "Host" not in headers:
            headers.update({"Host": self.domain_name})
        return headers

    def get(self, path: str, **kwargs) -> requests.Response:
        return self._req("get", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        return self._req("post", path, **kwargs)

    def put(self, path: str, **kwargs) -> requests.Response:
        return self._req("put", path, **kwargs)

    def patch(self, path: str, **kwargs) -> requests.Response:
        return self._req("patch", path, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        return self._req("delete", path, **kwargs)

    def _req(self, method: str, path: str, **kwargs) -> requests.Response:
        headers = self._add_headers(
            kwargs.pop("headers", None),
            override_host=kwargs.pop("override_host", False),
        )
        real_method = getattr(self.session, method.lower())
        if not real_method:
            raise requests.exceptions.RequestException(
                f"Provided method is invalid: {method}"
            )

        return real_method(self._build_url(path), headers=headers, **kwargs)


# proxy = FrontingProxy("example.com", ["<old-ip-1>", "<old-ip-2>", ...])
# resp = proxy.get(
#    "/",
#    headers={
#        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:145.0) Gecko/20100101 Firefox/145.0",
#    },
# )
# print(resp.content)
