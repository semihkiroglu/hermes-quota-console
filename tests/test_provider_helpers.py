from datetime import datetime, timezone

from dashboard.providers.base import finite_number, iso_time, percent


def test_provider_helpers_reject_non_finite_values():
    assert finite_number("3.5") == 3.5
    assert finite_number(float("nan")) is None
    assert finite_number(True) is None
    assert percent(120) == 100
    assert percent(-5) == 0


def test_iso_time_normalizes_epoch_and_timezone():
    assert iso_time(0) == "1970-01-01T00:00:00+00:00"
    assert iso_time(1_700_000_000) == datetime.fromtimestamp(
        1_700_000_000, tz=timezone.utc
    ).isoformat()
    assert iso_time("2026-01-01T03:00:00+03:00") == "2026-01-01T00:00:00+00:00"
