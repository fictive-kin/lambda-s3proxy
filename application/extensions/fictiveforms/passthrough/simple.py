import typing as t
from dataclasses import dataclass, field

from .base import (
    CachedToken,
    Passthrough,
    PassthroughError,
    describe_response_keys,
    get_path,
    parse_expiry,
    resolve_secret,
)


@dataclass(kw_only=True)
class SimplePassthrough(Passthrough):
    """Authenticates by POSTing a client secret to a token endpoint, which returns
    an arbitrary payload containing the token and when it expires.

    :param token_url: The token endpoint the secret is posted to.
    :param client_secret: The client secret.
    :param secret_field: The name of the field the secret is sent as.
    :param token_request_format: ``json`` or ``form``, for the token request body.
    :param token_key: Dotted path to the token within the token response.
    :param expires_key: Dotted path to the expiry within the token response. It may
        be epoch seconds or milliseconds, seconds from now, or a date string.
    """

    type_name: t.ClassVar[str] = "simple"

    token_url: str
    client_secret: str = field(repr=False)
    secret_field: str = "client_secret"
    token_request_format: str = "json"
    token_key: str = "token"
    expires_key: str = "expires_at"

    def __post_init__(self):
        super().__post_init__()
        if self.token_request_format not in ("json", "form"):
            raise ValueError(
                f"Unsupported token request format: {self.token_request_format}"
            )

    @classmethod
    def options_from_config(
        cls, options: t.Mapping[str, t.Any], app_config: t.Mapping[str, t.Any]
    ) -> t.Dict[str, t.Any]:
        return {
            "token_url": options["token_url"],
            "client_secret": resolve_secret(options["client_secret"], app_config),
            "secret_field": options.get("secret_field", "client_secret"),
            "token_request_format": options.get("token_request_format", "json"),
            "token_key": options.get("token_key", "token"),
            "expires_key": options.get("expires_key", "expires_at"),
        }

    @property
    def cache_key(self) -> t.Tuple:
        return (self.type_name, self.token_url, self.secret_field, self.client_secret)

    def exchange_token(self) -> CachedToken:
        data = {self.secret_field: self.client_secret}
        if self.token_request_format == "json":
            body = self.post_token_request(json=data)
        else:
            body = self.post_token_request(data=data)

        token = get_path(body, self.token_key)
        if not token or not isinstance(token, str):
            raise PassthroughError(
                f"Token exchange returned no token at {self.token_key} "
                f"({describe_response_keys(body)})"
            )

        return CachedToken(
            access_token=token,
            expires_at=parse_expiry(get_path(body, self.expires_key)),
        )
