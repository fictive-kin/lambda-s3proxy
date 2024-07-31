
from datetime import datetime, timedelta
import boto3


class CWLogs:
    _client = None

    def __init__(self, log_group):
        self.log_group = log_group

    @property
    def client(self):
        if not self._client:
            self._client = boto3.client('logs')

        return self._client

    def get(self, since=None, until=None):
        if until is None:
            until = datetime.utcnow().timestamp()

        if since is None:
            print(datetime.utcnow())
            delta = timedelta(minutes=30)
            since = (datetime.now() - delta).timestamp()

        if since > until:
            args = (int(until), int(since),)
        else: 
            args = (int(since), int(until),)

        kwargs = {
            'logGroupName': self.log_group,
            'startTime': args[0]*1000,
            'endTime': args[1]*1000,
            'interleaved': True,
        }

        for event in self.paginate('filter_log_events', 'events', **kwargs):
            yield event

    def get_streams(self, since, until):
        kwargs = {
            'logGroupName': self.log_group,
            'orderBy': 'LastEventTime',
            'descending': True,
        }

        for stream in self.paginate('describe_log_streams', 'logStreams', **kwargs):
            yield stream

    def paginate(self, func, resp_key, **kwargs):
        paginator = self.client.get_paginator(func)
        for page in paginator.paginate(**kwargs):
            for item in page.get(resp_key):
                yield item
