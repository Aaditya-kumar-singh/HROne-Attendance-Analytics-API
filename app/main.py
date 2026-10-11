"""HROne Employee Attendance & Analytics API.

The assignment requires all application code to remain in this file.
"""
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import re
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]



# ---- Shared date/time calculations and response serialization ----

IST = timezone(timedelta(hours=5, minutes=30))

def _as_ist(value: datetime) -> datetime:
    """Normalize an instant to IST; naive BSON datetimes are interpreted as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).replace(microsecond=0)


def _shift_datetime(date_str: str, shift_time: str) -> datetime:
    h, m = map(int, shift_time.split(":"))
    return datetime.fromisoformat(date_str).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: Optional[str] = None,
    shift_end: Optional[str] = None,
) -> int:
    local_in = _as_ist(punch_in)
    shift_date = datetime.fromisoformat(attendance_date).date() if attendance_date else local_in.date()

    # An early-morning punch belongs to the previous shift-start date for an
    # overnight shift. Explicit attendance_date always remains authoritative.
    if attendance_date is None and shift_end is not None:
        start_minutes = int(shift_start[:2]) * 60 + int(shift_start[3:])
        end_minutes = int(shift_end[:2]) * 60 + int(shift_end[3:])
        punch_minutes = local_in.hour * 60 + local_in.minute
        if end_minutes <= start_minutes and punch_minutes < end_minutes:
            shift_date -= timedelta(days=1)

    start = _shift_datetime(shift_date.isoformat(), shift_start)
    elapsed_seconds = int((local_in - start).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    start = _as_ist(punch_in)
    end = _as_ist(punch_out)
    seconds = int((end - start).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: Optional[str] = None,
) -> int:
    local_out = _as_ist(punch_out)
    shift_date = datetime.fromisoformat(date_str).date()
    if shift_start is not None:
        start_minutes = int(shift_start[:2]) * 60 + int(shift_start[3:])
        end_minutes = int(shift_end[:2]) * 60 + int(shift_end[3:])
        if end_minutes <= start_minutes:
            shift_date += timedelta(days=1)
    elif local_out.date() > shift_date:
        shift_date += timedelta(days=1)

    end = _shift_datetime(shift_date.isoformat(), shift_end)
    elapsed_seconds = int((local_out - end).total_seconds())
    if elapsed_seconds < 1800:
        return 0
    return elapsed_seconds // 60


def _epoch_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).replace(microsecond=0).timestamp() * 1000)


def _valid_date(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    return parsed


def _attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local_in = _as_ist(punch_in)
    start_mins = int(shift_start[:2]) * 60 + int(shift_start[3:])
    end_mins = int(shift_end[:2]) * 60 + int(shift_end[3:])
    day = local_in.date()
    if end_mins <= start_mins and local_in.hour * 60 + local_in.minute < end_mins:
        day -= timedelta(days=1)
    return day.isoformat()


def _serialize_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k not in {"_id", "id"}}
    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = _epoch_ms(result["created_at"])
    return result


def _serialize_attendance(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k not in {"_id", "id"}}
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    for key in ("punch_in", "punch_out"):
        if isinstance(result.get(key), datetime):
            result[key] = _epoch_ms(result[key])
    history = []
    for entry in result.get("history") or []:
        item = dict(entry)
        if isinstance(item.get("at"), datetime):
            item["at"] = _epoch_ms(item["at"])
        changes = item.get("changes")
        if isinstance(changes, dict):
            changes = {key: dict(value) for key, value in changes.items()}
            for change in changes.values():
                for side in ("from", "to"):
                    if isinstance(change.get(side), datetime):
                        change[side] = _epoch_ms(change[side])
            item["changes"] = changes
        history.append(item)
    result["history"] = history
    return result

# ---- Request models and validation ----

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=7, max_length=9)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    department: str = Field(min_length=1, max_length=100)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must match EMP followed by 4 to 6 digits")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("email must be a valid email address")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("shift time must be HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        if parsed.isoformat() != value:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_shift_differs(cls, value: str, info):
        if value == info.data.get("shift_start", "09:30"):
            raise ValueError("shift_start and shift_end must differ")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        if value is not None and (value < 100_000_000_000 or value > 4_102_444_800_000):
            raise ValueError("punched_at must be epoch milliseconds in the supported range")
        return value



class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        if value is not None and (value < 100_000_000_000 or value > 4_102_444_800_000):
            raise ValueError("punched_at must be epoch milliseconds in the supported range")
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamps(cls, value):
        if value is not None and (value < 100_000_000_000 or value > 4_102_444_800_000):
            raise ValueError("timestamp must be epoch milliseconds in the supported range")
        return value

# ---- MongoDB indexes ----

def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee_sort")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_late")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="employees_joined_department")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="employees_department_joined")



@asynccontextmanager
async def lifespan(_: FastAPI):
    create_indexes()
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ---- Employee and attendance endpoints ----

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return _serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = list(db.employees.find(query, {"_id": 0, "id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    return {"items": [_serialize_employee(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be an integer epoch-millisecond timestamp")
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = datetime.fromtimestamp(body.punched_at // 1000, timezone.utc) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    ts = ts.replace(microsecond=0)
    shift_start, shift_end = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    attendance_day = _attendance_date(ts, shift_start, shift_end)
    doc = {"emp_code": body.emp_code, "date": attendance_day, "status": body.status,
           "punch_in": ts, "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(ts, shift_start, attendance_day, shift_end),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return _serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        date_filter = {}
        if date_from is not None:
            _valid_date(date_from, "date_from")
            date_filter["$gte"] = date_from
        if date_to is not None:
            _valid_date(date_to, "date_to")
            date_filter["$lte"] = date_to
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        query["date"] = date_filter
    if status is not None:
        if status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
            raise HTTPException(422, "invalid status")
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query, {"_id": 0, "id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size).limit(page_size))
    return {"items": [_serialize_attendance(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}



def _as_utc(value: datetime) -> datetime:
    """Normalize aware or PyMongo-naive BSON datetimes to whole-second UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _from_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value // 1000, timezone.utc).replace(microsecond=0)


