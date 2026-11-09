"""CAN frame encode/decode tests.

Byte order and scaling are the easy things to get wrong, so frames are
checked byte-for-byte against hand-computed little-endian values.
"""

import struct
import unittest

from bms_monitor.sources import can_frames as F


class TestPackSummary(unittest.TestCase):
    def test_byte_order_is_little_endian(self):
        _, data = F.encode_pack_summary(378.0, 3.6912, 3.7124, 21.0)
        # 378.00 V / 0.01 = 37800 = 0x93A8 -> a8 93 little-endian
        self.assertEqual(data[0:2], b"\xa8\x93")
        self.assertEqual(struct.unpack("<H", data[0:2])[0], 37800)
        # And the big-endian reading is a different, wrong number.
        self.assertNotEqual(struct.unpack(">H", data[0:2])[0], 37800)

    def test_field_offsets_match_the_spec(self):
        _, data = F.encode_pack_summary(100.0, 3.0, 4.2, 1200.0)
        self.assertEqual(len(data), 8)
        self.assertEqual(struct.unpack_from("<H", data, 0)[0], 10000)   # 100.00 / 0.01
        self.assertEqual(struct.unpack_from("<H", data, 2)[0], 30000)   # 3.0000 / 0.0001
        self.assertEqual(struct.unpack_from("<H", data, 4)[0], 42000)   # 4.2000 / 0.0001
        self.assertEqual(struct.unpack_from("<H", data, 6)[0], 1200)    # mV

    def test_round_trip(self):
        _, data = F.encode_pack_summary(378.42, 3.6912, 3.7124, 21.2)
        got = F.decode_pack_summary(data)
        self.assertAlmostEqual(got["pack_v"], 378.42, places=2)
        self.assertAlmostEqual(got["min_cell_v"], 3.6912, places=4)
        self.assertAlmostEqual(got["max_cell_v"], 3.7124, places=4)
        self.assertAlmostEqual(got["spread_mv"], 21.0, places=0)

    def test_full_pack_voltage_fits(self):
        """90 cells x 4.2 V = 378 V, and the field must have headroom."""
        _, data = F.encode_pack_summary(378.0, 4.2, 4.2, 0)
        self.assertAlmostEqual(F.decode_pack_summary(data)["pack_v"], 378.0, places=2)
        # u16 at 0.01 tops out at 655.35 V.
        _, data = F.encode_pack_summary(655.0, 4.2, 4.2, 0)
        self.assertAlmostEqual(F.decode_pack_summary(data)["pack_v"], 655.0, places=2)

    def test_over_range_clamps_rather_than_wraps(self):
        """A saturated value is obviously wrong; a wrapped one is not."""
        _, data = F.encode_pack_summary(9999.0, 3.7, 3.7, 0)
        self.assertAlmostEqual(F.decode_pack_summary(data)["pack_v"], 655.35, places=2)

    def test_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            F.decode_pack_summary(b"\x00" * 7)


class TestTempsExtremes(unittest.TestCase):
    def test_round_trip_including_negative_temperature(self):
        _, data = F.encode_temps_extremes(12, 89, -5.4, 41.3, 3, 10)
        got = F.decode_temps_extremes(data)
        self.assertEqual(got["min_cell_index"], 12)
        self.assertEqual(got["max_cell_index"], 89)
        self.assertAlmostEqual(got["min_temp_c"], -5.4, places=1)
        self.assertAlmostEqual(got["max_temp_c"], 41.3, places=1)
        self.assertEqual(got["max_temp_sensor"], 3)
        self.assertEqual(got["ics_responding"], 10)

    def test_negative_temperature_is_signed_little_endian(self):
        _, data = F.encode_temps_extremes(0, 0, -5.4, 0, 0, 0)
        self.assertEqual(struct.unpack_from("<h", data, 2)[0], -54)
        # Unsigned or big-endian both give a wrong, plausible-looking value.
        self.assertNotEqual(struct.unpack_from("<H", data, 2)[0], 65482 - 65536)

    def test_missing_temperature_is_a_sentinel_not_a_zero(self):
        """No sensor must not read as 0 degC."""
        _, data = F.encode_temps_extremes(0, 0, None, None, 0, 10)
        got = F.decode_temps_extremes(data)
        self.assertIsNone(got["min_temp_c"])
        self.assertIsNone(got["max_temp_c"])

    def test_cell_index_covers_the_whole_pack(self):
        _, data = F.encode_temps_extremes(89, 89, 0, 0, 0, 10)
        self.assertEqual(F.decode_temps_extremes(data)["max_cell_index"], 89)


