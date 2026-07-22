# -*- coding: utf-8 -*-

import json
import random
import re
import typing as t

from flask import abort, request, Response

from ..utils import str2bool, str2json
from .s3proxy import FlaskS3Proxy


class FlaskS3VariantsProxy(FlaskS3Proxy):
    """
    Extends FlaskS3Proxy with cookie-based file variant serving.

    Variant files are stored in S3 at:
        <variants_dir>/<group_name>/<variant_name>/<original_path>

    Each user is assigned a variant per group via a cookie (<group_name>=<variant_name>).
    If no variant file exists for the assigned variant, the original file is served.

    Useful for A/B testing, member vs. non-member content, feature flags, etc.

    Variant groups can be configured via S3PROXY_VARIANTS (JSON in app config) or loaded
    from an S3-hosted JSON file via process_variants_from_file() / process_variants().
    When loaded from a file, the file data takes precedence over S3PROXY_VARIANTS.
    """

    _variant_groups_data: t.Optional[list] = None

    @property
    def variant_prefix(self):
        return self.app.config.get("S3PROXY_VARIANT_PREFIX", "variant_")

    @property
    def variants_dir(self):
        return self.app.config.get("S3PROXY_VARIANTS_DIR", "variants")

    @property
    def variant_groups(self):
        if self._variant_groups_data is not None:
            return self._variant_groups_data
        return str2json(self.app.config.get("S3PROXY_VARIANTS", "[]")) or []

    def process_variants_from_file(self, file: t.Union[str, t.IO]):
        try:
            if isinstance(file, str):
                with open(file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = json.load(file)
            self.process_variants(data)
        except (IOError, json.JSONDecodeError) as exc:
            self.app.logger.exception(exc)

    def process_variants(self, data: list):
        self._variant_groups_data = data

    @property
    def ssi_enabled(self):
        return str2bool(self.app.config.get("S3PROXY_SSI_ENABLED", False))

    def apply_ssi(self, response: Response, assignments: dict) -> Response:
        if not isinstance(response, Response) or response.status_code not in (200,):
            return response

        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            return response

        prefix = self.variant_prefix
        vars = {f"{prefix}{k.replace('-', '_')}": v for k, v in assignments.items()}

        def resolve_path(path):
            def sub(m):
                name = m.group(1) or m.group(2)
                return vars.get(name, m.group(0))

            return re.sub(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)", sub, path)

        def replace_include(m):
            path = resolve_path(m.group(1))
            included = self.retrieve(path.lstrip("/"), abort_on_fail=False)
            if included is None or not isinstance(included, Response):
                return ""
            return included.get_data(as_text=True)

        def replace_echo(m):
            return vars.get(m.group(1), "")

        body = response.get_data(as_text=True)
        body = re.sub(r'<!--#include virtual="([^"]+)" -->', replace_include, body)
        body = re.sub(r'<!--#echo var="([^"]+)" -->', replace_echo, body)
        response.set_data(body)
        return response

    def _cookie_name(self, group_name):
        return f"{self.variant_prefix}{group_name}"

    def _select_variant(self, group):
        selection_type = group.get("type", "cookie")
        if selection_type == "geo":
            return self._select_variant_by_geo(group)
        return self._select_variant_by_cookie(group)

    def _select_variant_by_cookie(self, group):
        existing = request.cookies.get(self._cookie_name(group["name"]))
        if existing and any(v["name"] == existing for v in group["variants"]):
            return existing
        variants = group["variants"]
        weights = [v.get("weight", 1) for v in variants]
        return random.choices([v["name"] for v in variants], weights=weights, k=1)[0]

    def _select_variant_by_geo(self, group):
        from .geography import DESIRED_HEADERS

        geo_field = group.get("geo_field", "country_code")

        geo_value = None
        for header, field in DESIRED_HEADERS.items():
            if field == geo_field:
                raw = request.headers.get(header)
                if raw:
                    from urllib.parse import unquote

                    geo_value = unquote(raw).lower()
                break

        fallback = group.get("default")
        for variant in group["variants"]:
            geo_values = variant.get("geo_values", [])
            if geo_value and geo_value in [v.lower() for v in geo_values]:
                return variant["name"]

        return fallback or group["variants"][0]["name"]

    def _variant_possibilities(self, possibilities, assignments):
        variant_paths = []
        for group_name, variant_name in assignments.items():
            prefix = f"{self.variants_dir}/{group_name}/{variant_name}/"
            for p in possibilities:
                variant_paths.append(f"{prefix}{p.lstrip('/')}")
        return tuple(variant_paths) + tuple(possibilities)

    def proxy_it(self, url=None):
        groups = self.variant_groups
        if not groups:
            return super().proxy_it(url)

        assignments = {g["name"]: self._select_variant(g) for g in groups}
        cookie_groups = {
            g["name"] for g in groups if g.get("type", "cookie") == "cookie"
        }

        if url is None:
            all_possibilities = self._variant_possibilities(
                ("index.html",), assignments
            )
            response = self.retrieve_from_possibilities(
                all_possibilities, check_for_trailing_slash_path=None
            )
            if response is None:
                return abort(404)
        else:
            has_trailing_slash = url.endswith("/")
            check_for_trailing_slash_path = None

            if not has_trailing_slash:
                self.app.logger.debug(f"Requested URL has no trailing slash: {url}")

                if self.trailing_slash_only:
                    possibilities = (
                        url,
                        f"{url}.html",
                    )
                    check_for_trailing_slash_path = url

                else:
                    possibilities = (
                        url,
                        f"{url}/index.html",
                        f"{url}.html",
                    )

            else:
                self.app.logger.debug(f"Requested URL has trailing slash: {url}")
                if self.trailing_slash_redirection:
                    return self.redirect_with_querystring(f"/{url[:-1]}")

                else:
                    url = url[:-1]

                possibilities = (
                    url,
                    f"{url}/index.html",
                    f"{url}.html",
                )

            all_possibilities = self._variant_possibilities(possibilities, assignments)
            response = self.retrieve_from_possibilities(
                all_possibilities,
                check_for_trailing_slash_path=check_for_trailing_slash_path,
            )

            if response is None:
                return abort(404)

        if self.ssi_enabled:
            response = self.apply_ssi(response, assignments)

        for group_name, variant_name in assignments.items():
            if group_name in cookie_groups:
                response.set_cookie(
                    self._cookie_name(group_name), variant_name, max_age=2592000
                )

        return response