def _now_utc_seconds() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _presence_status(status: str) -> bool:
    return status in {"PRESENT", "WFH", "ON_DUTY"}


def _derived_values(record: dict, employee: dict) -> dict:
    status = record["status"]
    punch_in_value, punch_out_value = record.get("punch_in"), record.get("punch_out")
    if isinstance(punch_in_value, datetime):
        punch_in_value = _as_utc(punch_in_value)
    if isinstance(punch_out_value, datetime):
        punch_out_value = _as_utc(punch_out_value)
    if not _presence_status(status):
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if punch_in_value is None:
        raise HTTPException(422, "presence status requires punch_in")
    late = compute_late_minutes(punch_in_value, employee.get("shift_start", "09:30"), record["date"], employee.get("shift_end", "18:30"))
    if punch_out_value is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    duration = (punch_out_value - punch_in_value).total_seconds()
    if duration <= 0 or duration > 86400:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    hours = compute_work_hours(punch_in_value, punch_out_value)
    overtime = compute_overtime(punch_out_value, employee.get("shift_end", "18:30"), record["date"], employee.get("shift_start", "09:30"))
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.50}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be an integer epoch-millisecond timestamp")
    employee = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = _from_epoch_ms(body.punched_at) if body.punched_at is not None else _now_utc_seconds()
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}, sort=[("punch_in", DESCENDING), ("date", DESCENDING)])
    if record is None:
        future_open_record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": punched_at}, "punch_out": None},
            sort=[("punch_in", ASCENDING), ("date", DESCENDING)],
        )
        if future_open_record is not None:
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "no eligible punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record already punched out")
    record_punch_in = _as_utc(record["punch_in"])
    duration = (punched_at - record_punch_in).total_seconds()
    if duration <= 0 or duration > 86400:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": record["date"], "punch_out": None, "punch_in": record["punch_in"]},
        {"$set": {"punch_out": punched_at}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        latest = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": record["date"]})
        if latest and latest.get("punch_out") is not None:
            raise HTTPException(409, "attendance record already punched out")
        raise HTTPException(409, "attendance record changed concurrently")
    values = _derived_values(updated, employee)
    db.attendance_logs.update_one({"emp_code": body.emp_code, "date": record["date"], "punch_out": punched_at}, {"$set": values})
    updated.update(values)
    return _serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    _valid_date(date, "date")
    if "status" in body.model_fields_set and body.status is None:
        raise HTTPException(422, "status cannot be null")
    if "punch_in" in body.model_fields_set and body.punch_in is None:
        raise HTTPException(422, "punch_in cannot be null")
    if "punch_out" in body.model_fields_set and body.punch_out is None:
        raise HTTPException(422, "punch_out cannot be null")
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if old is None:
        raise HTTPException(404, "attendance record not found")
    # PyMongo decodes BSON datetimes as naive UTC by default. Normalize the in-memory
    # record so comparisons with request timestamps (aware UTC) remain well-defined.
    old = dict(old)
    for timestamp_field in ("punch_in", "punch_out"):
        if isinstance(old.get(timestamp_field), datetime):
            old[timestamp_field] = _as_utc(old[timestamp_field])
    new = dict(old)
    requested = body.model_dump(exclude_unset=True)
    requested.pop("reason", None)
    requested.pop("regularized_by", None)
    new["status"] = requested.get("status", old.get("status", "PRESENT"))
    if new["status"] in {"ABSENT", "LEAVE"}:
        if "punch_in" in requested or "punch_out" in requested:
            raise HTTPException(422, "ABSENT/LEAVE cannot be combined with punch times")
        new["punch_in"] = None
        new["punch_out"] = None
    for key in ("punch_in", "punch_out"):
        if key in requested:
            new[key] = _from_epoch_ms(requested[key])
    if _presence_status(new["status"]) and new.get("punch_in") is None:
        raise HTTPException(422, "presence status requires punch_in")
    if new.get("punch_in") is not None and _attendance_date(new["punch_in"], employee.get("shift_start", "09:30"), employee.get("shift_end", "18:30")) != date:
        raise HTTPException(422, "punch_in must belong to the attendance date")
    if new.get("punch_out") is not None:
        if new.get("punch_in") is None:
            raise HTTPException(422, "punch_out requires punch_in")
        duration = (new["punch_out"] - new["punch_in"]).total_seconds()
        if duration <= 0 or duration > 86400:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    values = _derived_values(new, employee)
    new.update(values)
    fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for key in fields:
        before, after = old.get(key), new.get(key)
        if key in {"late_minutes", "overtime_minutes"}:
            before, after = before or 0, after or 0
        if before != after:
            changes[key] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "regularization does not change the record")
    history = list(old.get("history") or [])
    history.append({"at": _now_utc_seconds(), "by": body.regularized_by, "reason": body.reason, "changes": changes})
    cas = {"emp_code": emp_code, "date": date, "history": old.get("history", [])}
    for key in fields:
        cas[key] = old.get(key)
    result = db.attendance_logs.update_one(cas, {"$set": {**{key: new.get(key) for key in fields}, "history": history}})
    if result.modified_count != 1:
        raise HTTPException(409, "attendance record changed concurrently")
    new.update({"emp_code": emp_code, "date": date, "history": history})
    return _serialize_attendance(new)