class TestStatus(unittest.TestCase):
    def test_fault_bit_positions(self):
        for mask, name in F.FAULT_BITS:
            _, data = F.encode_status(mask, 0, 0, None, 0, 0)
            got = F.decode_status(data)
            self.assertEqual(got["faults"], [name])
            self.assertEqual(got["fault_bits"], mask)

    def test_ams_fault_is_bit_7(self):
        self.assertEqual(F.FLT_AMS, 0x80)
        _, data = F.encode_status(F.FLT_AMS, 0, 0, None, 0, 0)
        self.assertTrue(F.decode_status(data)["ams_fault"])
        _, data = F.encode_status(0x7F, 0, 0, None, 0, 0)
        self.assertFalse(F.decode_status(data)["ams_fault"])

    def test_not_responding_bitmap_is_little_endian_and_covers_ten_ics(self):
        _, data = F.encode_status(0, 0, 0b1000000001, None, 0, 0)
        self.assertEqual(struct.unpack_from("<H", data, 2)[0], 0b1000000001)
        self.assertEqual(F.decode_status(data)["not_responding_ics"], [1, 10])

    def test_all_ten_ics_silent(self):
        _, data = F.encode_status(0, 0, 0x3FF, None, 0, 0)
        self.assertEqual(F.decode_status(data)["not_responding_ics"], list(range(1, 11)))

    def test_soc_round_trip_and_sentinel(self):
        _, data = F.encode_status(0, 0, 0, 87.25, 0, 0)
        self.assertAlmostEqual(F.decode_status(data)["soc_pct"], 87.25, places=2)
        # The controller does not estimate SOC yet. That must read as
        # "unavailable", never as 655.35%.
        _, data = F.encode_status(0, 0, 0, None, 0, 0)
        self.assertIsNone(F.decode_status(data)["soc_pct"])
        self.assertEqual(struct.unpack_from("<H", data, 4)[0], 0xFFFF)

    def test_consecutive_pec_and_balancing_saturate(self):
        _, data = F.encode_status(0, 99999, 0, None, 99999, 0)
        got = F.decode_status(data)
        self.assertEqual(got["consecutive_pec"], 255)
        self.assertEqual(got["balancing_count"], 255)

    def test_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            F.decode_status(b"\x00" * 4)


