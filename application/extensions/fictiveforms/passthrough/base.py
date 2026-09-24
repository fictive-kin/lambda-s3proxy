import logging
import threading
import time
import typing as t
from dataclasses import dataclass, field
from datetime import timezone

import requests
from dateutil import parser as date_parser
from flask import current_app, has_app_context


CONFIG_PREFIX = "config://"

# Refresh tokens this many seconds before they actually expire
TOKEN_EXPIRY_LEEWAY = 60

# Used when the token endpoint does not say when the token expires
DEFAULT_TOKEN_TTL = 300

DEFAULT_TIMEOUT = 10

DEFAULT_TYPE = "oauth"


def get_logger() -> logging.Logger:
    """The app logger when available, so passthrough logging follows the app's
    log level, otherwise this module's logger"""
    return current_app.logger if has_app_context() else logging.getLogger(__name__)


class PassthroughError(Exception):
    """Raised when a submission could not be delivered to the 3rd party API"""

    def __init__(self, message: str, *, status_code: t.Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def resolve_secret(
    value: t.Optional[str], config: t.Mapping[str, t.Any]
) -> t.Optional[str]:
    """Resolve ``config://KEY`` references against the app config, so that
    secrets do not need to live in the (S3 hosted) routes file."""

    if not value or not value.startswith(CONFIG_PREFIX):
        return value

    key = value.removeprefix(CONFIG_PREFIX)
    resolved = config.get(key)
    if not resolved:
        raise ValueError(f"Config key {key} is not set")
    return resolved


def parse_expiry(value: t.Any) -> float:
    """Converts an expiry value from a token response into an epoch timestamp.

    Numbers are treated as epoch milliseconds (> 1e12), epoch seconds (> 1e9), or
    otherwise as a number of seconds from now. Strings that are not numeric are
    parsed as dates, with naive dates assumed to be UTC.
    """

    now = time.time()
    if value is None or value == "" or isinstance(value, bool):
        return now + DEFAULT_TOKEN_TTL

    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            try:
                parsed = date_parser.parse(value)
            except (ValueError, OverflowError) as exc:
                raise PassthroughError(f"Unable to parse token expiry: {value}") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()

    if not isinstance(value, (int, float)):
        raise PassthroughError(f"Unable to parse token expiry: {value!r}")

    if value > 1e12:
        return value / 1000
    if value > 1e9:
        return float(value)
    return now + value


def get_path(data: t.Any, path: str) -> t.Any:
    """Looks up a dotted path (``data.auth.token``) in a nested dict"""

    for part in path.split("."):
        if not isinstance(data, t.Mapping) or part not in data:
            return None
        data = data[part]
    return data


def key_paths(data: t.Any, prefix: str = "") -> t.List[str]:
    """Lists the dotted paths of every key in a nested dict (without the values,
    which may be secret), to help find the right token/expiry path"""

    if not isinstance(data, t.Mapping):
        return []

    paths = []
    for key, value in data.items():
        path = f"{prefix}{key}"
        paths.append(path)
        paths.extend(key_paths(value, f"{path}."))
    return paths


def describe_response_keys(data: t.Any) -> str:
    if not isinstance(data, t.Mapping):
        return f"response was a {type(data).__name__}, not an object"
    return f"response keys: {key_paths(data) or 'none'}"


@dataclass
class CachedToken:
    access_token: str
    expires_at: float

    @property
    def is_valid(self) -> bool:
        return time.time() < self.expires_at - TOKEN_EXPIRY_LEEWAY


@dataclass(kw_only=True)
class Passthrough:
    """Forwards a form submission to a 3rd party API, authenticating with a token
    obtained from a token endpoint. Subclasses implement the token exchange.

    Use :meth:`from_config` to build the appropriate subclass from a route's
    ``passthrough`` configuration block, selected by its ``type`` key.

    :param url: The API endpoint the submission is sent to.
    :param method: The HTTP method used for the submission.
    :param payload_format: ``json`` or ``form``.
    :param field_map: Renames form fields (``{"form_field": "api_field"}``).
    :param include_unmapped: Whether fields missing from ``field_map`` are sent as-is.
    :param extra: Static values merged into every submission payload.
    :param headers: Additional headers sent with the submission.
    :param token_header: The header the token is sent in.
    :param token_prefix: The prefix prepended to the token in *token_header*.
    :param timeout: Timeout, in seconds, for both the token and submission requests.
    """

    type_name: t.ClassVar[str] = ""

    url: str
    method: str = "POST"
    payload_format: str = "json"
    field_map: t.Dict[str, str] = field(default_factory=dict)
    include_unmapped: bool = True
    extra: t.Dict[str, t.Any] = field(default_factory=dict)
    headers: t.Dict[str, str] = field(default_factory=dict)
    token_header: str = "Authorization"
    token_prefix: str = "Bearer "
    timeout: float = DEFAULT_TIMEOUT

    _registry: t.ClassVar[t.Dict[str, t.Type["Passthrough"]]] = {}

    # Tokens are shared across instances using the same credentials, and survive
    # between invocations of a warm Lambda container
    _token_cache: t.ClassVar[t.Dict[t.Tuple, CachedToken]] = {}
    _token_lock: t.ClassVar[threading.Lock] = threading.Lock()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.type_name:
            Passthrough._registry[cls.type_name] = cls

    def __post_init__(self):
        if self.payload_format not in ("json", "form"):
            raise ValueError(f"Unsupported payload format: {self.payload_format}")
        self.method = self.method.upper()

    def __str__(self) -> str:
        return f"{self.type_name} passthrough ({self.method} {self.url})"

    @classmethod
    def from_config(
        cls, options: t.Mapping[str, t.Any], app_config: t.Mapping[str, t.Any]
    ) -> "Passthrough":
        """Builds the subclass selected by ``options["type"]`` from a route's
        ``passthrough`` configuration block"""

        type_name = options.get("type", DEFAULT_TYPE)
        try:
            subclass = cls._registry[type_name]
        except KeyError:
            raise ValueError(f"Unknown passthrough type: {type_name}") from None

        return subclass(
            url=options["url"],
            method=options.get("method", "POST"),
            payload_format=options.get("format", "json"),
            field_map=options.get("field_map") or {},
            include_unmapped=options.get("include_unmapped", True),
            extra=options.get("extra") or {},
            headers=options.get("headers") or {},
            token_header=options.get("token_header", "Authorization"),
            token_prefix=options.get("token_prefix", "Bearer "),
            timeout=options.get("timeout", DEFAULT_TIMEOUT),
            **subclass.options_from_config(options, app_config),
        )

    @classmethod
    def options_from_config(
        cls, options: t.Mapping[str, t.Any], app_config: t.Mapping[str, t.Any]
    ) -> t.Dict[str, t.Any]:
        """Returns the subclass specific constructor arguments"""
        raise NotImplementedError

    @property
    def cache_key(self) -> t.Tuple:
        """Identifies the credentials a token was issued for"""
        raise NotImplementedError

    def exchange_token(self) -> CachedToken:
        raise NotImplementedError

    def get_token(self, *, force_refresh: bool = False) -> str:
        log = get_logger()
        with self._token_lock:
            cached = self._token_cache.get(self.cache_key)
            if cached and cached.is_valid and not force_refresh:
                log.debug(
                    "fictiveforms: %s using cached token (expires in %ds)",
                    self,
                    cached.expires_at - time.time(),
                )
                return cached.access_token

            if force_refresh:
                reason = "refresh forced"
            elif cached:
                reason = "cached token expired"
            else:
                reason = "no cached token"
            log.debug(
                "fictiveforms: %s exchanging token at %s (%s)",
                self,
                self.token_url,  # type: ignore[attr-defined]
                reason,
            )

            token = self.exchange_token()
            self._token_cache[self.cache_key] = token
            log.debug(
                "fictiveforms: %s obtained token (expires in %ds)",
                self,
                token.expires_at - time.time(),
            )
            return token.access_token

    def invalidate_token(self) -> None:
        get_logger().debug("fictiveforms: %s invalidating cached token", self)
        with self._token_lock:
            self._token_cache.pop(self.cache_key, None)

    def post_token_request(self, **kwargs) -> t.Any:
        """POSTs to the token endpoint and returns the decoded JSON body"""

        try:
            response = requests.post(
                self.token_url,  # type: ignore[attr-defined]
                headers={"Accept": "application/json"},
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise PassthroughError(f"Token exchange failed: {exc}") from exc

        get_logger().debug(
            "fictiveforms: %s token endpoint responded %s",
            self,
            response.status_code,
        )
        if not response.ok:
            raise PassthroughError(
                f"Token exchange failed with status {response.status_code}",
                status_code=response.status_code,
            )

        try:
            return response.json()
        except ValueError as exc:
            raise PassthroughError("Token exchange returned invalid JSON") from exc

    def build_payload(self, data: t.Mapping[str, t.Any]) -> t.Dict[str, t.Any]:
        payload: t.Dict[str, t.Any] = {}
        for key, value in data.items():
            if key in self.field_map:
                payload[self.field_map[key]] = value
            elif self.include_unmapped:
                payload[key] = value

        payload.update(self.extra)
        return payload

    def _request(self, payload: t.Dict[str, t.Any], token: str) -> requests.Response:
        headers = {**self.headers, self.token_header: f"{self.token_prefix}{token}"}
        kwargs: t.Dict[str, t.Any] = {"headers": headers, "timeout": self.timeout}
        if self.payload_format == "json":
            kwargs["json"] = payload
        else:
            kwargs["data"] = payload

        return requests.request(self.method, self.url, **kwargs)

    def submit(self, data: t.Mapping[str, t.Any]) -> requests.Response:
        """Sends *data* to the 3rd party API. A 401 response triggers a single
        retry with a freshly exchanged token, in case the cached one was revoked."""

        log = get_logger()
        payload = self.build_payload(data)
        # Only field names are logged, as the values are user submitted data
        log.debug(
            "fictiveforms: %s sending %s payload with fields %s",
            self,
            self.payload_format,
            sorted(payload.keys()),
        )

        try:
            response = self._request(payload, self.get_token())
            log.debug("fictiveforms: %s responded %s", self, response.status_code)
            if response.status_code == 401:
                log.debug("fictiveforms: %s rejected the token, retrying", self)
                response = self._request(payload, self.get_token(force_refresh=True))
                log.debug("fictiveforms: %s responded %s", self, response.status_code)
        except requests.RequestException as exc:
            raise PassthroughError(f"Submission failed: {exc}") from exc

        if not response.ok:
            if response.status_code == 401:
                self.invalidate_token()
            raise PassthroughError(
                f"Submission failed with status {response.status_code}",
                status_code=response.status_code,
            )

        return response