# ---- Analytics pipelines and endpoints ----

def _month_bounds(month: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    try:
        first = date(year, mon, 1)
        last = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - timedelta(days=1)
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")
    return first.isoformat(), last.isoformat()


def _round_half_up(value, places: int = 2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _weekday_count(start: str, end: str) -> int:
    # This iterates over at most one calendar month, never attendance documents.
    cursor, last, count = date.fromisoformat(start), date.fromisoformat(end), 0
    while cursor <= last:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def _attendance_month_match(start: str, end: str, emp_code: Optional[str] = None) -> dict:
    query = {"date": {"$gte": start, "$lte": end}}
    if emp_code is not None:
        query["emp_code"] = emp_code
    return query


def _employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": _attendance_month_match(start, end, emp_code)},
        {"$set": {"_weekday": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$lte": ["$_weekday", 5]}]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
    ]


def _department_summary_pipeline(start: str, end: str, department: Optional[str] = None) -> list:
    employee_match = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined": "$joined_on"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", "$$joined"]},
                {"$gte": ["$date", start]}, {"$lte": ["$date", end]}]}}},
                {"$set": {"_weekday": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}}],
            "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": {"department": "$department", "emp_code": "$emp_code"},
            "joined_on": {"$first": "$joined_on"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$lte": ["$logs._weekday", 5]}]},
                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]},
                "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$group": {"_id": "$_id.department",
            "headcount": {"$sum": 1}, "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"}, "work_hours_count": {"$sum": "$work_hours_count"},
            "late_count": {"$sum": "$late_count"}, "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"}}},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]},
                {"$round": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]

