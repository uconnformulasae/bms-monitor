"""Tests for LineAssembler, which splits raw serial bytes into lines."""

import unittest

from bms_monitor.parsing.lines import MAX_LINE_BYTES, LineAssembler


def texts(pairs):
    return [t for t, _ in pairs]


class TestLineAssembler(unittest.TestCase):
    def test_bare_lf_is_the_firmware_format(self):
        a = LineAssembler()
        self.assertEqual(texts(a.feed(b"one\ntwo\n")), ["one", "two"])

    def test_crlf(self):
        a = LineAssembler()
        self.assertEqual(texts(a.feed(b"one\r\ntwo\r\n")), ["one", "two"])

    def test_bare_cr(self):
        a = LineAssembler()
        self.assertEqual(texts(a.feed(b"one\rtwo\r")), ["one", "two"])

    def test_mixed_endings(self):
        a = LineAssembler()
        self.assertEqual(texts(a.feed(b"a\nb\r\nc\rd\n")), ["a", "b", "c", "d"])

    def test_blank_line_is_preserved(self):
        # The firmware's "\n\n" record separator is meaningful: the serial
        # pacer uses it to know a response finished.
        a = LineAssembler()
        self.assertEqual(texts(a.feed(b"IC1:x\n\n")), ["IC1:x", ""])

    def test_partial_line_is_held(self):
        a = LineAssembler()
        self.assertEqual(a.feed(b"partial"), [])
        self.assertEqual(texts(a.feed(b" rest\n")), ["partial rest"])

    def test_split_at_every_byte_position(self):
        stream = (b"IC1:C1=3.712000V,C2=3.708000V,CCount:63,PECError:0\n\n"
                  b"Enter cmd:4\r\n"
                  b"Status A:\nVREF2:3.000000V\r")
        whole = texts(LineAssembler().feed(stream)) + texts(LineAssembler().feed(stream)[:0])
        reference = texts(LineAssembler().feed(stream))
        self.assertEqual(whole[:len(reference)], reference)

        for cut in range(len(stream) + 1):
            a = LineAssembler()
            got = texts(a.feed(stream[:cut])) + texts(a.feed(stream[cut:]))
            self.assertEqual(got, reference, "split at byte %d" % cut)

    def test_one_byte_at_a_time_matches_one_chunk(self):
        stream = b"a\r\nb\n\nc\rd\r\n"
        reference = texts(LineAssembler().feed(stream))
        a = LineAssembler()
        got = []
        for i in range(len(stream)):
            got += texts(a.feed(stream[i:i + 1]))
        self.assertEqual(got, reference)

    def test_crlf_split_across_chunks(self):
        a = LineAssembler()
        self.assertEqual(texts(a.feed(b"line\r")), ["line"])
        self.assertEqual(texts(a.feed(b"\nnext\n")), ["next"])

    def test_nul_and_ff_from_a_just_enumerated_port_are_dropped(self):
        a = LineAssembler()
        got = a.feed(b"\x00\x00IC1:ok\xff\n\xff\x00next\n")
        self.assertEqual(texts(got), ["IC1:ok", "next"])
        self.assertEqual(a.dropped_noise_bytes, 5)

    def test_noise_inside_a_line_does_not_split_it(self):
        a = LineAssembler()
        self.assertEqual(texts(a.feed(b"IC1:\x00C1=3.7V\n")), ["IC1:C1=3.7V"])

    def test_over_long_line_hits_the_cap_without_unbounded_growth(self):
        a = LineAssembler()
        out = a.feed(b"x" * (MAX_LINE_BYTES * 3))
        self.assertEqual(len(out), 3)
        self.assertTrue(all(trunc for _, trunc in out))
        self.assertTrue(all(len(t) == MAX_LINE_BYTES for t, _ in out))
        self.assertEqual(a.truncated_lines, 3)
        # Nothing accumulated.
        self.assertEqual(a.flush(), [])

    def test_babbling_device_does_not_grow_memory(self):
        a = LineAssembler()
        for _ in range(200):
            a.feed(b"y" * 1000)
        self.assertLess(len(a._buf), MAX_LINE_BYTES)

    def test_utf8_degree_sign(self):
        a = LineAssembler()
        self.assertEqual(texts(a.feed("ITMP:27.5\u00b0C\n".encode("utf-8"))),
                         ["ITMP:27.5\u00b0C"])

    def test_latin1_degree_sign_does_not_raise(self):
        # ADI shipped Latin-1 degree signs; an older build may still be on
        # the board. This must not take the reader thread down.
        a = LineAssembler()
        self.assertEqual(texts(a.feed(b"ITMP:27.5\xb0C\n")), ["ITMP:27.5\u00b0C"])

    def test_multibyte_char_split_across_chunks(self):
        a = LineAssembler()
        self.assertEqual(a.feed("ITMP:27.5\u00b0".encode("utf-8")[:-1]), [])
        self.assertEqual(texts(a.feed("\u00b0C\n".encode("utf-8")[1:])), ["ITMP:27.5\u00b0C"])

    def test_random_binary_never_raises(self):
        import random
        rng = random.Random(4)
        a = LineAssembler()
        for _ in range(500):
            a.feed(bytes(rng.randrange(256) for _ in range(rng.randrange(0, 64))))

    def test_flush_emits_the_tail(self):
        a = LineAssembler()
        a.feed(b"tail")
        self.assertEqual(texts(a.flush()), ["tail"])
        self.assertEqual(a.flush(), [])

    def test_reset_discards_partial_state(self):
        a = LineAssembler()
        a.feed(b"half")
        a.reset()
        self.assertEqual(texts(a.feed(b"whole\n")), ["whole"])

    def test_empty_feed(self):
        self.assertEqual(LineAssembler().feed(b""), [])


if __name__ == "__main__":
    unittest.main()