class TestCan2CellFrames(unittest.TestCase):
    """CAN2: fixed IDs, 4 cells per frame, no multiplexing.

    Cell n is always in the same byte of the same ID, so a dropped frame
    only loses four cells for one cycle.
    """

    def test_ninety_cells_need_twenty_three_frames(self):
        self.assertEqual(F.CELLS_PER_FRAME, 4)
        self.assertEqual(F.CELL_FRAME_COUNT, 23)
        self.assertEqual(F.ID_CELLS_BASE, 0x700)
        self.assertEqual(F.ID_CELLS_LAST, 0x716)
        self.assertGreaterEqual(F.CELL_FRAME_COUNT * F.CELLS_PER_FRAME, 90)

    def test_round_trip(self):
        volts = [3.6912, 3.7124, 3.7001, 3.6998]
        can_id, data = F.encode_cells(0, volts)
        self.assertEqual(can_id, 0x700)
        got = F.decode_cells(can_id, data)
        self.assertEqual(got["frame"], 0)
        self.assertEqual(got["first_cell"], 1)
        for want, have in zip(volts, got["volts"]):
            self.assertAlmostEqual(want, have, places=4)

    def test_cell_n_is_always_the_same_byte_of_the_same_id(self):
        for cell in range(1, 91):
            frame_index, offset = divmod(cell - 1, F.CELLS_PER_FRAME)
            can_id = F.cell_frame_id(frame_index)
            marker = 3.0 + cell * 0.001
            volts = [0.0] * F.CELLS_PER_FRAME
            volts[offset] = marker
            _, data = F.encode_cells(frame_index, volts)
            got = F.decode_cells(can_id, data)
            self.assertEqual(got["first_cell"] + offset, cell)
            self.assertAlmostEqual(got["volts"][offset], marker, places=4)

    def test_byte_order_is_little_endian(self):
        _, data = F.encode_cells(0, [3.7124, 0, 0, 0])
        self.assertEqual(struct.unpack_from("<H", data, 0)[0], 37124)
        self.assertNotEqual(struct.unpack_from(">H", data, 0)[0], 37124)

    def test_the_last_frame_pads_its_unused_slots_as_no_data(self):
        """90 cells is 22.5 frames, so 0x716 has two slots for cells that
        don't exist. They should carry the no-data sentinel, not 0 V."""
        can_id, data = F.encode_cells(22, [3.70, 3.71])
        self.assertEqual(can_id, 0x716)
        self.assertEqual(len(data), 8)
        got = F.decode_cells(can_id, data)["volts"]
        self.assertAlmostEqual(got[0], 3.70, places=4)
        self.assertIsNone(got[2])
        self.assertIsNone(got[3])

    def test_no_data_decodes_to_none_not_six_and_a_half_volts(self):
        """The controller sends 0xFFFF for a cell it couldn't read (silent IC
        or PEC failure). Decoded naively that's 6.5535 V, which looks like a
        real reading and skews the chart, max-cell tile and pack total.
        """
        data = struct.pack("<4H", 37124, F.CELL_NO_DATA, 37130, F.CELL_NO_DATA)
        got = F.decode_cells(0x700, data)["volts"]
        self.assertAlmostEqual(got[0], 3.7124, places=4)
        self.assertIsNone(got[1])
        self.assertAlmostEqual(got[2], 3.7130, places=4)
        self.assertIsNone(got[3])

    def test_none_round_trips_through_encode_and_decode(self):
        _, data = F.encode_cells(0, [3.7, None, 3.8, None])
        got = F.decode_cells(0x700, data)["volts"]
        self.assertAlmostEqual(got[0], 3.7, places=4)
        self.assertIsNone(got[1])
        self.assertAlmostEqual(got[2], 3.8, places=4)
        self.assertIsNone(got[3])

    def test_a_real_voltage_can_never_collide_with_the_sentinel(self):
        """0xFFFF is only safe as a sentinel if no real reading reaches it.

        The widest code the ADBMS6830 produces is 0x7FFF, which is
        (32767 + 10000) x 150 uV = 6.4151 V -> 64151 counts. Clamping
        keeps anything a caller passes below the sentinel too.
        """
        widest = (32767 + 10000) * 150 / 1e6
        _, data = F.encode_cells(0, [widest] * 4)
        self.assertTrue(all(v is not None for v in F.decode_cells(0x700, data)["volts"]))
        _, data = F.encode_cells(0, [99.0] * 4)          # absurd, clamped
        self.assertTrue(all(v is not None for v in F.decode_cells(0x700, data)["volts"]))

    def test_firmware_bytes_decode_correctly(self):
        """Bytes built the way App/Src/can_frames.c builds them.

        Its cell conversion is (code + 10000) * 3 / 2 in 0.1 mV units,
        with CAN_FRAMES_RESET_CODE (0x8000) mapping to the sentinel.
        """
        def firmware_0p1mv(code):
            if code == -32768:
                return F.CELL_NO_DATA
            x = code + 10000
            return 0 if x <= 0 else (3 * x + 1) // 2

        codes = [14416, 14417, -32768, 14419]
        data = struct.pack("<4H", *[firmware_0p1mv(c) for c in codes])
        got = F.decode_cells(0x700, data)["volts"]
        self.assertAlmostEqual(got[0], 3.6624, places=4)
        self.assertAlmostEqual(got[1], 3.6626, places=4)
        self.assertIsNone(got[2], "a chip reset must not render as a voltage")
        self.assertAlmostEqual(got[3], 3.6629, places=4)

    def test_negative_voltage_clamps_rather_than_wraps(self):
        _, data = F.encode_cells(0, [-1.2, 3.7, 3.7, 3.7])
        self.assertEqual(F.decode_cells(0x700, data)["volts"][0], 0.0)

    def test_rejects_a_bad_frame_index_or_id(self):
        with self.assertRaises(ValueError):
            F.encode_cells(23, [0, 0, 0, 0])
        with self.assertRaises(ValueError):
            F.decode_cells(0x717, b"\x00" * 8)
        with self.assertRaises(ValueError):
            F.decode_cells(0x700, b"\x00" * 7)


