import typing as t
import hashlib
import json

import click
from cryptography.fernet import Fernet
from flask import Flask, session


def hash(string: str, algorithm: str = "sha1"):
    """
    Generates the SHA1 hexadecimal digest of a given string.
    """

    return getattr(hashlib, algorithm)(string.encode("utf-8")).hexdigest()


class FlaskEncryptedSession:
    app: Flask = None  # type: ignore
    _key: t.Optional[t.ByteString] = None

    def __init__(
        self, app: t.Optional[Flask] = None, *, key: t.Optional[t.ByteString] = None
    ):
        self.key = key

        if app:
            self.init_app(app)

    def init_app(self, app: Flask, *, key: t.Optional[t.ByteString] = None):
        self.app = app
        if key is not None:
            self.key = key

        self.register_cli(app)

    @property
    def key(self) -> t.ByteString:
        if not self._key:
            if key := self.app.config.get("ENCRYPTED_SESSION_KEY"):
                self._key = str(key).encode("utf-8")
            else:
                raise ValueError(
                    "No encryption key was found. Unable to encrypt the session values"
                )
        return self._key

    @key.setter
    def key(self, value: t.Optional[t.ByteString | str] = None):
        if isinstance(value, str):
            self._key = value.encode("utf-8")
        else:
            self._key = value

    def register_cli(self, app: Flask):
        @app.cli.group("session")
        def cli():
            """Encrypted session tools"""
            pass

        @cli.command("generate-key")
        @click.option(
            "-f",
            "--file",
            type=click.File("wb"),
            help="The file to save to, if desired",
        )
        def generate_key(file: t.Optional[t.BinaryIO] = None):
            """Generates a key to use with encryption/decryption"""

            key: t.ByteString = self.generate_key()
            if not file:
                click.echo("Your new key is:")
                click.echo(key.decode("utf-8"))
            else:
                file.write(key)
                click.echo(f"Key generated and saved to {file.name}")

        @cli.command("encrypt")
        @click.argument("string")
        def encrypt(string: str):
            click.echo("Encrypted value is:")
            click.echo(self.encrypt(string).encode("utf-8"))

        @cli.command("decrypt")
        @click.argument("string")
        def decrypt(string: str):
            click.echo("Decrypted value is:")
            click.echo(self.decrypt(string))

    @classmethod
    def generate_key(cls) -> t.ByteString:
        return Fernet.generate_key()

    @classmethod
    def from_key_file(cls, keyfile: str) -> "EncryptedSession":
        return cls(key=open(keyfile, "rb").read())

    def __getitem__(self, key: str) -> t.Any:
        value = session.get(hash(key), None)
        if value is None:
            raise KeyError(key)
        return self.decrypt(value)

    def __setitem__(self, key: str, value: t.Any):
        session[hash(key)] = self.encrypt(value)

    def __delitem__(self, key: str):
        hashed = hash(key)
        if hashed not in session:
            raise KeyError(key)
        del session[hashed]

    def __contains__(self, key: str) -> bool:
        return hash(key) in session

    def update(self, data: t.Dict[str, t.Any]):
        encrypted_data = {}
        for k, v in data.items():
            encrypted_data.update({hash(k): self.encrypt(v)})
        session.update(encrypted_data)

    def get(self, key: str, default: t.Any = None):
        value = session.get(hash(key), None)
        return self.decrypt(value) if value else default

    def fernet(self, key: t.Optional[t.ByteString] = None) -> Fernet:
        return Fernet(key or self.key)

    def encrypt_json(
        self, value: t.Any, *, key: t.Optional[t.ByteString] = None, **json_kwargs
    ) -> str:
        """Encrypts any value that is convertable to JSON"""
        return self.encrypt(json.dumps(value, **json_kwargs))

    def decrypt_json(
        self,
        value: t.ByteString | str,
        *,
        key: t.Optional[t.ByteString] = None,
        **json_kwargs,
    ) -> str:
        """Decrypts any value that was converted to JSON"""
        return json.loads(self.decrypt(value), **json_kwargs)

    def encrypt(
        self, value: t.ByteString | str, *, key: t.Optional[t.ByteString] = None
    ) -> str:
        """Encrypts a string message using the provided key."""
        # The message must be encoded to bytes before encryption
        bytes_string = value.encode("utf-8") if isinstance(value, str) else value
        return self.fernet(key).encrypt(bytes_string).decode("utf-8")

    def decrypt(
        self, value: t.ByteString | str, *, key: t.Optional[t.ByteString] = None
    ) -> str:
        """Decrypts an encrypted message using the provided key."""
        return self.fernet(key).decrypt(value).decode("utf-8")
