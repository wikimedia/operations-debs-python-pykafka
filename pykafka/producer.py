from __future__ import division
"""
Author: Emmett Butler, Keith Bourgoin
"""
__license__ = """
Copyright 2015 Parse.ly, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
__all__ = ["Producer"]
from collections import deque
import logging
import sys
import threading
import time
import traceback
import weakref

from .common import CompressionType
from .exceptions import (
    ERROR_CODES,
    KafkaException,
    InvalidMessageSize,
    MessageSizeTooLarge,
    NotLeaderForPartition,
    ProducerQueueFullError,
    ProducerStoppedException,
    SocketDisconnectedError,
)
from .partitioners import random_partitioner
from .protocol import Message, ProduceRequest
from .utils.compat import iteritems, range, itervalues, Queue

log = logging.getLogger(__name__)


class Producer(object):
    """Implements asynchronous producer logic similar to the JVM driver.

    It creates a thread of execution for each broker that is the leader of
    one or more of its topic's partitions. Each of these threads (which may
    use `threading` or some other parallelism implementation like `gevent`)
    is associated with a queue that holds the messages that are waiting to be
    sent to that queue's broker.
    """
    def __init__(self,
                 cluster,
                 topic,
                 partitioner=random_partitioner,
                 compression=CompressionType.NONE,
                 max_retries=3,
                 retry_backoff_ms=100,
                 required_acks=1,
                 ack_timeout_ms=10 * 1000,
                 max_queued_messages=100000,
                 min_queued_messages=70000,
                 linger_ms=5 * 1000,
                 block_on_queue_full=True,
                 sync=False,
                 delivery_reports=False):
        """Instantiate a new AsyncProducer

        :param cluster: The cluster to which to connect
        :type cluster: :class:`pykafka.cluster.Cluster`
        :param topic: The topic to which to produce messages
        :type topic: :class:`pykafka.topic.Topic`
        :param partitioner: The partitioner to use during message production
        :type partitioner: :class:`pykafka.partitioners.BasePartitioner`
        :param compression: The type of compression to use.
        :type compression: :class:`pykafka.common.CompressionType`
        :param max_retries: How many times to attempt to produce a given batch of
            messages before raising an error.
        :type max_retries: int
        :param retry_backoff_ms: The amount of time (in milliseconds) to
            back off during produce request retries.
        :type retry_backoff_ms: int
        :param required_acks: The number of other brokers that must have
            committed the data to their log and acknowledged this to the leader
            before a request is considered complete
        :type required_acks: int
        :param ack_timeout_ms: The amount of time (in milliseconds) to wait for
            acknowledgment of a produce request.
        :type ack_timeout_ms: int
        :param max_queued_messages: The maximum number of messages the producer
            can have waiting to be sent to the broker. If messages are sent
            faster than they can be delivered to the broker, the producer will
            either block or throw an exception based on the preference specified
            with block_on_queue_full.
        :type max_queued_messages: int
        :param min_queued_messages: The minimum number of messages the producer
            can have waiting in a queue before it flushes that queue to its
            broker (must be greater than 0).
        :type min_queued_messages: int
        :param linger_ms: This setting gives the upper bound on the delay for
            batching: once the producer gets min_queued_messages worth of
            messages for a broker, it will be sent immediately regardless of
            this setting.  However, if we have fewer than this many messages
            accumulated for this partition we will 'linger' for the specified
            time waiting for more records to show up. linger_ms=0 indicates no
            lingering.
        :type linger_ms: int
        :param block_on_queue_full: When the producer's message queue for a
            broker contains max_queued_messages, we must either stop accepting
            new messages (block) or throw an error. If True, this setting
            indicates we should block until space is available in the queue.
            If False, we should throw an error immediately.
        :type block_on_queue_full: bool
        :param sync: Whether calls to `produce` should wait for the message to
            send before returning.  If `True`, an exception will be raised from
            `produce()` if delivery to kafka failed.
        :type sync: bool
        :param delivery_reports: If set to `True`, the producer will maintain a
            thread-local queue on which delivery reports are posted for each
            message produced.  These must regularly be retrieved through
            `get_delivery_report()`, which returns a 2-tuple of
            :class:`pykafka.protocol.Message` and either `None` (for success)
            or an `Exception` in case of failed delivery to kafka.
            This setting is ignored when `sync=True`.
        :type delivery_reports: bool
        """
        self._cluster = cluster
        self._topic = topic
        self._partitioner = partitioner
        self._compression = compression
        self._max_retries = max_retries
        self._retry_backoff_ms = retry_backoff_ms
        self._required_acks = required_acks
        self._ack_timeout_ms = ack_timeout_ms
        self._max_queued_messages = max_queued_messages
        self._min_queued_messages = max(1, min_queued_messages)
        self._linger_ms = linger_ms
        self._block_on_queue_full = block_on_queue_full
        self._synchronous = sync
        self._worker_exception = None
        self._worker_trace_logged = False
        self._owned_brokers = None
        self._delivery_reports = (_DeliveryReportQueue()
                                  if delivery_reports or self._synchronous
                                  else _DeliveryReportNone())
        self._running = False
        self._update_lock = self._cluster.handler.Lock()
        self.start()

    def __del__(self):
        log.debug("Finalising {}".format(self))
        self.stop()

    def _raise_worker_exceptions(self):
        """Raises exceptions encountered on worker threads"""
        if self._worker_exception is not None:
            _, ex, tb = self._worker_exception
            # avoid logging worker exceptions more than once, which can
            # happen when this function's `raise` triggers `__exit__`
            # which calls `stop`
            if not self._worker_trace_logged:
                self._worker_trace_logged = True
                log.error("Exception encountered in worker thread:\n%s",
                          "".join(traceback.format_tb(tb)))
            raise ex

    def __repr__(self):
        return "<{module}.{name} at {id_}>".format(
            module=self.__class__.__module__,
            name=self.__class__.__name__,
            id_=hex(id(self))
        )

    def __enter__(self):
        """Context manager entry point - start the producer"""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Context manager exit point - stop the producer"""
        self.stop()

    def start(self):
        """Set up data structures and start worker threads"""
        if not self._running:
            self._setup_owned_brokers()
            self._running = True
        self._raise_worker_exceptions()

    def _update(self):
        """Update the producer and cluster after an ERROR_CODE

        Also re-produces messages that were in queues at the time the update
        was triggered
        """
        # only allow one thread to be updating the producer at a time
        with self._update_lock:
            self._cluster.update()
            queued_messages = self._setup_owned_brokers()
            if len(queued_messages):
                log.debug("Re-producing %d queued messages after update",
                          len(queued_messages))
                for message in queued_messages:
                    self._produce(message)

    def _setup_owned_brokers(self):
        """Instantiate one OwnedBroker per broker

        If there are already OwnedBrokers instantiated, safely stop and flush them
        before creating new ones.
        """
        queued_messages = []
        if self._owned_brokers is not None:
            brokers = list(self._owned_brokers.keys())
            for broker in brokers:
                owned_broker = self._owned_brokers.pop(broker)
                owned_broker.stop()
                batch = owned_broker.flush(self._linger_ms)
                if batch:
                    queued_messages.extend(batch)
        self._owned_brokers = {}
        for partition in self._topic.partitions.values():
            if partition.leader.id not in self._owned_brokers:
                self._owned_brokers[partition.leader.id] = OwnedBroker(
                    self, partition.leader)
        return queued_messages

    def stop(self):
        """Mark the producer as stopped"""
        self._running = False
        self._wait_all()
        if self._owned_brokers is not None:
            for owned_broker in self._owned_brokers.values():
                owned_broker.stop()

    def produce(self, message, partition_key=None):
        """Produce a message.

        :param message: The message to produce (use None to send null)
        :type message: bytes
        :param partition_key: The key to use when deciding which partition to send this
            message to
        :type partition_key: bytes
        """
        if not (isinstance(message, bytes) or message is None):
            raise TypeError("Producer.produce accepts a bytes object, but it "
                            "got '%s'", type(message))
        if not self._running:
            raise ProducerStoppedException()
        partitions = list(self._topic.partitions.values())
        partition_id = self._partitioner(partitions, partition_key).id

        msg = Message(value=message,
                      partition_key=partition_key,
                      partition_id=partition_id,
                      # We must pass our thread-local Queue instance directly,
                      # as results will be written to it in a worker thread
                      delivery_report_q=self._delivery_reports.queue)
        self._produce(msg)

        if self._synchronous:
            reported_msg, exc = self.get_delivery_report()
            assert reported_msg is msg
            if exc is not None:
                raise exc
        self._raise_worker_exceptions()

    def get_delivery_report(self, block=True, timeout=None):
        """Fetch delivery reports for messages produced on the current thread

        Returns 2-tuples of a `pykafka.protocol.Message` and either `None`
        (for successful deliveries) or `Exception` (for failed deliveries).
        This interface is only available if you enabled `delivery_reports` on
        init (and you did not use `sync=True`)
        """
        try:
            return self._delivery_reports.queue.get(block, timeout)
        except AttributeError:
            raise KafkaException("Delivery-reporting is disabled")

    def _produce(self, message):
        """Enqueue a message for the relevant broker

        :param message: Message with valid `partition_id`, ready to be sent
        :type message: `pykafka.protocol.Message`
        """
        success = False
        while not success:
            leader_id = self._topic.partitions[message.partition_id].leader.id
            if leader_id in self._owned_brokers:
                self._owned_brokers[leader_id].enqueue(message)
                success = True
            else:
                success = False

    def _send_request(self, message_batch, owned_broker):
        """Send the produce request to the broker and handle the response.

        :param message_batch: An iterable of messages to send
        :type message_batch: iterable of `pykafka.protocol.Message`
        :param owned_broker: The broker to which to send the request
        :type owned_broker: :class:`pykafka.producer.OwnedBroker`
        """
        req = ProduceRequest(
            compression_type=self._compression,
            required_acks=self._required_acks,
            timeout=self._ack_timeout_ms
        )
        for msg in message_batch:
            req.add_message(msg, self._topic.name, msg.partition_id)
        log.debug("Sending %d messages to broker %d",
                  len(message_batch), owned_broker.broker.id)

        def _get_partition_msgs(partition_id, req):
            """Get all the messages for the partitions from the request."""
            return (
                mset
                for topic, partitions in iteritems(req.msets)
                for p_id, mset in iteritems(partitions)
                if p_id == partition_id
            )

        def mark_as_delivered(message_batch):
            owned_broker.increment_messages_pending(-1 * len(message_batch))
            for msg in message_batch:
                self._delivery_reports.put(msg)

        try:
            response = owned_broker.broker.produce_messages(req)
            if self._required_acks == 0:  # and thus, `response` is None
                mark_as_delivered(message_batch)
                return

            # Kafka either atomically appends or rejects whole MessageSets, so
            # we define a list of potential retries thus:
            to_retry = []  # (MessageSet, Exception) tuples

            for topic, partitions in iteritems(response.topics):
                for partition, presponse in iteritems(partitions):
                    if presponse.err == 0:
                        mark_as_delivered(req.msets[topic][partition].messages)
                        continue  # All's well
                    if presponse.err == NotLeaderForPartition.ERROR_CODE:
                        # Update cluster metadata to get new leader
                        self._update()
                    info = "Produce request for {}/{} to {}:{} failed.".format(
                        topic,
                        partition,
                        owned_broker.broker.host,
                        owned_broker.broker.port)
                    log.warning(info)
                    exc = ERROR_CODES[presponse.err](info)
                    to_retry.extend(
                        (mset, exc)
                        for mset in _get_partition_msgs(partition, req))
        except SocketDisconnectedError as exc:
            log.warning('Broker %s:%s disconnected. Retrying.',
                        owned_broker.broker.host,
                        owned_broker.broker.port)
            self._update()
            to_retry = [
                (mset, exc)
                for topic, partitions in iteritems(req.msets)
                for p_id, mset in iteritems(partitions)
            ]

        if to_retry:
            time.sleep(self._retry_backoff_ms / 1000)
            owned_broker.increment_messages_pending(-1 * len(to_retry))
            for mset, exc in to_retry:
                # XXX arguably, we should try to check these non_recoverables
                # for individual messages in _produce and raise errors there
                # right away, rather than failing a whole batch here?
                non_recoverable = type(exc) in (InvalidMessageSize,
                                                MessageSizeTooLarge)
                for msg in mset.messages:
                    if (non_recoverable or msg.produce_attempt >= self._max_retries):
                        self._delivery_reports.put(msg, exc)
                    else:
                        msg.produce_attempt += 1
                        self._produce(msg)

    def _wait_all(self):
        """Block until all pending messages are sent

        "Pending" messages are those that have been used in calls to `produce`
        and have not yet been dequeued and sent to the broker
        """
        log.info("Blocking until all messages are sent")
        while any(q.message_is_pending() for q in itervalues(self._owned_brokers)):
            time.sleep(.3)
            self._raise_worker_exceptions()


