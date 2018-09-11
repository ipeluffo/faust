"""Monitor using datadog."""
import re
from datadog.dogstatsd import DogStatsd
from time import monotonic
from typing import Any, Dict, Optional, Pattern, cast

from mode.utils.objects import cached_property

from faust.exceptions import ImproperlyConfigured
from faust.sensors.monitor import Monitor, TPOffsetMapping
from faust.types import CollectionT, EventT, Message, StreamT, TP
from faust.types.transports import ConsumerT, ProducerT

try:
    import datadog
except ImportError:
    datadog = None

__all__ = ['DatadogStatsMonitor']

# This regular expression is used to generate stream ids in Statsd.
# It converts for example
#    "Stream: <Topic: withdrawals>"
# -> "Stream_Topic_withdrawals"
#
# See StatsdMonitor._normalize()
RE_NORMALIZE = re.compile(r'[\<\>:\s]+')
RE_NORMALIZE_SUBSTITUTION = '_'


class DatadogStatsClient:
    """Statsd compliant datadog client

    """

    def __init__(self,
                 host: str = 'localhost',
                 port: int = 8125,
                 prefix: str = 'faust-app',
                 rate: float = 1.0,
                 **kwargs: Any) -> None:
        self.client = DogStatsd(host=host, port=port, namespace=prefix, **kwargs)
        self.rate = rate
        self.sanitize_re = re.compile("[^0-9a-zA-Z_]")
        self.re_substitution = "_"

    def gauge(self, metric, value, labels=None):
        self.client.gauge(
            metric,
            value=value,
            tags=self._encode_labels(labels),
            sample_rate=self.rate
        )

    def increment(self, metric, value=1, labels=None):
        self.client.increment(
            metric,
            value=value,
            tags=self._encode_labels(labels),
            sample_rate=self.rate
        )

    def incr(self, metric, count=1):
        """ Statsd compatibility. """
        return self.increment(metric, value=count)

    def decrement(self, metric, value=1.0, labels=None):
        return self.client.decrement(
            metric,
            value=value,
            tags=self._encode_labels(labels),
            sample_rate=self.rate
        )

    def decr(self, metric, count=1.0):
        """ Statsd compatibility. """
        return self.decrement(metric, value=count)

    def timing(self, metric, value, labels=None):
        self.client.timing(
            metric,
            value=value,
            tags=self._encode_labels(labels),
            sample_rate=self.rate
        )

    def timed(self, metric=None, labels=None, use_ms=None):
        return self.client.timed(
            metric=metric,
            tags=self._encode_labels(labels),
            sample_rate=self.rate,
            use_ms=use_ms
        )

    def histogram(self, metric, value, labels=None):
        self.client.histogram(
            metric,
            value=value,
            tags=self._encode_labels(labels),
            sample_rate=self.rate
        )

    def _encode_labels(self, labels):
        def sanitize(s):
            return self.sanitize_re.sub(self.re_substitution, str(s))

        return [f"{sanitize(k)}:{sanitize(v)}"
                for k, v in labels.items()] if labels else None


