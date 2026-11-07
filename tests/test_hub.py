"""Hub: bounded history, fan-out, and the derived statistics."""

import queue
import time
import unittest

from bms_monitor.hub import MAX_SSE_CLIENTS, SSE_QUEUE_DEPTH, Hub
from bms_monitor.model import CELL_COUNT
from bms_monitor.ringbuffer import RingBuffer

from .packs import pack


def sample(seq, t=None, cells=None, pec_ok=True, stale=False, ccount=0, jump=False):
    """One single-IC poll cycle, which is what most of these tests want."""
    return pack(seq=seq, t=t, ics=1, pec_ok=pec_ok, stale=stale,
                ccount=ccount, jump=jump)


class TestRingBuffer(unittest.TestCase):
    def test_bounded(self):
        rb = RingBuffer(10)
        for i in range(1000):
            rb.append(i)
        self.assertEqual(len(rb), 10)
        self.assertEqual(rb.snapshot(), list(range(990, 1000)))
        self.assertEqual(rb.dropped, 990)

    def test_sequence_numbers_keep_counting_past_eviction(self):
        rb = RingBuffer(3)
        for i in range(10):
            self.assertEqual(rb.append(i), i)
        self.assertEqual(rb.next_seq, 10)
        self.assertEqual(rb.since(6), [7, 8, 9])
        self.assertEqual(rb.since(100), [])

    def test_snapshot_limit(self):
        rb = RingBuffer(100)
        rb.extend(range(50))
        self.assertEqual(rb.snapshot(limit=5), [45, 46, 47, 48, 49])
        self.assertEqual(len(rb.snapshot(limit=500)), 50)

    def test_rejects_bad_maxlen(self):
        with self.assertRaises(ValueError):
            RingBuffer(0)

    def test_clear(self):
        rb = RingBuffer(5)
        rb.extend(range(5))
        rb.clear()
        self.assertEqual(len(rb), 0)


class TestHubBounds(unittest.TestCase):
    def test_bounded_over_100k_samples(self):
        # The endurance-run test: memory must not grow.
        hub = Hub(history=500, raw_lines=50, events=20)
        for i in range(100_000):
            hub._on_sample(sample(i))
        self.assertEqual(len(hub.samples), 500)
        self.assertEqual(hub.total_samples, 100_000)
        self.assertEqual(hub.samples.dropped, 99_500)
        snap = hub.snapshot()
        self.assertLessEqual(len(snap["samples"]), 500)

    def test_raw_lines_and_events_are_bounded(self):
        from bms_monitor.model import EventRecord
        hub = Hub(history=10, raw_lines=7, events=5)
        for i in range(1000):
            hub._on_source_event("line", "line %d" % i)
            hub._on_source_event("event", EventRecord(kind="unparsed", text="e%d" % i))
        self.assertEqual(len(hub.raw_lines), 7)
        self.assertEqual(len(hub.events), 5)


