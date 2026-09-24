"""FictiveForms: creates form submission endpoints from route configuration.

Each route validates the submission, counts it (see :mod:`.counter`), and then
hands it to any combination of:

- a passthrough to a 3rd party API (see :mod:`.passthrough`)
- form2email, which emails the submission (see :mod:`.form2email`)
"""

import io
import json
import typing as t

from botocore.exceptions import ClientError
from flask import Flask, jsonify, render_template, request, Response
from sentry_sdk import capture_exception
from slugify import slugify

from .counter import SubmissionCounter
from .form2email import DEFAULT_TEMPLATE, FormToEmail
from .passthrough import Passthrough, PassthroughError


DEFAULT_REQUIRED_FIELDS = ("name", "email")

CONFIG_PREFIX = "FICTIVEFORMS_"

# Config prefix used before the extension was renamed, still honoured as a fallback
LEGACY_CONFIG_PREFIX = "FORM2EMAIL_"


class FlaskFictiveForms:
    app: Flask = None  # type: ignore
    email: FormToEmail = None  # type: ignore
    counter: SubmissionCounter = None  # type: ignore
    _routes: t.Set[str] = None  # type: ignore
    _email_options: t.Dict[str, t.Any] = None  # type: ignore

    def __init__(
        self,
        app: t.Optional[Flask] = None,
        *,
        encryption_key: t.Optional[str] = None,
        recipient: t.Optional[str] = None,
        sender: t.Optional[str] = None,
    ):
        self._routes = set()
        self._email_options = {
            "encryption_key": encryption_key,
            "recipient": recipient,
            "sender": sender,
        }

        if app:
            self.init_app(app)

    def init_app(
        self,
        app: Flask,
        *,
        encryption_key: t.Optional[str] = None,
        recipient: t.Optional[str] = None,
        sender: t.Optional[str] = None,
        file: t.Optional[str | io.IOBase] = None,
    ):
        """
        :param encryption_key: Default key used to encrypt emails.
        :param recipient: Default email recipient.
        :param sender: Default email sender address.
        :param file: A JSON file of routes to create.
        """

        self.app = app

        email_options = {
            key: value
            for key, value in {
                "encryption_key": encryption_key,
                "recipient": recipient,
                "sender": sender,
            }.items()
            if value is not None
        }
        self.email = FormToEmail(app, **{**self._email_options, **email_options})
        self.counter = SubmissionCounter(app, self.config("COUNTER_TABLE"))
        app.logger.debug(
            "fictiveforms: init (email enabled=%s, counter table=%s)",
            self.email.enabled,
            self.counter.table_name,
        )

        if file:
            app.logger.debug("fictiveforms: loading routes from file")
            self.process_routes_from_file(file)

        if self.config("ROUTES"):
            app.logger.debug("fictiveforms: loading routes from config")
            self.process_routes(self.config("ROUTES"))

        app.logger.debug("fictiveforms: %s route(s) registered", len(self._routes))

        self.counter.initiate_table_creation()

        self.register_cli(app)

    def config(self, name: str) -> t.Any:
        """Returns the ``FICTIVEFORMS_<name>`` config value, falling back to the
        legacy ``FORM2EMAIL_<name>``"""
        value = self.app.config.get(f"{CONFIG_PREFIX}{name}")
        if value is None:
            value = self.app.config.get(f"{LEGACY_CONFIG_PREFIX}{name}")
        return value

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

        self.app.logger.debug("fictiveforms: loading %s from S3", template)
        template_obj = self.app.extensions["s3_proxy"].get_file(
            template.removeprefix("s3proxy://")
        )
        return template_obj["Body"].read()

    def process_routes(self, routes: t.List[t.Dict[str, t.Any]]):
        for item in routes:
            try:
                passthrough = None
                if item.get("passthrough"):
                    passthrough = Passthrough.from_config(
                        item["passthrough"], self.app.config
                    )

                self.add_route(
                    item["route"],
                    template=self.maybe_get_s3_file(item.get("template")),
                    recipient=item.get("recipient"),
                    encryption_key=self.maybe_get_s3_file(item.get("encryption_key")),
                    counter_key=item.get("counter_key"),
                    passthrough=passthrough,
                    required_fields=item.get("required_fields"),
                )

            except ClientError as exc:
                capture_exception(exc)
                if exc.response["Error"]["Code"] == "NoSuchKey":
                    self.app.logger.warning(
                        f"{item.get('template')} does not exist. Ignoring route: {item['route']}"
                    )

            except Exception as exc:
                self.app.logger.exception(
                    "Ignoring fictiveforms route %s: %s", item.get("route"), exc
                )
                capture_exception(exc)

    def add_route(
        self,
        route_url: str,
        *,
        template: t.Optional[str] = None,
        recipient: t.Optional[str] = None,
        encryption_key: t.Optional[str] = None,
        counter_key: t.Optional[str] = None,
        passthrough: t.Optional[Passthrough] = None,
        required_fields: t.Optional[t.Iterable[str]] = None,
    ):
        """Creates a form submission endpoint.

        When *passthrough* is provided, the submission is forwarded to the 3rd party
        API first, and an email is only sent if a *template* was also provided.
        Without *passthrough*, an email is always sent (using the default template
        when none is provided).

        *required_fields* defaults to ``name`` and ``email``, or to none for
        passthrough routes.

        Sending email requires mail to be configured. Without it, email-only routes
        are not created, and passthrough routes are created without sending email.
        """

        self.app.logger.debug("fictiveforms: setting up route %s", route_url)

        if route_url in self._routes:
            self.app.logger.error("fictiveforms: route %s already exists!", route_url)
            return

        if template is None and passthrough is None:
            template = DEFAULT_TEMPLATE

        if template and not self.email.enabled:
            if not passthrough:
                self.app.logger.error(
                    "Mail is not configured, ignoring email-only route: %s", route_url
                )
                return

            self.app.logger.error(
                "Mail is not configured, %s will only pass submissions through",
                route_url,
            )
            template = None

        self._routes.add(route_url)

        if required_fields is None:
            # The 3rd party API is responsible for validating passthrough submissions
            required_fields = () if passthrough else DEFAULT_REQUIRED_FIELDS
        required_fields = tuple(required_fields)

        resolved_counter_key = counter_key or slugify(route_url)

        self.app.logger.debug(
            "fictiveforms: route %s -> passthrough=%s, email=%s (recipient=%s), "
            "required fields=%s, counter key=%s",
            route_url,
            passthrough or "none",
            bool(template),
            (recipient or "default") if template else "n/a",
            list(required_fields) or "none",
            resolved_counter_key,
        )

        def handle_submission() -> t.Tuple[Response, int]:
            log = self.app.logger
            # Only field names are logged, as the values are user submitted data
            log.debug(
                "fictiveforms: %s received submission with fields %s",
                route_url,
                sorted(request.form.keys()),
            )

            missing = [
                key for key in required_fields if not request.form.get(key, "").strip()
            ]
            if missing:
                log.debug(
                    "fictiveforms: %s rejected, missing required fields %s",
                    route_url,
                    missing,
                )
                noun = "fields are" if len(missing) > 1 else "field is"
                return (
                    jsonify({"error": f"{' and '.join(missing)} {noun} required"}),
                    400,
                )

            form_data = request.form.to_dict()
            count = self.counter.increment(resolved_counter_key)

            if passthrough:
                log.debug("fictiveforms: %s submitting to %s", route_url, passthrough)
                try:
                    passthrough.submit(form_data)
                except PassthroughError as exc:
                    self.counter.decrement(resolved_counter_key)
                    self.app.logger.exception(exc)
                    capture_exception(exc)
                    return jsonify({"error": "Failed to submit form"}), 502
                log.debug("fictiveforms: %s passthrough succeeded", route_url)

            if template:
                log.debug("fictiveforms: %s sending email", route_url)
                try:
                    sent = self.email.send_template(
                        template,
                        form_data,
                        recipient=recipient,
                        encryption_key=encryption_key,
                        submission_count=count,
                        reraise=True,
                    )
                    error = None if sent else "Failed to send message"
                except Exception as exc:
                    self.app.logger.exception(exc)
                    error = f"Failed to send message: {exc}"

                if error:
                    if passthrough:
                        # The 3rd party API already accepted the submission, so it
                        # must not be reported as failed (and potentially resubmitted)
                        self.app.logger.error(
                            "fictiveforms: passthrough succeeded but email failed for %s",
                            route_url,
                        )
                    else:
                        self.counter.decrement(resolved_counter_key)
                        return jsonify({"error": error}), 500
                else:
                    log.debug("fictiveforms: %s email sent", route_url)

            response: t.Dict[str, t.Any] = {
                "message": "Your message has been sent",
                "submission_count": count or 0,
            }
            log.debug("fictiveforms: %s submission complete (count=%s)", route_url, count)

            return jsonify(response), 200

        self.app.add_url_rule(
            route_url,
            f"fictiveforms-{slugify(route_url)}",
            handle_submission,
            methods=["POST"],
        )
        self.app.logger.debug("fictiveforms: loaded %s", route_url)

    def register_cli(self, app: Flask):
        """Register the ``flask fictiveforms`` CLI command group on *app*."""

        @app.cli.group("fictiveforms")
        def fictiveforms_group():
            """FictiveForms management commands."""

        self.email.register_cli(fictiveforms_group)

    def add_test(
        self, route: str = "/form2email", *, recipient: t.Optional[str] = None
    ):
        """Adds a test form, at *route*, that submits to an email-only route"""

        if not self.email.enabled:
            self.app.logger.warning("Mail is not configured, not adding test form")
            return

        handler = f"{route}/handler"

        @self.app.route(route)
        def test_form():
            return render_template("form2email-test.html", action_url=handler)

        self.add_route(handler, recipient=recipient)
