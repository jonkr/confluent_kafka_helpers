import io
import traceback
import opentracing
import socket
from functools import partial
from typing import Callable

import structlog
from confluent_kafka import Consumer, KafkaError, KafkaException
from confluent_kafka.avro import AvroConsumer as ConfluentAvroConsumer

from confluent_kafka_helpers.callbacks import (
    default_error_cb, default_stats_cb, get_callback
)
from confluent_kafka_helpers.exceptions import (
    EndOfPartition, KafkaTransportError
)
from confluent_kafka_helpers.message import Message
from confluent_kafka_helpers.metrics import base_metric, statsd
from confluent_kafka_helpers.utils import retry_exception

logger = structlog.get_logger(__name__)


@retry_exception(exceptions=[KafkaTransportError])
def get_message(consumer, error_handler, timeout=0.1, stop_on_eof=False):
    message = consumer.poll(timeout=timeout)
    if message is None:
        return None

    if message.error():
        try:
            error_handler(message.error())
        except EndOfPartition:
            if stop_on_eof:
                raise
            else:
                return None

    return message


def default_error_handler(kafka_error):
    code = kafka_error.code()
    if code == KafkaError._PARTITION_EOF:
        raise EndOfPartition
    elif code == KafkaError._TRANSPORT:
        statsd.increment(f'{base_metric}.consumer.message.count.error')
        raise KafkaTransportError(kafka_error)
    else:
        statsd.increment(f'{base_metric}.consumer.message.count.error')
        raise KafkaException(kafka_error)


class AvroConsumer:

    DEFAULT_CONFIG = {
        'client.id': socket.gethostname(),
        'default.topic.config': {
            'auto.offset.reset': 'earliest'
        },
        'enable.auto.commit': False,
        'fetch.wait.max.ms': 1000,
        'fetch.min.bytes': 10000,
        'log.connection.close': False,
        'log.thread.name': False,
    }

    def __init__(
        self, config, get_message: Callable = get_message,
        error_handler: Callable = default_error_handler
    ) -> None:
        stop_on_eof = config.pop('stop_on_eof', False)
        poll_timeout = config.pop('poll_timeout', 0.1)
        self.non_blocking = config.pop('non_blocking', False)

        self.config = {**self.DEFAULT_CONFIG, **config}
        self.config['error_cb'] = get_callback(
            config.pop('error_cb', None), default_error_cb
        )
        self.config['stats_cb'] = get_callback(
            config.pop('stats_cb', None), default_stats_cb
        )
        self.topics = self._get_topics(self.config)

        logger.info("Initializing consumer", config=self.config)
        self.consumer = ConfluentAvroConsumer(self.config)
        self.consumer.subscribe(self.topics)

        self._generator = self._message_generator()

        self._get_message = partial(
            get_message, consumer=self.consumer, error_handler=error_handler,
            timeout=poll_timeout, stop_on_eof=stop_on_eof
        )
        self.tracer = opentracing.global_tracer()

    def __getattr__(self, name):
        return getattr(self.consumer, name)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._generator)
        except EndOfPartition:
            raise StopIteration

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        # the only reason a consumer exits is when an
        # exception is raised.
        #
        # close down the consumer cleanly accordingly:
        #  - stops consuming
        #  - commit offsets (only on auto commit)
        #  - leave consumer group
        logger.info("Closing consumer")
        self.consumer.close()

        span = self.tracer.active_span
        if not span:
            return

        buff = io.StringIO()
        traceback.print_exception(
            exc_type, exc_value, exc_tb, file=buff, limit=20
        )
        tb = buff.getvalue()

        span.set_tag(opentracing.tags.ERROR, True)
        span.log_kv(
            {
                "event": opentracing.tags.ERROR,
                "error.kind": exc_type.__name__,
                "error.object": exc_value,
                "stack": tb,
            }
        )
        span.finish()

    def _message_generator(self):
        while True:
            message = self._get_message()
            if message is None:
                if self.non_blocking:
                    yield None
                continue
            headers = message.headers()
            if headers:
                headers = {
                    k: v.decode('utf-8')
                    for k, v in dict(headers).items()
                }
                tags = {
                    opentracing.tags.SPAN_KIND: opentracing.tags.
                    SPAN_KIND_CONSUMER,
                    opentracing.tags.COMPONENT: 'messagebus',
                    opentracing.tags.PEER_SERVICE: 'kafka',
                    opentracing.tags.MESSAGE_BUS_DESTINATION: message.topic(),
                    'message_bus.key': message.key(),
                }
                operation_name = 'kafka.consume'
                try:
                    parent_context = self.tracer.extract(
                        opentracing.Format.TEXT_MAP, headers
                    )
                except (
                    opentracing.InvalidCarrierException,
                    opentracing.SpanContextCorruptedException,
                ):
                    span = self.tracer.start_active_span(operation_name).span
                else:
                    span = self.tracer.start_span(
                        operation_name,
                        references=[opentracing.follows_from(parent_context)],
                        tags=tags
                    )
            statsd.increment(f'{base_metric}.consumer.message.count.total')
            yield Message(message)
            if span:
                span.finish()

    def _get_topics(self, config):
        topics = config.pop('topics', None)
        assert topics is not None, "You must subscribe to at least one topic"

        if not isinstance(topics, list):
            topics = [topics]

        return topics

    @property
    def is_auto_commit(self):
        return self.config.get('enable.auto.commit', True)

    def commit(self, *args, **kwargs):
        tags = {
            opentracing.tags.SPAN_KIND: opentracing.tags.SPAN_KIND_CONSUMER,
            opentracing.tags.COMPONENT: 'messagebus',
            opentracing.tags.PEER_SERVICE: 'kafka',
        }
        span = self.tracer.start_span('kafka.commit', tags=tags)
        self.consumer.commit(*args, **kwargs)
        span.finish()


class AvroLazyConsumer(ConfluentAvroConsumer):
    """
    By default the Confluent AvroConsumer decode all messages in the partition.

    This consumer uses a lazy approach, it doesn't decode the messages, just
    provide the methods so we can do it manually.

    We use this approach, because we want to check the key messages before
    decoding the message, this will avoid performance issues.
    """
    def poll(self, timeout=None):
        if timeout is None:
            timeout = -1

        # We use the Consumer.poll because we want to avoid the
        # ConfluentAvroConsumer.poll, the later is doing the decode
        # of all the messages and we want to have a lazy approach
        message = Consumer.poll(self, timeout)
        return message

    def decode_message(self, message):
        if not message.error():
            if message.value() is not None:
                decoded_value = self._serializer.decode_message(message.value())
                message.set_value(decoded_value)

            if message.key() is not None:
                decoded_key = self._serializer.decode_message(message.key())
                message.set_key(decoded_key)
        return message
