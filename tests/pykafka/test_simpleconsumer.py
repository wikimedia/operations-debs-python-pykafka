from contextlib import contextmanager
import mock
import time
import unittest2
from uuid import uuid4

from pykafka import KafkaClient
from pykafka.simpleconsumer import OwnedPartition, OffsetType
from pykafka.test.utils import get_cluster, stop_cluster
from pykafka.utils.compat import range, iteritems


class TestSimpleConsumer(unittest2.TestCase):
    maxDiff = None
    USE_RDKAFKA = False

    @classmethod
    def setUpClass(cls):
        cls.kafka = get_cluster()
        cls.topic_name = uuid4().hex.encode()
        cls.kafka.create_topic(cls.topic_name, 3, 2)

        # It turns out that the underlying producer used by KafkaInstance will
        # write all messages in a batch to a single partition, though not the
        # same partition every time.  We try to attain some spread here by
        # sending more than one batch:
        batch = 300
        cls.total_msgs = 3 * batch
        for _ in range(3):
            cls.kafka.produce_messages(
                cls.topic_name,
                ('msg {i}'.format(i=i) for i in range(batch)))

        cls.client = KafkaClient(cls.kafka.brokers)

    @classmethod
    def tearDownClass(cls):
        stop_cluster(cls.kafka)

    @contextmanager
    def _get_simple_consumer(self, **kwargs):
        topic = self.client.topics[self.topic_name]
        consumer = topic.get_simple_consumer(
            use_rdkafka=self.USE_RDKAFKA, **kwargs)
        try:
            yield consumer
        finally:
            consumer.stop()

    def test_consume(self):
        with self._get_simple_consumer() as consumer:
            messages = [consumer.consume() for _ in range(self.total_msgs)]
            self.assertEquals(len(messages), self.total_msgs)
            self.assertTrue(None not in messages)

    @staticmethod
    def _convert_offsets(offset_responses):
        """Helper function to translate Offset(Fetch)PartitionResponse

        Calls like consumer.fetch_offsets() and earliest_available_offsets()
        return lists of OffsetPartitionResponses.  These hold the next offset
        to be consumed, whereas consumer.held_offsets returns the latest
        consumed offset.  This translates them to facilitate comparisons.
        """
        if isinstance(offset_responses, dict):
            offset_responses = iteritems(offset_responses)
        f1 = lambda off: OffsetType.EARLIEST if off == 0 else off - 1
        f2 = lambda off: off[0] if isinstance(off, list) else off
        return {partition_id: f1(f2(offset_response.offset))
                for partition_id, offset_response in offset_responses}

    def test_offset_commit(self):
        """Check fetched offsets match pre-commit internal state"""
        with self._get_simple_consumer(
                consumer_group=b'test_offset_commit') as consumer:
            [consumer.consume() for _ in range(100)]
            offsets_committed = consumer.held_offsets
            consumer.commit_offsets()

            offsets_fetched = self._convert_offsets(consumer.fetch_offsets())
            self.assertEquals(offsets_fetched, offsets_committed)

    def test_offset_resume(self):
        """Check resumed internal state matches committed offsets"""
        with self._get_simple_consumer(
                consumer_group=b'test_offset_resume') as consumer:
            [consumer.consume() for _ in range(100)]
            offsets_committed = consumer.held_offsets
            consumer.commit_offsets()

        with self._get_simple_consumer(
                consumer_group=b'test_offset_resume') as consumer:
            self.assertEquals(consumer.held_offsets, offsets_committed)

    def test_reset_offset_on_start(self):
        """Try starting from LATEST and EARLIEST offsets"""
        with self._get_simple_consumer(
                auto_offset_reset=OffsetType.EARLIEST,
                reset_offset_on_start=True) as consumer:
            earliest_offs = self._convert_offsets(
                consumer.topic.earliest_available_offsets())
            self.assertEquals(earliest_offs, consumer.held_offsets)
            self.assertIsNotNone(consumer.consume())

        with self._get_simple_consumer(
                auto_offset_reset=OffsetType.LATEST,
                reset_offset_on_start=True,
                consumer_timeout_ms=500) as consumer:
            latest_offs = self._convert_offsets(
                consumer.topic.latest_available_offsets())
            self.assertEquals(latest_offs, consumer.held_offsets)
            self.assertIsNone(consumer.consume(block=False))

        difference = sum(latest_offs[i] - earliest_offs[i]
                         if earliest_offs[i] >= 0 else latest_offs[i] + 1
                         if latest_offs[i] >= 0 else 0
                         for i in latest_offs)
        self.assertEqual(difference, self.total_msgs)

    def test_reset_offsets(self):
        """Test resetting to user-provided offsets"""
        with self._get_simple_consumer(
                auto_offset_reset=OffsetType.EARLIEST) as consumer:
            # Find us a non-empty partition "target_part"
            part_id, latest_offset = next(
                (p, res.offset[0])
                for p, res in consumer.topic.latest_available_offsets().items()
                if res.offset[0] > 0)
            target_part = consumer.partitions[part_id]

            # Set all other partitions to LATEST, to ensure that any consume()
            # calls read from target_part
            partition_offsets = {
                p: OffsetType.LATEST for p in consumer.partitions.values()}

            new_offset = latest_offset - 5
            partition_offsets[target_part] = new_offset
            consumer.reset_offsets(partition_offsets.items())

            self.assertEqual(consumer.held_offsets[part_id], new_offset)
            msg = consumer.consume()
            self.assertEqual(msg.offset, new_offset + 1)

            # Invalid offsets should get overwritten as per auto_offset_reset
            partition_offsets[target_part] = latest_offset + 5  # invalid!
            consumer.reset_offsets(partition_offsets.items())

            # SimpleConsumer's fetcher thread will detect the invalid offset
            # and reset it immediately.  RdKafkaSimpleConsumer however will
            # only get to write the valid offset upon a call to consume():
            msg = consumer.consume()
            expected_offset = target_part.earliest_available_offset()
            self.assertEqual(msg.offset, expected_offset)
            self.assertEqual(consumer.held_offsets[part_id], expected_offset)

    def test_update_cluster(self):
        """Check that the consumer can initiate cluster updates"""
        with self._get_simple_consumer() as consumer:
            self.assertIsNotNone(consumer.consume())

            for broker in self.client.brokers.values():
                broker._connection.disconnect()

            # The consumer fetcher thread should prompt broker reconnection
            t_start = time.time()
            timeout = 10.
            try:
                for broker in self.client.brokers.values():
                    while not broker._connection.connected:
                        time.sleep(.1)
                        self.assertTrue(time.time() - t_start < timeout,
                                        msg="Broker reconnect failed.")
            finally:
                # Make sure further tests don't get confused
                consumer._update()
            # If the fetcher thread fell over during the cluster update
            # process, we'd get an exception here:
            self.assertIsNotNone(consumer.consume())

    def test_consumer_lag(self):
        """Ensure that after consuming the entire topic, lag is 0"""
        with self._get_simple_consumer(consumer_group=b"test_lag_group",
                                       consumer_timeout_ms=1000) as consumer:
            while True:
                message = consumer.consume()
                if message is None:
                    break
            consumer.commit_offsets()
            latest_offsets = {p_id: res.offset[0]
                              for p_id, res
                              in iteritems(consumer.topic.latest_available_offsets())}
            current_offsets = {p_id: res.offset for p_id, res in consumer.fetch_offsets()}
            self.assertEqual(current_offsets, latest_offsets)


