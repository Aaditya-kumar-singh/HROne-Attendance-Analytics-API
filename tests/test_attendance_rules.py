from datetime import datetime, timedelta, timezone

from app.main import compute_late_minutes, compute_overtime, compute_work_hours

IST = timezone(timedelta(hours=5, minutes=30))


def test_late_grace_boundary_is_strict_and_minutes_are_since_shift_start():
    assert compute_late_minutes(datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST), "09:30") == 0
    assert compute_late_minutes(datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST), "09:30") == 10


def test_work_hours_use_half_up_not_bankers_rounding():
    start = datetime(2026, 7, 6, 9, 0, 0, tzinfo=IST)
    end = datetime(2026, 7, 6, 13, 30, 18, tzinfo=IST)
    assert compute_work_hours(start, end) == 4.51


def test_overtime_under_30_minutes_is_zero():
    punch_out = datetime(2026, 7, 6, 18, 50, 0, tzinfo=IST)
    assert compute_overtime(punch_out, "18:30", "2026-07-06") == 0


def test_overnight_overtime_uses_next_calendar_day_for_shift_end():
    punch_out = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    assert compute_overtime(punch_out, "06:00", "2026-07-06") == 40


def test_late_minutes_are_floored_from_shift_start_after_grace():
    assert compute_late_minutes(datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST), "09:30") == 45


def test_overtime_threshold_is_inclusive_at_30_minutes():
    assert compute_overtime(datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST), "18:30", "2026-07-06") == 0
    assert compute_overtime(datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST), "18:30", "2026-07-06") == 30


def test_overnight_overtime_accepts_shift_start_for_end_date_resolution():
    punch_out = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    assert compute_overtime(punch_out, "06:00", "2026-07-06", "22:00") == 40


def test_naive_bson_datetime_is_interpreted_as_utc():
    # 04:10 UTC is 09:40 IST; the grace boundary is inclusive.
    assert compute_late_minutes(datetime(2026, 7, 6, 4, 10, 0), "09:30") == 0


def test_overnight_early_morning_punch_uses_previous_shift_date():
    punch_in = datetime(2026, 7, 7, 0, 45, 0, tzinfo=IST)
    assert compute_late_minutes(punch_in, "22:00", shift_end="06:00") == 165
