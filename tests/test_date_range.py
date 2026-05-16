"""Tests for the dashboard time-range resolver and the `_in_range` filter.

These are pure-function tests — no DB required. They lock in:
  - preset name → (start, end) resolution rules
  - YTD / ALL / Custom edge cases
  - `_in_range` inclusivity on both ends
  - open vs unbounded behaviour (None bound means no filter on that side)
"""

from datetime import datetime, timedelta

import pytest

from app.services.analytics import (
    DateRange,
    PRESET_RANGE_LABELS,
    _in_range,
    _position_active_in_range,
    resolve_date_range,
)


class _Pos:
    """Stand-in for OptionPosition with just the dates we filter on."""
    def __init__(self, open_date, close_date):
        self.open_date = open_date
        self.close_date = close_date


_NOW = datetime(2026, 5, 15, 12, 0)


class TestResolveDateRange:
    def test_all_when_no_inputs(self):
        dr = resolve_date_range(None, None, None, now=_NOW)
        assert dr.start is None and dr.end is None
        assert dr.label == "ALL"
        assert dr.is_unbounded

    def test_explicit_all(self):
        dr = resolve_date_range("all", None, None, now=_NOW)
        assert dr.is_unbounded
        assert dr.label == "ALL"

    def test_ytd_starts_jan_1(self):
        dr = resolve_date_range("ytd", None, None, now=_NOW)
        assert dr.start == datetime(2026, 1, 1)
        assert dr.end == _NOW
        assert dr.label == "YTD"

    @pytest.mark.parametrize("preset,days", [
        ("1M", 30), ("3M", 90), ("6M", 180),
        ("1Y", 365), ("2Y", 730), ("5Y", 1825),
    ])
    def test_rolling_window_presets(self, preset, days):
        dr = resolve_date_range(preset, None, None, now=_NOW)
        assert dr.end == _NOW
        assert dr.start == _NOW - timedelta(days=days)
        assert dr.label == preset

    def test_preset_is_case_insensitive(self):
        dr = resolve_date_range("1m", None, None, now=_NOW)
        assert dr.label == "1M"
        dr = resolve_date_range("Ytd", None, None, now=_NOW)
        assert dr.label == "YTD"

    def test_custom_with_both_bounds(self):
        a, b = datetime(2025, 1, 1), datetime(2025, 6, 30)
        dr = resolve_date_range("custom", a, b, now=_NOW)
        assert dr.start == a
        assert dr.end == b
        assert dr.label == "Custom"

    def test_custom_one_sided(self):
        # Only start: open-ended on the right.
        dr = resolve_date_range("custom", datetime(2025, 1, 1), None, now=_NOW)
        assert dr.start == datetime(2025, 1, 1)
        assert dr.end is None
        # Only end: open-ended on the left (e.g., "everything before X").
        dr = resolve_date_range("custom", None, datetime(2025, 1, 1), now=_NOW)
        assert dr.start is None
        assert dr.end == datetime(2025, 1, 1)

    def test_explicit_dates_without_range_name(self):
        # Passing start/end with no range= implies custom.
        dr = resolve_date_range(None, datetime(2025, 1, 1), datetime(2025, 6, 30), now=_NOW)
        assert dr.label == "Custom"

    def test_unknown_preset_falls_back_to_all(self):
        # Don't 400 — just degrade to no filter so the dashboard always loads.
        dr = resolve_date_range("nonsense", None, None, now=_NOW)
        assert dr.is_unbounded
        assert dr.label == "ALL"

    def test_preset_labels_constant_includes_all(self):
        # Sanity: the constant the route ships to the template must contain
        # every value resolve_date_range produces (except "Custom" which is
        # always-active when the form is used).
        assert "ALL" in PRESET_RANGE_LABELS
        assert "YTD" in PRESET_RANGE_LABELS
        for p in ("1M", "3M", "6M", "1Y", "2Y", "5Y"):
            assert p in PRESET_RANGE_LABELS