def _late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str] = None) -> list:
    pipeline = [
        {"$match": {**_attendance_month_match(start, end), "late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        # Filter pre-join legacy attendance before summing; orphan logs are removed by the employee join.
        {"$match": {"$expr": {"$gte": ["$date", "$employee.joined_on"]}}},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$employee.name"},
            "department": {"$first": "$employee.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def _department_trend_pipeline(department: str, start: str, end: str) -> list:
    start_dt = datetime.combine(date.fromisoformat(start), datetime.min.time())
    end_dt = datetime.combine(date.fromisoformat(end), datetime.min.time())
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "_start": {"$dateFromString": {"dateString": start}}, "_end": {"$dateFromString": {"dateString": end}}, "department": {"$literal": department}}},
        {"$set": {"_days": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": "$_start", "endDate": "$_end", "unit": "day"}}, 1]}]}}},
        {"$set": {"_days": {"$map": {"input": "$_days", "as": "offset", "in": {"$dateAdd": {"startDate": "$_start", "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$_days"},
        {"$set": {"_date": {"$dateToString": {"date": "$_days", "format": "%Y-%m-%d"}}}},
        {"$lookup": {"from": "employees", "let": {"dept": "$department", "day": "$_date"},
            "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", "$$dept"]}, {"$lte": ["$joined_on", "$$day"]}]}}},
                         {"$count": "count"}], "as": "_headcount"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.count", 0]}, 0]},
                  "is_working_day": {"$lte": [{"$isoDayOfWeek": "$_days"}, 5]}}},
        {"$lookup": {"from": "attendance_logs", "let": {"dept": "$department", "day": "$_date"},
            "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}]} }},
                         {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "_emp"}},
                         {"$unwind": "$_emp"}, {"$match": {"$expr": {"$and": [
                             {"$eq": ["$_emp.department", "$$dept"]}, {"$lte": ["$_emp.joined_on", "$$day"]}]}}},
                         {"$group": {"_id": None,
                             "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0]}},
                             "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}],
            "as": "_daily"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$_daily.present_count", 0]}, 0]},
                  "late_count": {"$ifNull": [{"$arrayElemAt": ["$_daily.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$round": [{"$divide": ["$present_count", "$headcount"]}, 8]}, None]}}},
        {"$setWindowFields": {"sortBy": {"_days": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$_date", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


def _validate_month(month: str) -> tuple[str, str]:
    return _month_bounds(month)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = _validate_month(month)
    if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "employee not found")
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    effective_start = max(start, emp.get("joined_on", start))
    working_days = _weekday_count(effective_start, end) if effective_start <= end else 0
    rows = list(db.attendance_logs.aggregate(_employee_monthly_pipeline(emp_code, effective_start, end), hint="uq_attendance_employee_date"))
    row = rows[0] if rows else {}
    present_days = row.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": _round_half_up(present_days),
            "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0),
            "attendance_pct": _round_half_up(present_days / working_days * 100, 4) if working_days else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = _validate_month(month)
    items = list(db.employees.aggregate(_department_summary_pipeline(start, end, department), hint="employees_joined_department"))
    for item in items:
        item["present_days"] = _round_half_up(item.get("present_days", 0))
        item["avg_work_hours"] = _round_half_up(item.get("avg_work_hours"), 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = _validate_month(month)
    items = list(db.attendance_logs.aggregate(_late_leaderboard_pipeline(start, end, limit, department), hint="attendance_date_late"))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start_date, end_date = _valid_date(from_date, "from"), _valid_date(to_date, "to")
    if end_date < start_date:
        raise HTTPException(422, "to must be on or after from")
    if (end_date - start_date).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    pipeline = _department_trend_pipeline(department, start_date.isoformat(), end_date.isoformat())
    items = list(db.employees.aggregate(pipeline, hint="employees_department_joined"))
    for item in items:
        item["attendance_rate"] = _round_half_up(item.get("attendance_rate"), 4)
        item["moving_avg_7d"] = _round_half_up(item.get("moving_avg_7d"), 4)
        item["present_count"] = _round_half_up(item.get("present_count", 0))
    return {"department": department, "items": items}


def _explain_command(collection_name: str, pipeline: list, hint: str) -> dict:
    collection = db[collection_name]
    command = {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}, "hint": hint}
    return db.command({"explain": command, "verbosity": "executionStats"})


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                     from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"),
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        match = {}
        if emp_code: match["emp_code"] = emp_code
        if date_from: match["date"] = {"$gte": _valid_date(date_from, "date_from").isoformat()}
        if date_to:
            _valid_date(date_to, "date_to")
            match.setdefault("date", {})["$lte"] = date_to
        if status:
            if status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}: raise HTTPException(422, "invalid status")
            match["status"] = status
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be on or before date_to")
        pipeline = [{"$match": match}, {"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page-1)*page_size}, {"$limit": page_size}]
        coll, hint = "attendance_logs", "attendance_date_employee_sort"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        start, end = _validate_month(month)
        employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
        if employee is None:
            raise HTTPException(404, "employee not found")
        effective_start = max(start, employee.get("joined_on", start))
        pipeline, coll, hint = _employee_monthly_pipeline(emp_code, effective_start, end), "attendance_logs", "uq_attendance_employee_date"
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        start, end = _validate_month(month)
        pipeline, coll, hint = _department_summary_pipeline(start, end, department), "employees", "employees_joined_department"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        start, end = _validate_month(month)
        pipeline, coll, hint = _late_leaderboard_pipeline(start, end, limit, department), "attendance_logs", "attendance_date_late"
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
        start_date, end_date = _valid_date(from_date, "from"), _valid_date(to_date, "to")
        if end_date < start_date or (end_date - start_date).days + 1 > 92: raise HTTPException(422, "invalid date range")
        pipeline, coll, hint = _department_trend_pipeline(department, start_date.isoformat(), end_date.isoformat()), "employees", "employees_department_joined"
    else:
        raise HTTPException(422, "unsupported endpoint")
    try:
        explain = _explain_command(coll, pipeline, hint)
    except Exception as exc:
        raise HTTPException(422, f"explain unavailable for this query: {str(exc)[:200]}")
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(explain))}
