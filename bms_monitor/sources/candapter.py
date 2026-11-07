"""python-can's slcan bus, fixed for the Orion/Ewert CANdapter.

The CANdapter answers each command, including every transmit, with one
ASCII ACK byte (0x06) and no line end. That byte ends up in front of the
next frame, b"\\x06t6FF8...\\r", and python-can drops any line that doesn't
start with a frame letter, so each transmit silently cost one received
frame (seen as an IC "left out of this cycle"). Stripping the ACK from the
start of a line gives that frame back.

python-can is imported on first use, so the serial path doesn't need it.
"""

from __future__ import annotations

ACK = "\x06"

#: First letters python-can parses as a frame.
FRAME_LETTERS = set("tTrRdDbBx")

#: Whole lines that are command replies, not lost frames: an empty OK, a
#: BELL (error), and the Lawicel "z"/"Z" transmit replies.
REPLIES = {"", "\r", "\a", "z\r", "Z\r"}

_bus_class = None


def candapter_bus_class():
    """The slcanBus subclass, built on first call."""
    global _bus_class
    if _bus_class is None:
        from can.interfaces.slcan import slcanBus
        if not callable(getattr(slcanBus, "_read", None)):
            raise RuntimeError("python-can's slcanBus has no _read(); "
                               "the CANdapter ACK fix in candapter.py needs updating")

        class CandapterSlcanBus(slcanBus):
            #: Called with each line python-can would throw away, for the
            #: raw console. Replies to our own commands are not reported.
            on_unparsed = None

            def _read(self, timeout):
                string = super()._read(timeout)
                if not string:
                    return string
                string = string.lstrip(ACK)
                if (string[:1] not in FRAME_LETTERS and string not in REPLIES
                        and self.on_unparsed is not None):
                    self.on_unparsed(string)
                return string

        _bus_class = CandapterSlcanBus
    return _bus_class