class TestOwnedPartition(unittest2.TestCase):
    def test_partition_saves_offset(self):
        msgval = "test"
        partition = mock.MagicMock()
        op = OwnedPartition(partition)

        message = mock.Mock()
        message.value = msgval
        message.offset = 20

        op.enqueue_messages([message])
        self.assertEqual(op.message_count, 1)
        ret_message = op.consume()
        self.assertEqual(op.last_offset_consumed, message.offset)
        self.assertEqual(op.next_offset, message.offset + 1)
        self.assertNotEqual(ret_message, None)
        self.assertEqual(ret_message.value, msgval)

    def test_partition_rejects_old_message(self):
        last_offset = 400
        op = OwnedPartition(None)
        op.last_offset_consumed = last_offset

        message = mock.Mock()
        message.value = "test"
        message.offset = 20

        op.enqueue_messages([message])
        self.assertEqual(op.message_count, 0)
        op.consume()
        self.assertEqual(op.last_offset_consumed, last_offset)

    def test_partition_consume_empty_queue(self):
        op = OwnedPartition(None)

        message = op.consume()
        self.assertEqual(message, None)

    def test_partition_offset_commit_request(self):
        topic = mock.Mock()
        topic.name = "test_topic"
        partition = mock.Mock()
        partition.topic = topic
        partition.id = 12345

        op = OwnedPartition(partition)
        op.last_offset_consumed = 200

        request = op.build_offset_commit_request()

        self.assertEqual(request.topic_name, topic.name)
        self.assertEqual(request.partition_id, partition.id)
        self.assertEqual(request.offset, op.last_offset_consumed + 1)
        self.assertEqual(request.metadata, b'pykafka')

    def test_partition_offset_fetch_request(self):
        topic = mock.Mock()
        topic.name = "test_topic"
        partition = mock.Mock()
        partition.topic = topic
        partition.id = 12345

        op = OwnedPartition(partition)

        request = op.build_offset_fetch_request()

        self.assertEqual(request.topic_name, topic.name)
        self.assertEqual(request.partition_id, partition.id)

    def test_partition_offset_counters(self):
        res = mock.Mock()
        res.offset = 400

        op = OwnedPartition(None)
        op.set_offset(res.offset)

        self.assertEqual(op.last_offset_consumed, res.offset)
        self.assertEqual(op.next_offset, res.offset + 1)


if __name__ == "__main__":
    unittest2.main()
