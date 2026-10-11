from datetime import date
import pytest
from fastapi import HTTPException
from app.main import (
    app, _month_bounds, _employee_monthly_pipeline, _department_summary_pipeline,
    _late_leaderboard_pipeline, _department_trend_pipeline,
)


def test_all_contract_operations_have_routes():
    expected = {
        ("GET", "/health"), ("POST", "/employees"), ("GET", "/employees"),
        ("POST", "/attendance/punch-in"), ("POST", "/attendance/punch-out"),
        ("GET", "/attendance"), ("PATCH", "/attendance/{emp_code}/{date}"),
        ("GET", "/analytics/employees/{emp_code}/monthly"),
        ("GET", "/analytics/departments/summary"), ("GET", "/analytics/leaderboard/late"),
        ("GET", "/analytics/departments/{department}/trend"),
        ("GET", "/admin/explain/{endpoint}"),
    }
    actual = {(method, route.path) for route in app.routes for method in (getattr(route, "methods", None) or set()) if method in {"GET", "POST", "PATCH"} and not route.path.startswith(("/docs", "/redoc", "/openapi"))}
    assert expected <= actual


def test_month_bounds_handles_december_leap_year():
    assert _month_bounds("2024-12") == ("2024-12-01", "2024-12-31")
    assert _month_bounds("2024-02") == ("2024-02-01", "2024-02-29")


def test_invalid_month_is_422():
    with pytest.raises(HTTPException) as exc:
        _month_bounds("2026-13")
    assert exc.value.status_code == 422


def test_invalid_year_zero_month_is_422_not_server_error():
    with pytest.raises(HTTPException) as exc:
        _month_bounds("0000-01")
    assert exc.value.status_code == 422


def test_analytics_pipelines_are_database_side_and_expected_shapes():
    monthly = _employee_monthly_pipeline("EMP0001", "2026-07-01", "2026-07-31")
    summary = _department_summary_pipeline("2026-07-01", "2026-07-31", None)
    leaderboard = _late_leaderboard_pipeline("2026-07-01", "2026-07-31", 10)
    trend = _department_trend_pipeline("Engineering", "2026-07-01", "2026-07-07")
    assert monthly[0]["$match"]["emp_code"] == "EMP0001"
    assert any("$lookup" in stage for stage in summary)
    assert any("$setWindowFields" in stage for stage in leaderboard)
    assert trend[0]["$match"] == {"department": "Engineering"}
    assert any("$dateAdd" in str(stage) for stage in trend)
    assert any("$setWindowFields" in stage for stage in trend)
