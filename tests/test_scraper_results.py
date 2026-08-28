"""Tests for result-table parsing, especially time-gap ditto marks."""
from scraper.utils import soup
from scraper.results import _parse_results_table, _looks_cancelled


def _row(pos, slug, gap):
    return f"""
    <tr>
      <td>{pos}</td>
      <td class="ridername"><a href="rider/{slug}">{slug}</a></td>
      <td class="time ar"><font>{gap}</font></td>
    </tr>"""


def _table(rows):
    return soup(f"<table class='results'>{rows}</table>").find("table")


def test_ditto_mark_carries_previous_gap_not_zero():
    # PCS renders ",," when a rider's gap equals the PREVIOUS rider's gap.
    # Group 1: winner + one rider at 0:00. Group 2: two riders at +2:30.
    rows = (
        _row(1, "winner", "4:53:12")
        + _row(2, "same-time", ",,")
        + _row(3, "chase-a", "2:30")
        + _row(4, "chase-b", ",,")
    )
    res = _parse_results_table(_table(rows))
    gaps = {r["rider_slug"]: r["time_seconds"] for r in res}
    assert gaps["winner"] == 0
    assert gaps["same-time"] == 0
    assert gaps["chase-a"] == 150
    assert gaps["chase-b"] == 150  # ditto == previous rider's gap, not 0


def test_gruppetto_after_leaders():
    rows = (
        _row(1, "winner", "5:01:00")
        + _row(2, "second", "0:05")
        + _row(3, "grup-a", "21:14")
        + _row(4, "grup-b", ",,")
        + _row(5, "grup-c", ",,")
    )
    res = _parse_results_table(_table(rows))
    gaps = {r["rider_slug"]: r["time_seconds"] for r in res}
    assert gaps["grup-a"] == 1274
    assert gaps["grup-b"] == 1274
    assert gaps["grup-c"] == 1274


def test_dnf_rows_get_no_time():
    rows = _row(1, "winner", "4:00:00") + _row("DNF", "quitter", ",,")
    res = _parse_results_table(_table(rows))
    gaps = {r["rider_slug"]: r["time_seconds"] for r in res}
    assert gaps["quitter"] is None


def test_looks_cancelled_flags_all_nr_placeholder_table():
    # Cancelled/unraced stages render the startlist under table.results markup
    # with every rider marked "NR" instead of a real finish position.
    rows = (
        _row("NR", "rider-a", "") + _row("NR", "rider-b", "") + _row("NR", "rider-d", "")
        + _row("NR", "rider-e", "") + _row("NR", "rider-f", "") + _row("DNF", "rider-c", "")
    )
    res = _parse_results_table(_table(rows))
    assert _looks_cancelled(res)


def test_looks_cancelled_false_for_real_results():
    rows = _row(1, "winner", "4:00:00") + _row(2, "second", "0:05")
    res = _parse_results_table(_table(rows))
    assert not _looks_cancelled(res)
