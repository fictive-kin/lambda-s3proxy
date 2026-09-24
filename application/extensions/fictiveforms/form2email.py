"""Form2Email: renders a form submission with a template and emails it, optionally
encrypted with PGP or S/MIME. Requires Flask-Mail to be configured (``MAIL_SERVER``).
"""

import typing as t
from email.utils import formataddr, parseaddr

import click
from flask import Flask, render_template, render_template_string
from flask_mail import Mail, Message
from jinja2.exceptions import TemplateNotFound
from sentry_sdk import capture_exception

from .encrypt import (
    detect_encryption_type,
    decrypt_pgp_message,
    decrypt_smime_message,
    EncryptedMessage,
)


# Config keys that must be set to be able to send email
MAIL_REQUIRED_CONFIG_KEYS = [
    "MAIL_SERVER",
]

DEFAULT_TEMPLATE = "email/simple.html"


class FormToEmail:
    app: Flask
    _mail: t.Optional[Mail] = None
    _encryption_key: t.Optional[str] = None
    _recipient: t.Optional[str] = None
    _sender: t.Optional[str] = None

    def __init__(
        self,
        app: Flask,
        *,
        encryption_key: t.Optional[str] = None,
        recipient: t.Optional[str] = None,
        sender: t.Optional[str] = None,
    ):
        self.app = app
        self.encryption_key = encryption_key
        self.recipient = recipient
        self.sender = sender

        if self.enabled:
            # Initialised eagerly, as Flask-Mail's Message relies on its state being
            # registered in app.extensions["mail"]
            self._mail = Mail(app)
        else:
            app.logger.warning(
                "Missing mail config key(s) (%s), forms cannot send email",
                ", ".join(self.missing_config_keys),
            )

    @property
    def missing_config_keys(self) -> t.List[str]:
        return [key for key in MAIL_REQUIRED_CONFIG_KEYS if not self.app.config.get(key)]

    @property
    def enabled(self) -> bool:
        return not self.missing_config_keys

    @property
    def mail(self) -> Mail:
        if not self._mail:
            raise RuntimeError(
                f"Mail is not configured ({', '.join(self.missing_config_keys)} not set)"
            )
        return self._mail

    @property
    def encryption_key(self) -> str | None:
        return self._encryption_key or self.app.config.get("FORM2EMAIL_ENCRYPTION_KEY")

    @encryption_key.setter
    def encryption_key(self, value: t.Optional[str]):
        self._encryption_key = value

    @property
    def recipient(self) -> str:
        return self._recipient or self.app.config["MAIL_RECIPIENT"]

    @recipient.setter
    def recipient(self, value: t.Optional[str]):
        self._recipient = value

    @property
    def sender(self) -> str:
        return self._sender or self.app.config["MAIL_DEFAULT_SENDER"]

    @sender.setter
    def sender(self, value: t.Optional[str]):
        if isinstance(value, str) and " " in value:
            # We only want the email address in this property
            parsed = parseaddr(value)
            self._sender = parsed[1]
        else:
            self._sender = value

    def send_template(
        self,
        template,
        form_data: t.Mapping[str, t.Any],
        *,
        reraise: bool = False,
        submission_count: t.Optional[int] = None,
        **kwargs,
    ) -> bool:
        """Renders *template* with the submitted *form_data* and emails the result"""

        data: t.Dict[str, t.Any] = dict(form_data)

        data["submission_count"] = submission_count or "unknown"

        name = data.get("name", "Form Submitted")
        if "reply_to" not in kwargs and "email" in data:
            kwargs["reply_to"] = formataddr((name, data["email"]))
        if "subject" not in kwargs:
            kwargs["subject"] = data.get("subject", "Form Submission")

        sender = formataddr(
            (
                name,
                self.sender,
            )
        )

        # If there is no preexisting field named "form", add the data as
        # "form" to make it easily accessible
        if "form" not in data:
            data["form"] = data.copy()  # Use copy() to prevent recursion

        # The outer try/except is to protect both attempts to render the template.
        # Do not merge them down to one try/except block with the inner one that
        # handles TemplateNotFound.
        try:
            try:
                rendered = render_template(
                    template,
                    **data,
                )
            except (AttributeError, TemplateNotFound):
                rendered = render_template_string(
                    (
                        template.decode("utf-8")
                        if isinstance(template, bytes)
                        else template
                    ),
                    **data,
                )
        except Exception as exc:
            if reraise:
                raise exc

            self.app.logger.exception(exc)
            capture_exception(exc)
            return False

        # Intenionally not using self.recipient as the default for pop()
        # because when the kwarg is present, but empty, we want to
        # correctly fallback to using self.recipient
        recipient = kwargs.pop("recipient", None) or self.recipient
        if "<body>" in rendered:
            kwargs.update({"html": rendered})
        else:
            kwargs.update({"body": rendered})

        self.app.logger.debug(
            "fictiveforms: form2email rendered %s email to %s",
            "html" if "html" in kwargs else "plain text",
            recipient,
        )

        msg = self.message(
            sender=sender,
            recipients=[recipient],
            **kwargs,
        )
        return self.send(msg, reraise=reraise)

    def message(self, **kwargs) -> Message:
        """Returns the appropriate type of message"""

        if "encryption_key" not in kwargs:
            kwargs["encryption_key"] = self.encryption_key

        message = EncryptedMessage.factory(**kwargs)
        self.app.logger.debug(
            "fictiveforms: form2email built %s", type(message).__name__
        )
        return message

    def send(self, message: Message, *, reraise: bool = False) -> bool:
        try:
            self.mail.send(message)
            self.app.logger.debug(
                "fictiveforms: form2email sent message via %s",
                self.app.config.get("MAIL_SERVER"),
            )
        except Exception as exc:
            if reraise:
                raise exc
            self.app.logger.exception(exc)
            capture_exception(exc)
            return False

        return True

    def register_cli(self, group: click.Group):
        """Adds the email specific commands to the extension's CLI *group*."""

        @group.command("decrypt")
        @click.argument("message", type=click.File("rb"), default="-")
        @click.option(
            "--key",
            "-k",
            "key_file",
            type=click.Path(exists=True),
            default=None,
            help="Private key file (ASCII-armored PGP or PEM for S/MIME).",
        )
        @click.option(
            "--cert",
            "-c",
            "cert_file",
            type=click.Path(exists=True),
            default=None,
            help="Certificate file (required for S/MIME decryption).",
        )
        @click.option(
            "--passphrase",
            "-p",
            default=None,
            help="Passphrase for the private key.",
        )
        @click.option(
            "--output",
            "-o",
            type=click.File("wb"),
            default="-",
            help="Output file (default: stdout).",
        )
        def decrypt_command(message, key_file, cert_file, passphrase, output):
            """Decrypt an encrypted email message.

            MESSAGE is a path to the .eml file, or - to read from stdin.
            """

            if not key_file and not cert_file:
                raise click.ClickException("Either key or cert must be provided")

            message_bytes = message.read()
            enc_type = detect_encryption_type(message_bytes)

            if enc_type is None:
                raise click.ClickException(
                    "Could not detect encryption type. Is this message encrypted?"
                )

            try:
                if enc_type in ("pgp-mime", "pgp-inline"):
                    with open(key_file, "r") as f:
                        private_key = f.read()
                    decrypted = decrypt_pgp_message(
                        message_bytes, private_key, passphrase=passphrase
                    )
                else:  # smime
                    if not cert_file:
                        raise click.ClickException(
                            "--cert is required for S/MIME decryption"
                        )
                    decrypted = decrypt_smime_message(
                        message_bytes, key_file, cert_file, passphrase=passphrase
                    )
            except click.ClickException:
                raise
            except Exception as exc:
                raise click.ClickException(str(exc)) from exc

            output.write(decrypted)