class TestCan2IcFrames(unittest.TestCase):
    def test_one_frame_per_ic(self):
        self.assertEqual(F.ID_IC_BASE, 0x720)
        self.assertEqual(F.ID_IC_LAST, 0x729)
        self.assertEqual(F.IC_FRAME_COUNT, 10)

    def test_round_trip(self):
        can_id, data = F.encode_ic_status(9, F.IC_THSD | F.IC_ANY_UV, 63, 3,
                                         41.5, 0b101)
        self.assertEqual(can_id, 0x729)
        got = F.decode_ic_status(can_id, data)
        self.assertEqual(got["ic_index"], 9)
        self.assertEqual(sorted(got["flags"]), ["any_uv", "thsd"])
        self.assertEqual(got["cmd_counter"], 63)
        self.assertEqual(got["consecutive_pec"], 3)
        self.assertAlmostEqual(got["die_temp_c"], 41.5, places=1)
        self.assertEqual(got["balancing_bitmap"], 0b101)
        self.assertFalse(got["pec_fail"])

    def test_pec_fail_is_bit_zero(self):
        _, data = F.encode_ic_status(0, F.IC_PEC_FAIL, 1, 5, 30.0, 0)
        self.assertTrue(F.decode_ic_status(0x720, data)["pec_fail"])

    def test_every_status_bit_round_trips(self):
        for mask, name in F.IC_STATUS_BITS:
            _, data = F.encode_ic_status(0, mask, 0, 0, None, 0)
            self.assertEqual(F.decode_ic_status(0x720, data)["flags"], [name])

    def test_missing_die_temperature_is_a_sentinel(self):
        _, data = F.encode_ic_status(0, 0, 0, 0, None, 0)
        self.assertIsNone(F.decode_ic_status(0x720, data)["die_temp_c"])

    def test_command_counter_is_six_bits(self):
        _, data = F.encode_ic_status(0, 0, 999, 0, 0, 0)
        self.assertEqual(F.decode_ic_status(0x720, data)["cmd_counter"], 63)

    def test_rejects_a_bad_index(self):
        with self.assertRaises(ValueError):
            F.encode_ic_status(10, 0, 0, 0, 0, 0)
        with self.assertRaises(ValueError):
            F.decode_ic_status(0x72A, b"\x00" * 8)


class TestCan2TempFrames(unittest.TestCase):
    def test_round_trip_with_negatives_and_missing_sensors(self):
        can_id, data = F.encode_temps(0, [25.0, -5.5, None, 40.1])
        self.assertEqual(can_id, 0x730)
        got = F.decode_temps(can_id, data)["celsius"]
        self.assertAlmostEqual(got[0], 25.0, places=1)
        self.assertAlmostEqual(got[1], -5.5, places=1)
        self.assertIsNone(got[2], "a missing sensor must not read as 0 degC")
        self.assertAlmostEqual(got[3], 40.1, places=1)

    def test_an_empty_slot_is_told_apart_from_a_probe_with_no_reading(self):
        # The F105 sends INT16_MIN for "no thermistor in this slot" and
        # INT16_MAX for "a thermistor is there but has no reading".
        data = struct.pack("<4h", 250, -32768, 32767, -55)
        got = F.decode_temps(F.ID_TEMPS_BASE, data)
        self.assertEqual(got["celsius"], [25.0, None, None, -5.5])
        self.assertEqual(got["fitted"], [True, False, True, True])

    def test_signed_little_endian(self):
        _, data = F.encode_temps(0, [-5.5, 0, 0, 0])
        self.assertEqual(struct.unpack_from("<h", data, 0)[0], -55)

    def test_first_sensor_index(self):
        for frame in range(F.TEMP_FRAME_COUNT):
            _, data = F.encode_temps(frame, [20.0] * 4)
            got = F.decode_temps(F.ID_TEMPS_BASE + frame, data)
            self.assertEqual(got["first_sensor"], frame * 4 + 1)

    def test_twenty_channels_fit(self):
        self.assertGreaterEqual(F.TEMP_FRAME_COUNT * F.TEMPS_PER_FRAME, 20)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            F.encode_temps(99, [0, 0, 0, 0])
        with self.assertRaises(ValueError):
            F.decode_temps(0x740, b"\x00" * 8)