class OwnedBroker(object):
    """An abstraction over a broker connected to by the producer

    An OwnedBroker object contains thread-synchronization primitives
    and message queue corresponding to a single broker for this producer.

    :ivar lock: The lock used to control access to shared resources for this
        queue
    :ivar flush_ready: A condition variable that indicates that the queue is
        ready to be flushed via requests to the broker
    :ivar slot_available: A condition variable that indicates that there is
        at least one position free in the queue for a new message
    :ivar queue: The message queue for this broker. Contains messages that have
        been supplied as arguments to `produce()` waiting to be sent to the
        broker.
    :type queue: collections.deque
    :ivar messages_pending: A counter indicating how many messages have been
        enqueued for this broker and not yet sent in a request.
    :type messages_pending: int
    :ivar producer: The producer to which this OwnedBroker instance belongs
    :type producer: :class:`pykafka.producer.AsyncProducer`
    """
    def __init__(self, producer, broker):
        self.producer = weakref.proxy(producer)
        self.broker = broker
        self.lock = self.producer._cluster.handler.RLock()
        self.flush_ready = self.producer._cluster.handler.Event()
        self.slot_available = self.producer._cluster.handler.Event()
        self.queue = deque()
        self.messages_pending = 0
        self.running = True

        def queue_reader():
            while self.running:
                try:
                    batch = self.flush(self.producer._linger_ms)
                    if batch:
                        self.producer._send_request(batch, self)
                except Exception:
                    # surface all exceptions to the main thread
                    self.producer._worker_exception = sys.exc_info()
                    break
            log.info("Worker exited for broker %s:%s", self.broker.host,
                     self.broker.port)
        log.info("Starting new produce worker for broker %s", broker.id)
        self.producer._cluster.handler.spawn(queue_reader)

    def stop(self):
        self.running = False

    def increment_messages_pending(self, amnt):
        with self.lock:
            self.messages_pending += amnt
            self.messages_pending = max(0, self.messages_pending)

    def message_is_pending(self):
        """
        Indicates whether there are currently any messages that have been
            `produce()`d and not yet sent to the broker
        """
        return self.messages_pending > 0

    def enqueue(self, message):
        """Push message onto the queue

        :param message: The message to push onto the queue
        :type message: `pykafka.protocol.Message`
        """
        self._wait_for_slot_available()
        with self.lock:
            self.queue.appendleft(message)
            self.increment_messages_pending(1)
            if len(self.queue) >= self.producer._min_queued_messages:
                if not self.flush_ready.is_set():
                    self.flush_ready.set()

    def flush(self, linger_ms, release_pending=False):
        """Pop messages from the end of the queue

        :param linger_ms: How long (in milliseconds) to wait for the queue
            to contain messages before flushing
        :type linger_ms: int
        :param release_pending: Whether to decrement the messages_pending
            counter when the queue is flushed. True means that the messages
            popped from the queue will be discarded unless re-enqueued
            by the caller.
        :type release_pending: bool
        """
        self._wait_for_flush_ready(linger_ms)
        with self.lock:
            batch = [self.queue.pop() for _ in range(len(self.queue))]
            if release_pending:
                self.increment_messages_pending(-1 * len(batch))
            if not self.slot_available.is_set():
                self.slot_available.set()
        return batch

    def _wait_for_flush_ready(self, linger_ms):
        """Block until the queue is ready to be flushed

        If the queue does not contain at least one message after blocking for
        `linger_ms` milliseconds, return.

        :param linger_ms: How long (in milliseconds) to wait for the queue
            to contain messages before returning
        :type linger_ms: int
        """
        if len(self.queue) < self.producer._min_queued_messages:
            with self.lock:
                if len(self.queue) < self.producer._min_queued_messages:
                    self.flush_ready.clear()
            self.flush_ready.wait((linger_ms / 1000) if linger_ms > 0 else None)

    def _wait_for_slot_available(self):
        """Block until the queue has at least one slot not containing a message"""
        if len(self.queue) >= self.producer._max_queued_messages:
            with self.lock:
                if len(self.queue) >= self.producer._max_queued_messages:
                    self.slot_available.clear()
            if self.producer._block_on_queue_full:
                self.slot_available.wait()
            else:
                raise ProducerQueueFullError("Queue full for broker %d",
                                             self.broker.id)


class _DeliveryReportQueue(threading.local):
    """Helper that instantiates a new report queue on every calling thread"""
    def __init__(self):
        self.queue = Queue()

    @staticmethod
    def put(msg, exc=None):
        msg.delivery_report_q.put((msg, exc))


class _DeliveryReportNone(object):
    """Stand-in for when _DeliveryReportQueue has been disabled"""
    def __init__(self):
        self.queue = None

    @staticmethod
    def put(msg, exc=None):
        return