class TestInRange:
    def test_unbounded_includes_everything(self):
        dr = DateRange(None, None, "ALL")
        assert _in_range(datetime(2020, 1, 1), dr)
        assert _in_range(datetime(2030, 1, 1), dr)

    def test_no_close_date_never_in_range(self):
        # Open positions don't have a close_date and shouldn't slip into
        # closed-position aggregations.
        dr = DateRange(None, None, "ALL")
        assert not _in_range(None, dr)
        dr2 = DateRange(datetime(2025, 1, 1), datetime(2025, 12, 31), "1Y")
        assert not _in_range(None, dr2)

    def test_bounded_inclusivity(self):
        start = datetime(2025, 1, 1)
        end = datetime(2025, 12, 31, 23, 59, 59)
        dr = DateRange(start, end, "1Y")
        # Exact bounds are inclusive on both ends.
        assert _in_range(start, dr)
        assert _in_range(end, dr)
        # Outside is excluded.
        assert not _in_range(start - timedelta(seconds=1), dr)
        assert not _in_range(end + timedelta(seconds=1), dr)

    def test_one_sided_start(self):
        dr = DateRange(datetime(2025, 6, 1), None, "Custom")
        assert _in_range(datetime(2025, 6, 1), dr)
        assert _in_range(datetime(2030, 1, 1), dr)
        assert not _in_range(datetime(2024, 12, 31), dr)

    def test_one_sided_end(self):
        dr = DateRange(None, datetime(2025, 6, 1), "Custom")
        assert _in_range(datetime(2024, 12, 31), dr)
        assert _in_range(datetime(2025, 6, 1), dr)
        assert not _in_range(datetime(2025, 6, 2), dr)


# ----------------------------------------------------------------------------
# _position_active_in_range — overlap semantics for the positions page.
# Different from _in_range (used for closed-only stats on the dashboard).
# A position counts if it was on the books for ANY portion of the window.
# ----------------------------------------------------------------------------

class TestPositionActiveInRange:
    def test_unbounded_always_active(self):
        dr = DateRange(None, None, "ALL")
        assert _position_active_in_range(
            _Pos(datetime(2020, 1, 1), datetime(2020, 6, 1)), dr
        )
        assert _position_active_in_range(_Pos(datetime(2030, 1, 1), None), dr)

    def test_closed_inside_window(self):
        dr = DateRange(datetime(2024, 1, 1), datetime(2024, 12, 31), "1Y")
        # Opened and closed entirely inside the window.
        assert _position_active_in_range(
            _Pos(datetime(2024, 3, 1), datetime(2024, 5, 1)), dr
        )

    def test_spans_window_entirely(self):
        # Opened before window, closed after window — was active the whole
        # time. The old close-date-only filter would have missed this.
        dr = DateRange(datetime(2024, 4, 1), datetime(2024, 6, 30), "Custom")
        assert _position_active_in_range(
            _Pos(datetime(2024, 1, 1), datetime(2024, 12, 1)), dr
        )

    def test_open_overlaps_window_start(self):
        # Opened before window starts, closed inside window → still active.
        dr = DateRange(datetime(2024, 4, 1), datetime(2024, 6, 30), "Custom")
        assert _position_active_in_range(
            _Pos(datetime(2024, 1, 1), datetime(2024, 5, 1)), dr
        )

    def test_open_overlaps_window_end(self):
        # Opened inside window, closed after window → still active.
        dr = DateRange(datetime(2024, 4, 1), datetime(2024, 6, 30), "Custom")
        assert _position_active_in_range(
            _Pos(datetime(2024, 6, 1), datetime(2024, 8, 1)), dr
        )

    def test_still_open_position_extends_to_now(self):
        dr = DateRange(datetime(2024, 1, 1), datetime(2024, 12, 31), "1Y")
        # An open position with close_date=None is treated as "ongoing" —
        # included as long as it was opened before the window's end.
        assert _position_active_in_range(_Pos(datetime(2024, 6, 1), None), dr)
        assert _position_active_in_range(_Pos(datetime(2020, 1, 1), None), dr)

    def test_closed_entirely_before_window(self):
        dr = DateRange(datetime(2024, 1, 1), datetime(2024, 12, 31), "1Y")
        assert not _position_active_in_range(
            _Pos(datetime(2023, 1, 1), datetime(2023, 6, 1)), dr
        )

    def test_opened_entirely_after_window(self):
        dr = DateRange(datetime(2024, 1, 1), datetime(2024, 12, 31), "1Y")
        assert not _position_active_in_range(
            _Pos(datetime(2025, 1, 1), datetime(2025, 6, 1)), dr
        )

    def test_one_sided_start_only(self):
        dr = DateRange(datetime(2024, 1, 1), None, "Custom")
        # Anything that closed on/after the start is included; still-open too.
        assert _position_active_in_range(
            _Pos(datetime(2020, 1, 1), datetime(2024, 6, 1)), dr
        )
        assert _position_active_in_range(_Pos(datetime(2024, 1, 1), None), dr)
        # Closed strictly before the start is excluded.
        assert not _position_active_in_range(
            _Pos(datetime(2023, 1, 1), datetime(2023, 12, 31)), dr
        )

    def test_one_sided_end_only(self):
        dr = DateRange(None, datetime(2024, 12, 31), "Custom")
        # Opened on/before end → included regardless of close.
        assert _position_active_in_range(
            _Pos(datetime(2023, 1, 1), datetime(2025, 6, 1)), dr
        )
        # Opened after end → excluded.
        assert not _position_active_in_range(
            _Pos(datetime(2025, 1, 1), None), dr
        )