class TestBusOwnership(unittest.TestCase):
    def test_owns_id_covers_both_buses_and_nothing_else(self):
        owned = (list(F.CAN1_IDS)
                 + [F.ID_VERSION, F.ID_CHAIN_STATUS]   # 0x6FE, 0x6FF: the controller's own
                 + [0x700, 0x716, 0x720, 0x729, 0x730, 0x734])
        for can_id in owned:
            self.assertTrue(F.owns_id(can_id), hex(can_id))
        for can_id in (0x000, 0x100, 0x5FF, 0x603, 0x610, 0x6FD, 0x717,
                       0x71F, 0x72A, 0x735, 0x7FF):
            self.assertFalse(F.owns_id(can_id), hex(can_id))

    def test_the_old_multiplexed_id_is_gone(self):
        self.assertFalse(hasattr(F, "ID_CELL_DETAIL"))
        self.assertIsNone(F.decode(0x610, b"\x00" * 8))


class TestIndexMapping(unittest.TestCase):
    def test_ten_ics_of_nine_cells_maps_to_0_89(self):
        seen = set()
        for ic in range(1, 11):
            for cell in range(1, 10):
                index = F.cell_index(ic, cell, 9)
                self.assertEqual(F.from_cell_index(index, 9), (ic, cell))
                seen.add(index)
        self.assertEqual(seen, set(range(90)))
        self.assertEqual(max(seen), F.MAX_CELL_INDEX)

    def test_single_ic_of_sixteen(self):
        self.assertEqual(F.cell_index(1, 16, 16), 15)
        self.assertEqual(F.from_cell_index(15, 16), (1, 16))


class TestDispatch(unittest.TestCase):
    def test_every_known_id_decodes(self):
        frames = [
            F.encode_pack_summary(300, 3.7, 3.8, 100),
            F.encode_temps_extremes(0, 1, 20, 30, 0, 10),
            F.encode_status(0, 0, 0, None, 0, 0),
            F.encode_cells(0, [3.7] * 4),
            F.encode_ic_status(0, 0, 1, 0, 30.0, 0),
            F.encode_temps(0, [20.0] * 4),
        ]
        kinds = [F.decode(can_id, data)["kind"] for can_id, data in frames]
        self.assertEqual(kinds, ["summary", "extremes", "status",
                                 "cells", "ic", "temperatures"])

    def test_unknown_id_returns_none(self):
        self.assertIsNone(F.decode(0x123, b"\x00" * 8))

    def test_known_id_with_wrong_length_returns_none_rather_than_guessing(self):
        self.assertIsNone(F.decode(F.ID_PACK_SUMMARY, b"\x00" * 3))

    def test_never_raises_on_arbitrary_bytes(self):
        import random
        rng = random.Random(7)
        for _ in range(3000):
            can_id = rng.choice(list(F.CAN1_IDS)
                                + [0x700, 0x716, 0x720, 0x729, 0x730,
                                   0x100, 0x7FF])
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 12)))
            F.decode(can_id, data)


