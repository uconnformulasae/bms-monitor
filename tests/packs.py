"""Helpers that build Sample and PackSample objects for the tests."""

from bms_monitor.model import CELL_COUNT, PackSample, Sample


def reading(ic=1, seq=0, t=0.0, cells=None, ccount=0, pec_ok=True, stale=False,
            jump=False, base=3.70):
    if cells is None:
        cells = [base + 0.001 * i + 0.01 * (ic - 1) for i in range(CELL_COUNT)]
    cells = tuple(cells) + (0.0,) * (CELL_COUNT - len(cells))
    return Sample(seq=seq, t_mono=t, t_wall=1_700_000_000.0 + t, ic=ic,
                  cells=cells[:CELL_COUNT], ccount=ccount, pec_ok=pec_ok,
                  stale=stale, ccount_jump=jump)


def pack(seq=0, t=None, ics=1, cells_per_ic=CELL_COUNT, present=None,
         pec_ok=True, stale=False, jump=False, ccount=0, base=3.70):
    """One poll cycle.

    ``present`` lists the ICs that answered; the rest are missing. Use it to
    build a partial cycle.
    """
    t = seq * 0.1 if t is None else t
    if present is None:
        present = list(range(1, ics + 1))
    readings = tuple(
        reading(ic=ic, seq=seq, t=t, ccount=ccount, pec_ok=pec_ok, jump=jump,
                base=base, cells=[base + 0.001 * i + 0.01 * (ic - 1)
                                  for i in range(cells_per_ic)])
        for ic in present
    )
    return PackSample(seq=seq, t_mono=t, t_wall=1_700_000_000.0 + t,
                      readings=readings, expected_ics=ics,
                      cells_per_ic=cells_per_ic, stale=stale)
