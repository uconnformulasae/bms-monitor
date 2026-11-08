"""Property-based tests verifying codec roundtrips and mathematical invariants."""

import math
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from bms_monitor.model import NtcConfig, code_to_volts, is_plausible_voltage, ntc_celsius, volts_to_code
from bms_monitor.parsing.lines import LineAssembler
from bms_monitor.sources import can_frames as F


class TestModelProperties(unittest.TestCase):
    @given(st.integers(min_value=-32768, max_value=32767))
    def test_code_voltage_codec_roundtrip(self, code):
        volts = code_to_volts(code)
        recovered = volts_to_code(volts)
        self.assertEqual(recovered, code)

    @given(st.floats(min_value=0.0, max_value=5.0))
    def test_voltage_code_roundtrip_precision(self, volts):
        code = volts_to_code(volts)
        recovered = code_to_volts(code)
        self.assertAlmostEqual(recovered, volts, delta=0.000150)

    @given(st.floats(allow_nan=True, allow_infinity=True))
    def test_volts_to_code_never_crashes(self, val):
        code = volts_to_code(val)
        self.assertIsInstance(code, int)
        self.assertGreaterEqual(code, -32768)
        self.assertLessEqual(code, 32767)

    @given(
        st.floats(min_value=0.2, max_value=2.8),
        st.floats(min_value=0.2, max_value=2.8),
    )
    def test_ntc_temperature_monotonicity(self, v1, v2):
        if abs(v1 - v2) < 0.05:
            return
        cfg = NtcConfig(beta=3900.0, r25=10000.0, series=10000.0, vref=3.0, pullup=True)
        t1 = ntc_celsius(v1, cfg)
        t2 = ntc_celsius(v2, cfg)
        if t1 is not None and t2 is not None:
            if v1 < v2:
                self.assertGreaterEqual(t1, t2)
            else:
                self.assertLessEqual(t1, t2)


class TestLineAssemblerProperties(unittest.TestCase):
    @given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200))
    def test_chunking_invariance(self, text):
        clean_text = text.replace("\r", "")
        if not clean_text:
            return
        # Ensure trailing newline for line-based feed
        raw_bytes = (clean_text + "\n").encode("utf-8", errors="replace")

        # Full feed
        asm_full = LineAssembler()
        full_lines = [line for line, trunc in asm_full.feed(raw_bytes) if not trunc]

        # Single-byte feed
        asm_chunked = LineAssembler()
        chunked_lines = []
        for b in raw_bytes:
            for line, trunc in asm_chunked.feed(bytes([b])):
                if not trunc:
                    chunked_lines.append(line)

        self.assertEqual(full_lines, chunked_lines)


class TestCanCodecProperties(unittest.TestCase):
    @given(
        st.integers(min_value=0, max_value=F.CELL_FRAME_COUNT - 1),
        st.lists(
            st.floats(min_value=0.0, max_value=6.0),
            min_size=4,
            max_size=4,
        ),
    )
    def test_cells_roundtrip(self, frame_index, volts_list):
        can_id, payload = F.encode_cells(frame_index, volts_list)
        decoded = F.decode_cells(can_id, payload)
        self.assertEqual(decoded["frame"], frame_index)
        for original, recovered in zip(volts_list, decoded["volts"]):
            self.assertAlmostEqual(original, recovered, delta=0.0001)

    @given(
        st.integers(min_value=0, max_value=F.TEMP_FRAME_COUNT - 1),
        st.lists(
            st.floats(min_value=-40.0, max_value=125.0),
            min_size=4,
            max_size=4,
        ),
    )
    def test_temps_roundtrip(self, frame_index, temps_list):
        can_id, payload = F.encode_temps(frame_index, temps_list)
        decoded = F.decode_temps(can_id, payload)
        self.assertEqual(decoded["frame"], frame_index)
        for original, recovered in zip(temps_list, decoded["celsius"]):
            self.assertAlmostEqual(original, recovered, delta=0.1)

    @given(st.booleans())
    def test_balance_cmd_roundtrip(self, enable):
        can_id, payload = F.encode_balance_cmd(enable)
        decoded = F.decode_balance_cmd(payload)
        self.assertEqual(decoded["enable"], enable)


if __name__ == "__main__":
    unittest.main()