class TestHubStats(unittest.TestCase):
    def test_pec_window_is_the_last_100_cycles(self):
        hub = Hub()
        for i in range(150):
            hub._on_sample(sample(i, pec_ok=(i % 10 != 0)))
        stats = hub.stats()
        self.assertEqual(stats["pec_window"], 100)
        # Of samples 50..149, every 10th is bad -> 10.
        self.assertEqual(stats["pec_recent"], 10)
        self.assertEqual(stats["pec_total"], 15)

    def test_pec_window_recovers_after_a_bad_patch(self):
        hub = Hub()
        for i in range(100):
            hub._on_sample(sample(i, pec_ok=False))
        self.assertEqual(hub.stats()["pec_recent"], 100)
        for i in range(100, 200):
            hub._on_sample(sample(i, pec_ok=True))
        self.assertEqual(hub.stats()["pec_recent"], 0)

    def test_ccount_jump_counting(self):
        hub = Hub()
        for i in range(20):
            hub._on_sample(sample(i, jump=(i in (3, 11))))
        self.assertEqual(hub.stats()["ccount_jumps"], 2)

    def test_partial_cycles_are_counted_and_never_usable(self):
        hub = Hub()
        for i in range(10):
            hub._on_sample(pack(seq=i, ics=10, cells_per_ic=9,
                                present=[1, 2, 3] if i % 2 else None))
        stats = hub.stats()
        self.assertEqual(stats["partial_cycles"], 5)
        for p in hub.samples.snapshot():
            if p.partial:
                self.assertFalse(p.usable)
                self.assertIsNone(p.pack_v)

    def test_chain_state_names_the_silent_ics(self):
        hub = Hub()
        for i in range(20):
            hub._on_sample(pack(seq=i, ics=10, cells_per_ic=9, present=[1, 2, 3, 4, 5, 6, 7, 8]))
        chain = hub.chain_state()
        self.assertEqual(len(chain), 10)
        states = {c["ic"]: c["state"] for c in chain}
        self.assertEqual(states[1], "ok")
        self.assertEqual(states[9], "silent")
        self.assertEqual(states[10], "silent")

    def test_chain_state_distinguishes_pec_from_silent(self):
        hub = Hub()
        for i in range(20):
            hub._on_sample(pack(seq=i, ics=2, cells_per_ic=9, pec_ok=False))
        states = {c["ic"]: c["state"] for c in hub.chain_state()}
        self.assertEqual(states[1], "pec")
        self.assertEqual(states[2], "pec")

    def test_data_state_transitions(self):
        hub = Hub()
        self.assertEqual(hub.data_state(), "no-data")
        hub._on_sample(sample(0))
        self.assertEqual(hub.data_state(), "live")

    def test_stale_and_pec_bad_samples_do_not_make_data_live(self):
        hub = Hub()
        hub._on_sample(sample(0, stale=True))
        self.assertEqual(hub.data_state(), "no-data")
        hub._on_sample(sample(1, pec_ok=False))
        self.assertEqual(hub.data_state(), "no-data")
        hub._on_sample(sample(2))
        self.assertEqual(hub.data_state(), "live")

    def test_rate_is_zero_before_any_data(self):
        self.assertEqual(Hub().rate_hz(), 0.0)

    def test_stats_is_json_serialisable(self):
        import json
        hub = Hub()
        hub._on_sample(sample(0))
        json.dumps(hub.stats())

    def test_snapshot_is_json_serialisable(self):
        import json
        hub = Hub()
        for i in range(5):
            hub._on_sample(sample(i))
            hub._on_source_event("line", "IC1:whatever")
        json.dumps(hub.snapshot())


class TestCcountWrap(unittest.TestCase):
    def test_63_to_0_is_a_normal_step_not_a_jump(self):
        from bms_monitor.model import ccount_delta
        self.assertEqual(ccount_delta(63, 0), 1)
        self.assertEqual(ccount_delta(60, 5), 9)
        self.assertEqual(ccount_delta(0, 63), 63)

    def test_unknown_sides(self):
        from bms_monitor.model import ccount_delta
        self.assertIsNone(ccount_delta(None, 3))
        self.assertIsNone(ccount_delta(3, None))

    def test_pipeline_does_not_flag_the_wrap(self):
        from bms_monitor.sources.base import IngestPipeline
        samples = []
        pipeline = IngestPipeline(on_pack=samples.append, on_line=lambda x: None,
                                  on_status=lambda f: None, on_event=lambda e: None,
                                  warmup=0)
        body = ",".join("C%d=3.700000V" % (i + 1) for i in range(16))
        for ccount in (54, 63, 8, 17):          # steps of 9, wrapping at 64
            pipeline.feed_bytes(
                ("IC1:%s,CCount:%d,PECError:0\n\n" % (body, ccount)).encode())
        self.assertEqual(len(samples), 4)
        self.assertFalse(any(p.any_jump for p in samples[1:]))

    def test_pipeline_flags_a_stalled_counter(self):
        from bms_monitor.sources.base import IngestPipeline
        samples = []
        pipeline = IngestPipeline(on_pack=samples.append, on_line=lambda x: None,
                                  on_status=lambda f: None, on_event=lambda e: None,
                                  warmup=0)
        body = ",".join("C%d=3.700000V" % (i + 1) for i in range(16))
        for ccount in (5, 5, 5):
            pipeline.feed_bytes(
                ("IC1:%s,CCount:%d,PECError:0\n\n" % (body, ccount)).encode())
        self.assertTrue(samples[1].any_jump)

    def test_pipeline_flags_a_large_jump(self):
        from bms_monitor.sources.base import IngestPipeline
        samples = []
        pipeline = IngestPipeline(on_pack=samples.append, on_line=lambda x: None,
                                  on_status=lambda f: None, on_event=lambda e: None,
                                  warmup=0)
        body = ",".join("C%d=3.700000V" % (i + 1) for i in range(16))
        for ccount in (5, 45):
            pipeline.feed_bytes(
                ("IC1:%s,CCount:%d,PECError:0\n\n" % (body, ccount)).encode())
        self.assertTrue(samples[1].any_jump)