class TestChainStatusFrame(unittest.TestCase):
    """0x6FF, sent by the F105 controller every cycle.

    Not the same as 0x602: it carries the chain state and which chips aren't
    answering, not the CAN1 summary with SOC and fault bits.
    """

    def test_round_trip(self):
        can_id, data = F.encode_chain_status(F.CHAIN_STATE_RUNNING, 42, 0b0110)
        self.assertEqual(can_id, 0x6FF)
        got = F.decode_chain_status(data)
        self.assertEqual(got["chain_state"], "RUNNING")
        self.assertTrue(got["chain_running"])
        self.assertEqual(got["cycle"], 42)
        self.assertEqual(got["invalid_ics_bitmap"], 0b0110)

    def test_bitmap_becomes_a_one_based_ic_list(self):
        """The firmware counts chips from 0, the UI counts from 1. Bit 0
        is the first chip, which the rest of the tool calls IC1."""
        _, data = F.encode_chain_status(1, 0, 0b101)
        self.assertEqual(F.decode_chain_status(data)["not_responding_ics"], [1, 3])

    def test_init_state(self):
        _, data = F.encode_chain_status(F.CHAIN_STATE_INIT, 0, 0xFFFF)
        got = F.decode_chain_status(data)
        self.assertEqual(got["chain_state"], "INIT")
        self.assertFalse(got["chain_running"])
        self.assertEqual(len(got["not_responding_ics"]), 16)

    def test_the_dispatcher_recognises_it(self):
        _, data = F.encode_chain_status(1, 7, 0)
        out = F.decode(0x6FF, data)
        self.assertIsNotNone(out, "0x6FF must not fall through as unknown")
        self.assertEqual(out["kind"], "status")
        self.assertTrue(F.owns_id(0x6FF))

    def test_it_does_not_collide_with_the_can1_summary(self):
        """0x602 keeps its own richer layout; 0x6FF is a separate message."""
        self.assertNotEqual(F.ID_CHAIN_STATUS, F.ID_STATUS)
        self.assertLess(F.ID_CHAIN_STATUS, F.ID_CELLS_BASE,
                        "status must out-prioritise cell frames on the bus")

    def test_firmware_bytes_decode(self):
        """Bytes as can_frames_build_status() lays them out."""
        data = bytearray(8)
        data[0] = 1          # CAN_FRAMES_CHAIN_RUNNING
        data[1] = 200        # cycle counter, wraps at 255
        struct.pack_into("<H", data, 2, 0x0002)
        got = F.decode(0x6FF, bytes(data))
        self.assertEqual(got["chain_state"], "RUNNING")
        self.assertEqual(got["cycle"], 200)
        self.assertEqual(got["not_responding_ics"], [2])


class TestRaceStudioTable(unittest.TestCase):
    def test_every_can1_channel_is_listed_with_its_scale(self):
        text = "\n".join(F.race_studio_rows())
        for can_id in F.CAN1_IDS:
            self.assertIn("0x%03X" % can_id, text)
        for name in ("BMS_PackVoltage", "BMS_SOC", "BMS_ICsNotRespondingBitmap"):
            self.assertIn(name, text)

    def test_both_buses_are_described(self):
        text = "\n".join(F.race_studio_rows())
        self.assertIn("CAN1 -> AIM datalogger", text)
        self.assertIn("CAN2 -> BMS bus", text)
        self.assertIn("0x700-0x716", text)
        self.assertIn("0x720-0x729", text)
        self.assertIn("no multiplexing", text)

    def test_byte_order_is_stated_explicitly(self):
        """Byte order is the easiest thing to get wrong when setting up Race
        Studio, so the table states it in words as well as per field."""
        text = "\n".join(F.race_studio_rows())
        self.assertIn("LITTLE-ENDIAN (Intel)", text)
        self.assertIn("LE/Intel", text)
        self.assertIn("Intel", text)

    def test_channel_table_offsets_do_not_overlap(self):
        by_id = {}
        for can_id, name, start, length, _s, _sc, _o, _u in F.CHANNELS:
            by_id.setdefault(can_id, []).append((start, start + length, name))
        for can_id, spans in by_id.items():
            spans.sort()
            for i in range(1, len(spans)):
                self.assertGreaterEqual(
                    spans[i][0], spans[i - 1][1],
                    "0x%03X: %s overlaps %s" % (can_id, spans[i][2], spans[i - 1][2]))
            self.assertLessEqual(spans[-1][1], 8, "0x%03X exceeds 8 bytes" % can_id)


if __name__ == "__main__":
    unittest.main()