# ----------------------------------------------------------------------------
# carry_qs: nav-link query-string preservation across pages that support
# time ranges. Tested directly without spinning up the whole app — the
# helper is pure-function over request.query_params.
# ----------------------------------------------------------------------------

class _FakeRequest:
    """Stand-in for starlette.requests.Request — only `query_params` is
    used by carry_qs."""
    def __init__(self, **params):
        # Mimic the .get() interface the helper uses; ignore empty values
        # the same way Starlette's MultiDict would surface them.
        self.query_params = {k: v for k, v in params.items() if v}


class TestCarryQs:
    @staticmethod
    def _carry_qs(**params):
        # Lazy import to avoid coupling test discovery to FastAPI startup.
        from app.main import _carry_qs
        return _carry_qs(_FakeRequest(**params))

    def test_range_alone(self):
        assert self._carry_qs(range="1y") == "?range=1y"

    def test_range_with_custom_dates(self):
        # urlencode preserves insertion order; range first then start/end.
        out = self._carry_qs(range="custom", start="2024-01-01", end="2024-12-31")
        assert out == "?range=custom&start=2024-01-01&end=2024-12-31"

    def test_no_params_means_empty_string(self):
        # Crucial: must NOT return "?" alone — that produces dangling
        # query-strings in nav hrefs ("/positions?").
        assert self._carry_qs() == ""

    def test_page_specific_params_dropped(self):
        # top_n / closed / open / status are page-specific and would be
        # nonsense on the other pages. Helper keeps only range/start/end.
        out = self._carry_qs(range="1y", top_n="10", closed="true", status="active")
        assert out == "?range=1y"

    def test_empty_values_treated_as_absent(self):
        # ?range= with no value (user cleared it) shouldn't propagate.
        assert self._carry_qs(range="", start="", end="") == ""

    def test_url_encoding_special_chars(self):
        # If a value somehow contains a special character, it gets
        # percent-encoded so the resulting href is always well-formed.
        out = self._carry_qs(range="custom", start="2024 01 01")
        assert "2024+01+01" in out or "2024%2001%2001" in out
