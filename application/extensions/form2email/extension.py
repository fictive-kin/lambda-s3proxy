import typing as t
from email.utils import formataddr, parseaddr
import io
import json

from botocore.exceptions import ClientError
from flask import (
    Flask,
    jsonify,
    render_template,
    render_template_string,
    request,
    Response,
)
from flask_mail import Mail, Message
from jinja2.exceptions import TemplateNotFound
from sentry_sdk import capture_exception
from slugify import slugify

from .encrypt import EncryptedMessage


REQUIRED_CONFIG_KEYS = [
    "MAIL_SERVER",
]


class FlaskFormToEmail:
    app: Flask = None  # type: ignore
    _mail: Mail = None  # type: ignore
    _encryption_key: t.Optional[str] = None
    _recipient: t.Optional[str] = None
    _sender: t.Optional[str] = None
    _routes: t.Set[str] = None  # type: ignore

    def __init__(
        self,
        app: t.Optional[Flask] = None,
        *,
        encryption_key: t.Optional[str] = None,
        recipient: t.Optional[str] = None,
        sender: t.Optional[str] = None,
    ):
        self._routes = set()

        if app:
            self.init_app(
                app, encryption_key=encryption_key, recipient=recipient, sender=sender
            )

        else:
            if encryption_key is not None:
                self.encryption_key = encryption_key
            if recipient is not None:
                self.recipient = recipient
            if sender is not None:
                self.sender = sender

    def init_app(
        self,
        app: Flask,
        *,
        encryption_key: t.Optional[str] = None,
        recipient: t.Optional[str] = None,
        sender: t.Optional[str] = None,
        file: t.Optional[str | io.IOBase] = None,
    ):
        self.app = app
        if encryption_key is not None:
            self.encryption_key = encryption_key
        if recipient is not None:
            self.recipient = recipient
        if sender is not None:
            self.sender = sender

        for key in REQUIRED_CONFIG_KEYS:
            if not app.config.get(key):
                app.logger.warning(
                    "Missing required config key (%s), cannot init extension", key
                )
                return

        if file:
            self.process_routes_from_file(file)

        if app.config.get("FORM2EMAIL_ROUTES"):
            self.process_routes(app.config["FORM2EMAIL_ROUTES"])

    def process_routes_from_file(
        self, file: str | io.IOBase, *, encoding: t.Optional[str] = None
    ):
        """Process a JSON file of routes to create endpoints within Flask"""

        if encoding is None:
            encoding = "utf-8"

        try:
            if isinstance(file, str):
                with open(file, "r", encoding=encoding) as routesfile:
                    routes = json.load(routesfile)
            else:
                routes = json.load(file)

            self.process_routes(routes)

        except (IOError, json.JSONDecodeError) as exc:
            self.app.logger.exception(exc)
            capture_exception(exc)

    def maybe_get_s3_file(self, template: str | None) -> str | None:
        if not template or not template.startswith("s3proxy://"):
            return template

        if not self.app.extensions.get("s3_proxy"):
            raise ValueError(
                "Attempting to load an S3 template file without an S3 proxy"
            )

        template_obj = self.app.extensions["s3_proxy"].get_file(
            template.removeprefix("s3proxy://")
        )
        return template_obj["Body"].read()

    def process_routes(self, routes: t.List[t.Dict[str, str]]):
        for item in routes:
            try:
                self.add_route(
                    item["route"],
                    template=self.maybe_get_s3_file(item["template"]),
                    recipient=item.get("recipient"),
                    encryption_key=self.maybe_get_s3_file(item.get("encryption_key")),
                )

            except ClientError as exc:
                capture_exception(exc)
                if exc.response["Error"]["Code"] == "NoSuchKey":
                    self.app.logger.warning(
                        f"{item['template']} does not exist. Ignoring route: {item['route']}"
                    )

            except Exception as exc:
                capture_exception(exc)

    @property
    def mail(self) -> Mail:
        if not self._mail:
            self._mail = Mail(self.app)
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

    def send_template(self, template, *, reraise: bool = False, **kwargs) -> bool:
        data: t.Dict[str, t.Any] = {k: v for k, v in request.form.items()}

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

        # The outer try/except is to protect both attempts to render the template.
        # Do not merge them down to one try/except block with the inner one that
        # handles TemplateNotFound.
        try:
            # If there is no preexisting field named "form", add the data as
            # "form" to make it easily accessible
            if "form" not in data:
                data["form"] = data.copy()  # Use copy() to prevent recursion

            try:
                html = render_template(
                    template,
                    **data,
                )
            except TemplateNotFound:
                html = render_template_string(
                    template,
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

        msg = self.message(
            sender=sender,
            recipients=[recipient],
            html=html,
            **kwargs,
        )
        return self.send(msg, reraise=reraise)

    def message(self, **kwargs) -> Message:
        """Returns the appropriate type of message"""

        if "encryption_key" not in kwargs:
            kwargs["encryption_key"] = self.encryption_key

        return EncryptedMessage.factory(**kwargs)

    def send(self, message: Message, *, reraise: bool = False) -> bool:
        try:
            self.mail.send(message)
        except Exception as exc:
            if reraise:
                raise exc
            self.app.logger.exception(exc)
            capture_exception(exc)
            return False

        return True

    def add_route(
        self,
        route_url: str,
        *,
        template: t.Optional[str] = "email/simple.html",
        recipient: t.Optional[str] = None,
        encryption_key: t.Optional[str] = None,
    ):

        self.app.logger.debug(f"Setting up form2email: {route_url}")

        if route_url in self._routes:
            self.app.logger.error("form2email route already exists!")
            return

        self._routes.add(route_url)

        def form_to_email() -> t.Tuple[Response, int]:
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip()

            if not name or not email:
                return jsonify({"error": "name and email fields are required"}), 400

            try:
                if not self.send_template(
                    template,
                    recipient=recipient,
                    encryption_key=encryption_key,
                    reraise=True,
                ):
                    return jsonify({"error": "Failed to send message"}), 500

            except Exception as exc:
                self.app.logger.exception(exc)
                return jsonify({"error": f"Failed to send message: {exc}"}), 500

            return jsonify({"message": "Your message has been sent"}), 200

        self.app.add_url_rule(
            route_url,
            f"form2email-{slugify(route_url)}",
            form_to_email,
            methods=["POST"],
        )
        self.app.logger.debug(f"loaded: {route_url}")

    def add_test(
        self, route: str = "/form2email", *, recipient: t.Optional[str] = None
    ):
        handler = f"{route}/handler"

        @self.app.route(route)
        def test_form():
            return render_template("form2email-test.html", action_url=handler)

        self.add_route(handler, recipient=recipient)
