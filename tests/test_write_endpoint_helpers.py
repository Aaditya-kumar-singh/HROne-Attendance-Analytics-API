from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from app.main import _derived_values, _attendance_date, _from_epoch_ms, PunchInIn, PunchOutIn, RegularizeIn, health
from app import main
from pymongo.errors import ServerSelectionTimeoutError
from pydantic import ValidationError

IST = timezone(timedelta(hours=5, minutes=30))

def test_derived_values_half_day_uses_rounded_hours():
    emp = {"shift_start":"09:30", "shift_end":"18:30"}
    rec = {"date":"2026-07-06", "status":"PRESENT", "punch_in":datetime(2026,7,6,9,0,tzinfo=IST), "punch_out":datetime(2026,7,6,13,30,18,tzinfo=IST)}
    result = _derived_values(rec, emp)
    assert result["work_hours"] == 4.51 and result["half_day"] is False

def test_derived_values_rejects_more_than_24_hours():
    emp = {"shift_start":"09:30", "shift_end":"18:30"}
    rec = {"date":"2026-07-06", "status":"PRESENT", "punch_in":datetime(2026,7,6,9,0,tzinfo=IST), "punch_out":datetime(2026,7,7,9,0,1,tzinfo=IST)}
    with pytest.raises(HTTPException) as exc: _derived_values(rec, emp)
    assert exc.value.status_code == 422

def test_absent_record_derived_values_reset():
    assert _derived_values({"date":"2026-07-06", "status":"LEAVE", "punch_in":None, "punch_out":None}, {}) == {"late_minutes":0, "work_hours":None, "overtime_minutes":0, "half_day":False}

def test_overnight_attendance_date_uses_shift_start_day():
    assert _attendance_date(datetime(2026,7,7,0,45,tzinfo=IST), "22:00", "06:00") == "2026-07-06"


def test_half_day_threshold_uses_rounded_work_hours_at_exactly_four_point_five():
    employee = {"shift_start": "09:30", "shift_end": "18:30"}
    start = datetime(2026, 7, 6, 9, 0, tzinfo=IST)
    exact = {"date": "2026-07-06", "status": "PRESENT", "punch_in": start,
             "punch_out": datetime(2026, 7, 6, 13, 30, tzinfo=IST)}
    below = {**exact, "punch_out": datetime(2026, 7, 6, 13, 29, 24, tzinfo=IST)}
    assert _derived_values(exact, employee)["work_hours"] == 4.5
    assert _derived_values(exact, employee)["half_day"] is False
    assert _derived_values(below, employee)["work_hours"] == 4.49
    assert _derived_values(below, employee)["half_day"] is True


def test_derived_values_normalizes_naive_bson_datetimes_as_utc():
    employee = {"shift_start": "09:30", "shift_end": "18:30"}
    record = {"date": "2026-07-06", "status": "PRESENT",
              "punch_in": datetime(2026, 7, 6, 4, 0),
              "punch_out": datetime(2026, 7, 6, 13, 0)}
    values = _derived_values(record, employee)
    assert values["work_hours"] == 9.0
    assert values["half_day"] is False


def test_timestamp_models_reject_bool_float_string_and_seconds_like_values():
    for value in (True, 1783339200000.0, "1783339200000", 1783339200, 99_999_999_999, 4_102_444_800_001):
        with pytest.raises(ValidationError):
            PunchInIn(emp_code="EMP12345", punched_at=value)
    with pytest.raises(ValidationError):
        PunchOutIn(emp_code="EMP12345", punched_at=False)
    with pytest.raises(ValidationError):
        RegularizeIn(punch_in=1783339200, reason="valid reason", regularized_by="reviewer")


def test_epoch_millisecond_conversion_truncates_to_whole_seconds():
    value = 1_783_339_200_987
    assert _from_epoch_ms(value).microsecond == 0
    assert int(_from_epoch_ms(value).timestamp() * 1000) == 1_783_339_200_000


def test_health_maps_database_unavailability_to_503(monkeypatch):
    class FailingAdmin:
        @staticmethod
        def command(_command):
            raise ServerSelectionTimeoutError("synthetic unavailable database")

    class FailingClient:
        admin = FailingAdmin()

    monkeypatch.setattr(main, "client", FailingClient())
    with pytest.raises(HTTPException) as exc:
        health()
    assert exc.value.status_code == 503
