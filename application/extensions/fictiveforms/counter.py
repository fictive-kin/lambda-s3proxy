import typing as t

import boto3
from flask import Flask
from sentry_sdk import capture_exception


class SubmissionCounter:
    """Keeps per-route form submission counts in a DynamoDB table.

    Every operation is a no-op when no table name is configured, and failures are
    logged rather than raised, so that counting never prevents a submission.
    """

    app: Flask
    table_name: t.Optional[str]
    _dynamodb: t.Any = None
    _table: t.Any = None

    def __init__(self, app: Flask, table_name: t.Optional[str]):
        self.app = app
        self.table_name = table_name

    @property
    def enabled(self) -> bool:
        return bool(self.table_name)

    @property
    def dynamodb(self):
        if not self._dynamodb:
            self._dynamodb = boto3.resource("dynamodb")
        return self._dynamodb

    def initiate_table_creation(self) -> None:
        """Fire-and-forget table creation at app init. Does not wait for the table
        to become active; _get_table() waits for it at request time."""
        if not self.enabled:
            return

        try:
            self.dynamodb.create_table(
                TableName=self.table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            self.app.logger.info("Creating DynamoDB counter table: %s", self.table_name)
        except self.dynamodb.meta.client.exceptions.ResourceInUseException:
            # table already exists
            self.app.logger.debug(
                "fictiveforms: counter table %s already exists", self.table_name
            )
        except Exception as exc:
            self.app.logger.exception(exc)
            capture_exception(exc)

    def _get_table(self) -> t.Any:
        if self._table is not None:
            return self._table

        self.app.logger.debug(
            "fictiveforms: waiting for counter table %s to exist", self.table_name
        )
        table = self.dynamodb.Table(self.table_name)
        table.wait_until_exists()
        self._table = table
        return table

    def increment(self, counter_key: str) -> t.Optional[int]:
        """Atomically increment the submission counter for *counter_key*.

        Returns the new counter value, or None if no counter table is configured
        or if the operation fails.
        """
        if not self.enabled:
            self.app.logger.debug("fictiveforms: no counter table, not counting")
            return None

        try:
            result = self._get_table().update_item(
                Key={"id": counter_key},
                UpdateExpression="ADD #count :one",
                ExpressionAttributeNames={"#count": "count"},
                ExpressionAttributeValues={":one": 1},
                ReturnValues="UPDATED_NEW",
            )
            count = int(result["Attributes"]["count"])
            self.app.logger.debug(
                "fictiveforms: counter %s incremented to %s", counter_key, count
            )
            return count
        except Exception as exc:
            self.app.logger.exception(exc)
            capture_exception(exc)
            return None

    def decrement(self, counter_key: str) -> None:
        """Atomically decrement the submission counter for *counter_key*, rolling back
        a previously incremented value after a failed submission."""
        if not self.enabled:
            return

        try:
            self._get_table().update_item(
                Key={"id": counter_key},
                UpdateExpression="ADD #count :neg_one",
                ExpressionAttributeNames={"#count": "count"},
                ExpressionAttributeValues={":neg_one": -1},
            )
            self.app.logger.debug(
                "fictiveforms: counter %s rolled back after a failed submission",
                counter_key,
            )
        except Exception as exc:
            self.app.logger.exception(exc)
            capture_exception(exc)