class TestSseFanOut(unittest.TestCase):
    def test_client_cap(self):
        hub = Hub()
        clients = [hub.add_client() for _ in range(MAX_SSE_CLIENTS)]
        self.assertTrue(all(clients))
        self.assertIsNone(hub.add_client())
        hub.remove_client(clients[0])
        self.assertIsNotNone(hub.add_client())

    def test_a_slow_client_is_dropped_and_never_blocks_the_producer(self):
        hub = Hub()
        client = hub.add_client()
        start = time.monotonic()
        for i in range(SSE_QUEUE_DEPTH * 3):
            hub._on_sample(sample(i))
        elapsed = time.monotonic() - start
        self.assertTrue(client.dropped)
        # The producer must not have been throttled by the full queue.
        self.assertLess(elapsed, 5.0)
        # And the hub kept every sample.
        self.assertEqual(hub.total_samples, SSE_QUEUE_DEPTH * 3)

    def test_a_dropped_client_receives_nothing_further(self):
        hub = Hub()
        client = hub.add_client()
        for i in range(SSE_QUEUE_DEPTH + 10):
            hub._on_sample(sample(i))
        depth_after = client.q.qsize()
        hub._on_sample(sample(9999))
        self.assertEqual(client.q.qsize(), depth_after)

    def test_events_are_well_formed_sse(self):
        hub = Hub()
        client = hub.add_client()
        hub._on_sample(sample(0))
        payload = client.q.get_nowait()
        self.assertTrue(payload.startswith("event: sample\ndata: "))
        self.assertTrue(payload.endswith("\n\n"))

    def test_broadcast_with_no_clients_is_cheap_and_safe(self):
        hub = Hub()
        for i in range(1000):
            hub._on_sample(sample(i))

    def test_heartbeat_pushes_stats(self):
        hub = Hub()
        client = hub.add_client()
        hub.heartbeat()
        payload = client.q.get(timeout=1.0)
        self.assertIn("event: stats", payload)

    def test_removing_an_unknown_client_is_harmless(self):
        hub = Hub()
        client = hub.add_client()
        hub.remove_client(client)
        hub.remove_client(client)


class TestHubNeverRaises(unittest.TestCase):
    def test_malformed_source_events_are_swallowed(self):
        hub = Hub()
        for kind, payload in [("sample", None), ("event", object()),
                              ("status", None), ("line", None), ("unknown", 1)]:
            hub._on_source_event(kind, payload)      # must not raise

    def test_attach_then_stats_without_a_started_source(self):
        class Broken:
            def subscribe(self, cb):
                return lambda: None

            def status(self):
                raise RuntimeError("boom")

        hub = Hub()
        hub.attach(Broken())
        self.assertEqual(hub.stats()["source"], {})


if __name__ == "__main__":
    unittest.main()
