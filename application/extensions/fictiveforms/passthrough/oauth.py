import time
import typing as t
from dataclasses import dataclass, field

from .base import (
    DEFAULT_TOKEN_TTL,
    CachedToken,
    Passthrough,
    PassthroughError,
    describe_response_keys,
    resolve_secret,
)


@dataclass(kw_only=True)
class OAuthPassthrough(Passthrough):
    """Authenticates with an OAuth2 client credentials token exchange.

    :param token_url: The token endpoint used to exchange the client credentials.
    :param client_id: The OAuth2 client id.
    :param client_secret: The OAuth2 client secret.
    :param scope: Optional scope to request with the token.
    :param audience: Optional audience to request with the token (Auth0 et al).
    :param auth_method: ``basic`` sends the credentials in an Authorization header,
        ``post`` sends them in the token request body.
    """

    type_name: t.ClassVar[str] = "oauth"

    token_url: str
    client_id: str
    client_secret: str = field(repr=False)
    scope: t.Optional[str] = None
    audience: t.Optional[str] = None
    auth_method: str = "basic"

    def __post_init__(self):
        super().__post_init__()
        if self.auth_method not in ("basic", "post"):
            raise ValueError(f"Unsupported auth_method: {self.auth_method}")

    @classmethod
    def options_from_config(
        cls, options: t.Mapping[str, t.Any], app_config: t.Mapping[str, t.Any]
    ) -> t.Dict[str, t.Any]:
        return {
            "token_url": options["token_url"],
            "client_id": resolve_secret(options["client_id"], app_config),
            "client_secret": resolve_secret(options["client_secret"], app_config),
            "scope": options.get("scope"),
            "audience": options.get("audience"),
            "auth_method": options.get("auth_method", "basic"),
        }

    @property
    def cache_key(self) -> t.Tuple:
        return (self.type_name, self.token_url, self.client_id, self.scope, self.audience)

    def exchange_token(self) -> CachedToken:
        data: t.Dict[str, str] = {"grant_type": "client_credentials"}
        if self.scope:
            data["scope"] = self.scope
        if self.audience:
            data["audience"] = self.audience

        auth = None
        if self.auth_method == "basic":
            auth = (self.client_id, self.client_secret)
        else:
            data["client_id"] = self.client_id
            data["client_secret"] = self.client_secret

        body = self.post_token_request(data=data, auth=auth)

        access_token = body.get("access_token") if isinstance(body, dict) else None
        if not access_token:
            raise PassthroughError(
                "Token exchange returned no access_token "
                f"({describe_response_keys(body)})"
            )

        try:
            ttl = int(body.get("expires_in") or DEFAULT_TOKEN_TTL)
        except (TypeError, ValueError):
            ttl = DEFAULT_TOKEN_TTL

        return CachedToken(access_token=access_token, expires_at=time.time() + ttl)
