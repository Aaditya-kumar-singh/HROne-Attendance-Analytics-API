"""Opt-in 100k-row explain/performance smoke test for MongoDB Atlas.

Run separately and intentionally; it inserts 100,000 rows into a unique disposable
database and drops only that database afterward:
    $env:HRONE_RUN_PERF = "1"
    python -m pytest -s -q tests/test_mongodb_performance.py
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main


RUN_PERF = os.getenv("HRONE_RUN_PERF") == "1"
UTC = timezone.utc


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _stats(explain):
    nodes = list(_walk(explain))
    execution = next((node["executionStats"] for node in nodes if isinstance(node.get("executionStats"), dict)), {})
    stages = [node.get("stage") for node in nodes if isinstance(node.get("stage"), str)]
    stages += [node.get("planNodeId") for node in nodes if isinstance(node.get("planNodeId"), str)]
    return {
        "executionTimeMillis": execution.get("executionTimeMillis"),
        "totalDocsExamined": execution.get("totalDocsExamined"),
        "totalKeysExamined": execution.get("totalKeysExamined"),
        "nReturned": execution.get("nReturned"),
        "ixscan_seen": "IXSCAN" in stages or "ixseek" in stages,
        "collscan_seen": "COLLSCAN" in stages,
    }


@pytest.mark.skipif(not RUN_PERF, reason="Set HRONE_RUN_PERF=1 to run the 100k-row disposable benchmark.")
def test_explain_plans_on_100k_attendance_records(monkeypatch):
    original_db_name = main.db.name
    test_db_name = f"hrone_perf_{uuid.uuid4().hex[:16]}"
    if test_db_name == original_db_name or not test_db_name.startswith("hrone_perf_") or len(test_db_name) > 38:
        pytest.fail("Safety guard refused a non-disposable or overlong performance database name.")

    test_db = main.client[test_db_name]
    monkeypatch.setattr(main, "db", test_db)
    try:
        with TestClient(main.app) as api:
            employees = []
            for index in range(1000):
                code = f"EMP{index:05d}"
                employees.append({
                    "emp_code": code,
                    "name": f"Benchmark Employee {index}",
                    "email": f"benchmark{index}@example.test",
                    "department": "Engineering" if index < 500 else "QA",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                    "joined_on": "2026-01-01",
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                })
            test_db.employees.insert_many(employees, ordered=False)

            start_date = date(2026, 1, 1)
            batch = []
            insert_started = time.perf_counter()
            inserted = 0
            for day_offset in range(100):
                day = (start_date + timedelta(days=day_offset)).isoformat()
                punch_in = datetime.combine(start_date + timedelta(days=day_offset), datetime.min.time(), tzinfo=UTC) + timedelta(hours=4)
                punch_out = punch_in + timedelta(hours=9)
                for index in range(1000):
                    batch.append({
                        "emp_code": f"EMP{index:05d}",
                        "date": day,
                        "status": "PRESENT",
                        "punch_in": punch_in,
                        "punch_out": punch_out,
                        "work_hours": 9.0,
                        "late_minutes": (index % 7) * 5,
                        "overtime_minutes": 0,
                        "half_day": False,
                        "history": [],
                    })
                    if len(batch) >= 5000:
                        test_db.attendance_logs.insert_many(batch, ordered=False)
                        inserted += len(batch)
                        batch.clear()
            if batch:
                test_db.attendance_logs.insert_many(batch, ordered=False)
                inserted += len(batch)
            insert_seconds = round(time.perf_counter() - insert_started, 2)
            assert inserted == 100_000
            assert test_db.attendance_logs.count_documents({}) == 100_000

            requests = {
                "attendance_list": "/admin/explain/attendance_list?date_from=2026-01-01&date_to=2026-04-10&page_size=100",
                "employee_monthly": "/admin/explain/employee_monthly?emp_code=EMP00000&month=2026-01",
                "department_summary": "/admin/explain/department_summary?month=2026-01",
                "late_leaderboard": "/admin/explain/late_leaderboard?month=2026-01",
                "department_trend": "/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-03-31",
            }
            report = {"records": inserted, "insert_seconds": insert_seconds, "plans": {}}
            for name, path in requests.items():
                started = time.perf_counter()
                response = api.get(path)
                elapsed = round(time.perf_counter() - started, 2)
                assert response.status_code == 200, (name, response.text)
                explain = response.json()["explain"]
                stats = _stats(explain)
                stats["wall_seconds"] = elapsed
                stats["collscan_seen"] = stats["collscan_seen"] or "COLLSCAN" in str(explain)
                assert stats["ixscan_seen"], f"{name} did not expose an IXSCAN in explain output: {stats}"
                assert not stats["collscan_seen"], f"{name} used COLLSCAN: {stats}"
                report["plans"][name] = stats
            print(f"\n100k benchmark report: {report}")
    finally:
        assert test_db_name.startswith("hrone_perf_") and test_db_name != original_db_name
        main.client.drop_database(test_db_name)