class DatadogStatsMonitor(Monitor):
    """Datadog Faust Sensor.

    This sensor, records statistics to datadog agents along
    with computing metrics for the stats server
    """

    host: str
    port: int
    prefix: str

    def __init__(self,
                 host: str = 'localhost',
                 port: int = 8125,
                 prefix: str = 'faust-app',
                 rate: float = 1.0,
                 **kwargs: Any) -> None:
        self.host = host
        self.port = port
        self.prefix = prefix
        self.rate = rate
        if datadog is None:
            raise ImproperlyConfigured(
                'DatadogStatsMonitor requires `pip install datadog`.')
        super().__init__(**kwargs)

    def _new_datadog_stats_client(self) -> DatadogStatsClient:
        return DatadogStatsClient(
            host=self.host, port=self.port, prefix=self.prefix, rate=self.rate)

    def on_message_in(self, tp: TP, offset: int, message: Message) -> None:
        super().on_message_in(tp, offset, message)
        labels = self._format_label(tp)
        self.client.increment('messages_received', labels=labels)
        self.client.increment('messages_active', labels=labels)
        self.client.gauge('read_offset', offset, labels=labels)

    def on_stream_event_in(self, tp: TP, offset: int, stream: StreamT,
                           event: EventT) -> None:
        super().on_stream_event_in(tp, offset, stream, event)
        labels = self._format_label(tp, stream)
        self.client.increment('events', labels=labels)
        self.client.increment('events_active', labels=labels)

    def on_stream_event_out(self, tp: TP, offset: int, stream: StreamT,
                            event: EventT) -> None:
        super().on_stream_event_out(tp, offset, stream, event)
        labels = self._format_label(tp, stream)
        self.client.decrement('events_active', labels=labels)
        self.client.timing(
            'events_runtime',
            self._time(self.events_runtime[-1]),
            labels=labels
        )

    def on_message_out(self,
                       tp: TP,
                       offset: int,
                       message: Message) -> None:
        super().on_message_out(tp, offset, message)
        self.client.decrement('messages_active',
                              labels=self._format_label(tp))

    def on_table_get(self, table: CollectionT, key: Any) -> None:
        super().on_table_get(table, key)
        self.client.increment(
            'table_keys_retrieved',
            labels=self._format_label(table=table)
        )

    def on_table_set(self, table: CollectionT, key: Any, value: Any) -> None:
        super().on_table_set(table, key, value)
        self.client.increment(
            'table_keys_updated',
            labels=self._format_label(table=table)
        )

    def on_table_del(self, table: CollectionT, key: Any) -> None:
        super().on_table_del(table, key)
        self.client.increment(
            'table_keys_deleted',
            labels=self._format_label(table=table),
        )

    def on_commit_completed(self, consumer: ConsumerT, state: Any) -> None:
        super().on_commit_completed(consumer, state)
        self.client.timing(
            'commit_latency',
            self._time(monotonic() - cast(float, state)),
        )

    def on_send_initiated(self, producer: ProducerT, topic: str,
                          keysize: int, valsize: int) -> Any:
        self.client.increment(
            'topic_messages_sent',
            labels={'topic': topic}
        )
        return super().on_send_initiated(producer, topic, keysize, valsize)

    def on_send_completed(self, producer: ProducerT, state: Any) -> None:
        super().on_send_completed(producer, state)
        self.client.increment('messages_sent')
        self.client.timing(
            'send_latency',
            self._time(monotonic() - cast(float, state))
        )

    def count(self, metric_name: str, count: int = 1) -> None:
        super().count(metric_name, count=count)
        self.client.increment(metric_name, value=count)

    def on_tp_commit(self, tp_offsets: TPOffsetMapping) -> None:
        super().on_tp_commit(tp_offsets)
        for tp, offset in tp_offsets.items():
            self.client.gauge('committed_offset', offset, labels=self._format_label(tp))

    def track_tp_end_offset(self, tp: TP, offset: int) -> None:
        super().track_tp_end_offset(tp, offset)
        self.client.gauge('end_offset', offset, labels=self._format_label(tp))

    def _normalize(self, name: str,
                   *,
                   pattern: Pattern = RE_NORMALIZE,
                   substitution: str = RE_NORMALIZE_SUBSTITUTION) -> str:
        return pattern.sub(substitution, name)

    def _time(self, time: float) -> float:
        return time * 1000.

    def _stream_label(self, stream: StreamT) -> str:
        return self._normalize(
            stream.shortlabel.lstrip('Stream:'),
        ).strip('_').lower()

    def _format_label(self, tp: Optional[TP]=None,
                      stream: Optional[StreamT]=None,
                      table: Optional[CollectionT]=None) -> Dict:
        labels = dict()
        if tp is not None:
            labels.update({
                'topic': tp.topic,
                'partition': tp.partition,
            })
        if stream is not None:
            labels.update({
                'stream': self._stream_label(stream),
            })
        if table is not None:
            labels.update({
                'table': table.name,
            })
        return labels

    @cached_property
    def client(self) -> DatadogStatsClient:
        return self._new_datadog_stats_client()
