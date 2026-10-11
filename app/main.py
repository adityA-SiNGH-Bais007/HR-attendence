"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Lifespan: Startup Index Creation (Idempotent)
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure indexes on startup
    try:
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index("department")
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index("status")
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    except Exception as e:
        print(f"Index creation warning: {e}")
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Instant & Datetime Conversion Helpers
# --------------------------------------------------------------------------- #
def ensure_aware_utc(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware in UTC (PyMongo returns naive UTC datetimes)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def truncate_ms_to_seconds(ms: int) -> int:
    """R1: Before any calculation, instants are truncated to whole seconds."""
    return (ms // 1000) * 1000


def ms_to_utc_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds (truncated to seconds) to UTC datetime."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert BSON datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    aware = ensure_aware_utc(dt)
    return int(aware.timestamp() * 1000)


# --------------------------------------------------------------------------- #
# Business Logic Helpers (Rules R1 - R5)
# --------------------------------------------------------------------------- #
def get_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is strictly earlier than shift_end belongs to the
    shift that started the previous day.
    """
    time_str = punch_in_ist.strftime("%H:%M")
    is_overnight = shift_end <= shift_start
    if is_overnight and time_str < shift_end:
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes (600 seconds).
    Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    sh_h, sh_m = map(int, shift_start.split(":"))
    year, month, day = map(int, attendance_date.split("-"))
    shift_start_dt = datetime(year, month, day, sh_h, sh_m, 0, tzinfo=IST)

    punch_aware = ensure_aware_utc(punch_in_utc)
    elapsed_seconds = (punch_aware - shift_start_dt).total_seconds()

    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up
    (not banker's rounding, not float rounding).
    """
    p_in = ensure_aware_utc(punch_in_utc)
    p_out = ensure_aware_utc(punch_out_utc)
    diff_sec = Decimal(str((p_out - p_in).total_seconds()))
    hours = diff_sec / Decimal("3600")
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Overtime = whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    Handles overnight shifts (shift_end falls on next calendar day).
    """
    end_h, end_m = map(int, shift_end.split(":"))
    year, month, day = map(int, attendance_date.split("-"))
    shift_end_dt = datetime(year, month, day, end_h, end_m, 0, tzinfo=IST)

    if shift_end <= shift_start:
        shift_end_dt += timedelta(days=1)

    p_out = ensure_aware_utc(punch_out_utc)
    elapsed_seconds = (p_out - shift_end_dt).total_seconds()
    minutes = int(elapsed_seconds // 60)

    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_history_entry(entry: dict) -> dict:
    """Convert MongoDB history entry to OpenAPI HistoryEntry format."""
    at = entry.get("at")
    if isinstance(at, datetime):
        at_ms = datetime_to_ms(at)
    else:
        at_ms = at

    serialized_changes = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = datetime_to_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = datetime_to_ms(to_val)
        serialized_changes[k] = {"from": from_val, "to": to_val}

    return {
        "at": at_ms,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": serialized_changes,
    }


def format_attendance_record(doc: dict) -> dict:
    """Format MongoDB attendance document into OpenAPI AttendanceRecord response."""
    history = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_ms(doc.get("punch_in")),
        "punch_out": datetime_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v: str, info):
        if "shift_start" in info.data and v == info.data["shift_start"]:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInIn(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unreachable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc = body.model_dump()
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = datetime_to_ms(now_utc)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)

    items = []
    for emp in cursor:
        if isinstance(emp.get("created_at"), datetime):
            emp["created_at"] = datetime_to_ms(emp["created_at"])
        items.append(emp)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        truncated_ms = truncate_ms_to_seconds(body.punched_at)
    else:
        truncated_ms = int(time.time()) * 1000

    punch_in_utc = ms_to_utc_datetime(truncated_ms)
    punch_in_ist = punch_in_utc.astimezone(IST)

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    att_date = get_attendance_date(punch_in_ist, shift_start, shift_end)
    late_min = compute_late_minutes(punch_in_utc, shift_start, att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        truncated_ms = truncate_ms_to_seconds(body.punched_at)
    else:
        truncated_ms = int(time.time()) * 1000

    punch_out_utc = ms_to_utc_datetime(truncated_ms)

    # Find the employee's most recent record that has a punch-in
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    p_in_aware = ensure_aware_utc(record["punch_in"])
    diff_seconds = (punch_out_utc - p_in_aware).total_seconds()

    # Rule: 422 if punched_at <= punch_in or more than 24 hours after it
    if diff_seconds <= 0 or diff_seconds > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly after punch_in and within 24 hours",
        )

    # Rule: 409 if already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    work_hours = compute_work_hours(record["punch_in"], punch_out_utc)
    overtime_minutes = compute_overtime(
        punch_out_utc, emp["shift_start"], emp["shift_end"], record["date"]
    )
    half_day = compute_half_day(work_hours)

    # Concurrency safe update: exactly one of two simultaneous requests gets 200, the other 409
    updated_record = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_record:
        raise HTTPException(status_code=409, detail="Record already punched out")

    return format_attendance_record(updated_record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)

    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_record(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    effective_status = body.status if body.status is not None else record["status"]

    if effective_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch_in or punch_out when status is ABSENT or LEAVE",
            )
        target_punch_in = None
        target_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            target_punch_in = ms_to_utc_datetime(truncate_ms_to_seconds(body.punch_in))
        else:
            target_punch_in = record.get("punch_in")

        if target_punch_in is None:
            raise HTTPException(
                status_code=422, detail="Presence status requires a punch_in"
            )

        # Rule: punch_in must stay on record's attendance date
        target_punch_in_ist = ensure_aware_utc(target_punch_in).astimezone(IST)
        computed_att_date = get_attendance_date(
            target_punch_in_ist, emp["shift_start"], emp["shift_end"]
        )
        if computed_att_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in must stay on the record's attendance date {date}",
            )

        # Punch out validation
        if body.punch_out is not None:
            target_punch_out = ms_to_utc_datetime(truncate_ms_to_seconds(body.punch_out))
        else:
            target_punch_out = record.get("punch_out")

        if target_punch_out is not None:
            diff_seconds = (
                ensure_aware_utc(target_punch_out) - ensure_aware_utc(target_punch_in)
            ).total_seconds()
            if diff_seconds <= 0 or diff_seconds > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly after punch_in and within 24 hours",
                )

    # Recompute derived fields
    if effective_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out is not None:
            new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            new_overtime_minutes = compute_overtime(
                target_punch_out, emp["shift_start"], emp["shift_end"], date
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Determine what actually changed
    mongo_changes = {}

    if record.get("status") != effective_status:
        mongo_changes["status"] = {"from": record.get("status"), "to": effective_status}

    old_in_ms = datetime_to_ms(record.get("punch_in"))
    new_in_ms = datetime_to_ms(target_punch_in)
    if old_in_ms != new_in_ms:
        mongo_changes["punch_in"] = {"from": record.get("punch_in"), "to": target_punch_in}

    old_out_ms = datetime_to_ms(record.get("punch_out"))
    new_out_ms = datetime_to_ms(target_punch_out)
    if old_out_ms != new_out_ms:
        mongo_changes["punch_out"] = {"from": record.get("punch_out"), "to": target_punch_out}

    if record.get("work_hours") != new_work_hours:
        mongo_changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}

    if record.get("late_minutes", 0) != new_late_minutes:
        mongo_changes["late_minutes"] = {
            "from": record.get("late_minutes", 0),
            "to": new_late_minutes,
        }

    if record.get("overtime_minutes", 0) != new_overtime_minutes:
        mongo_changes["overtime_minutes"] = {
            "from": record.get("overtime_minutes", 0),
            "to": new_overtime_minutes,
        }

    if record.get("half_day", False) != new_half_day:
        mongo_changes["half_day"] = {
            "from": record.get("half_day", False),
            "to": new_half_day,
        }

    # Rule: request that changes nothing is 422
    if not mongo_changes:
        raise HTTPException(
            status_code=422, detail="Request made no changes to the record"
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": mongo_changes,
    }

    # Optimistic locking on history length to prevent lost updates under concurrency
    history_len = len(record.get("history", []))
    if history_len == 0:
        match_query = {
            "_id": record["_id"],
            "$or": [{"history": {"$size": 0}}, {"history": {"$exists": False}}],
        }
    else:
        match_query = {
            "_id": record["_id"],
            "history": {"$size": history_len},
        }

    update_doc = {
        "$set": {
            "status": effective_status,
            "punch_in": target_punch_in,
            "punch_out": target_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {
            "history": history_entry,
        },
    }

    updated_record = db.attendance_logs.find_one_and_update(
        match_query,
        update_doc,
        return_document=ReturnDocument.AFTER,
    )

    if not updated_record:
        raise HTTPException(
            status_code=409, detail="Concurrent update conflict on record"
        )

    return format_attendance_record(updated_record)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def get_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    """
    Department summary for a month.
    Structures pipeline starting from employees collection using $lookup so that
    headcount includes employees who have zero attendance logs, while filtering
    out employees who have not joined yet (joined_on <= end_of_month).
    """
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    start_of_month = f"{month}-01"
    end_of_month = f"{month}-{last_day:02d}"

    emp_match = {"joined_on": {"$lte": end_of_month}}
    if department:
        emp_match["department"] = department

    pipeline = [
        # 1. Match employees who joined on or before end of month
        {"$match": emp_match},

        # 2. Lookup attendance logs for this employee in the month
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_of_month]},
                                    {"$lte": ["$date", end_of_month]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },

        # 3. Unwind logs, preserving employees who have zero logs
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },

        # 4. Group by department
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$let": {
                                            "vars": {
                                                "dow": {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": {
                                                                "$ifNull": [
                                                                    "$logs.date",
                                                                    "1970-01-01",
                                                                ]
                                                            },
                                                            "onError": None,
                                                            "onNull": None,
                                                        }
                                                    }
                                                }
                                            },
                                            "in": {
                                                "$and": [
                                                    {"$gte": ["$$dow", 2]},
                                                    {"$lte": ["$$dow", 6]},
                                                ]
                                            },
                                        }
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "avg_work_hours_val": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
            }
        },

        # 5. Project final department summary fields
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": {"$round": ["$present_days", 2]},
                "avg_work_hours": {
                    "$cond": [
                        {"$ne": ["$avg_work_hours_val", None]},
                        {"$round": ["$avg_work_hours_val", 2]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },

        # 6. Omit departments with headcount == 0
        {"$match": {"headcount": {"$gt": 0}}},

        # 7. Sort by department ascending
        {"$sort": {"department": 1}},
    ]
    return pipeline


def get_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    """
    Late-comers leaderboard for a month.
    Uses $setWindowFields with $rank so tied employees share a rank and the next
    number is skipped (e.g. 1, 2, 2, 4). Limit is applied after ranking.
    """
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    start_of_month = f"{month}-01"
    end_of_month = f"{month}-{last_day:02d}"

    pipeline = [
        # 1. Match attendance logs in month with late_minutes > 0
        {
            "$match": {
                "date": {"$gte": start_of_month, "$lte": end_of_month},
                "late_minutes": {"$gt": 0},
            }
        },

        # 2. Group by emp_code to get total late minutes and late count
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },

        # 3. Lookup employee details
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },

        # 4. Ignore logs whose employee does not exist
        {"$unwind": "$emp"},
    ]

    # 5. If department is given, ranking happens inside that department only
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    # 6. Project and apply window ranking
    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },

        # 7. Standard competition ranking with ties (1, 2, 2, 4) using $rank
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                }
            }
        },

        # 8. Limit applied after ranking (rank <= limit)
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },

        # 9. Order of rows: total_late_minutes descending, then emp_code ascending
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        }
    ])

def get_department_trend_pipeline(department: str, from_date: str, to_date: str, dept_emp_codes: list) -> list:
    """
    Daily attendance trend for a department.
    Continuous daily series with gap-filling using $densify and a 7-day moving average
    using $setWindowFields, entirely implemented using MongoDB aggregation operators.
    """
    from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    to_dt_plus_one = to_dt + timedelta(days=1)

    pipeline = [
        # 1. Match attendance logs for the department in the date range
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
                "emp_code": {"$in": dept_emp_codes},
            }
        },

        # 2. Group by date to aggregate daily attendance metrics
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },

        # 3. Project date_dt for densification
        {
            "$project": {
                "_id": 0,
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },

        # 4. Guarantee at least one anchor document at from_dt so $densify always triggers
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date_dt": {"$literal": from_dt},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },

        # 5. Group by date_dt to merge anchor with existing log (if any)
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },

        # 6. Gap-filling: $densify over date_dt from from_dt to to_dt
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_one],
                },
            }
        },

        # 7. Convert date_dt to date string, zero out missing fields
        {
            "$project": {
                "date_dt": 1,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },

        # 8. Compute daily headcount (employees in department joined on or before this day)
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_str"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },

        # 9. Compute is_working_day and attendance_rate
        {
            "$project": {
                "date_dt": 1,
                "date": 1,
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
                "present_count": {"$round": ["$present_count", 2]},
                "late_count": 1,
                "is_working_day": {
                    "$let": {
                        "vars": {
                            "dow": {"$dayOfWeek": "$date_dt"}
                        },
                        "in": {
                            "$and": [
                                {"$gte": ["$$dow", 2]},
                                {"$lte": ["$$dow", 6]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "date_dt": 1,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                },
            }
        },

        # 10. Compute 7-day moving average using $setWindowFields
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },

        # 11. Final projection and sorting
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d_raw", None]},
                        {"$round": ["$moving_avg_7d_raw", 4]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = get_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = get_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    emp_exists = db.employees.find_one({"department": department})
    if not emp_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    if to_dt < from_dt:
        raise HTTPException(status_code=422, detail="to date must be >= from date")

    days_count = (to_dt - from_dt).days + 1
    if days_count > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    dept_emp_codes = [
        doc["emp_code"]
        for doc in db.employees.find({"department": department}, {"emp_code": 1})
    ]

    pipeline = get_department_trend_pipeline(department, from_date, to_date, dept_emp_codes)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


def compute_working_days(month: str, joined_on: str) -> int:
    """
    R7: Mon-Fri, no holiday calendar. For mid-month joiner, counted from joined_on.
    """
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    start_dt = datetime.strptime(f"{month}-01", "%Y-%m-%d").date()
    end_dt = datetime.strptime(f"{month}-{last_day:02d}", "%Y-%m-%d").date()
    join_dt = datetime.strptime(joined_on, "%Y-%m-%d").date()

    if join_dt > end_dt:
        return 0

    effective_start = max(start_dt, join_dt)
    working_days = 0
    cur = effective_start
    while cur <= end_dt:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def get_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    """Monthly attendance summary aggregation for one employee."""
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    start_of_month = f"{month}-01"
    end_of_month = f"{month}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_of_month, "$lte": end_of_month},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$let": {
                                            "vars": {
                                                "dow": {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": {
                                                                "$ifNull": [
                                                                    "$date",
                                                                    "1970-01-01",
                                                                ]
                                                            },
                                                            "onError": None,
                                                            "onNull": None,
                                                        }
                                                    }
                                                }
                                            },
                                            "in": {
                                                "$and": [
                                                    {"$gte": ["$$dow", 2]},
                                                    {"$lte": ["$$dow", 6]},
                                                ]
                                            },
                                        }
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
            }
        },
    ]
    return pipeline


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = compute_working_days(month, emp["joined_on"])
    pipeline = get_employee_monthly_pipeline(emp_code, month)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = round(float(res.get("present_days", 0)), 2)
        leave_days = res.get("leave_days", 0)
        late_count = res.get("late_count", 0)
        total_late = res.get("total_late_minutes", 0)
        total_ot = res.get("total_overtime_minutes", 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = float(
            Decimal(str(present_days / working_days * 100)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": collection, "explain": explain}

    elif endpoint == "employee_monthly":
        collection = "attendance_logs"
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = get_employee_monthly_pipeline(emp_code, month)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": collection, "explain": explain}

    elif endpoint == "department_summary":
        collection = "employees"
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = get_department_summary_pipeline(month, department)
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": collection, "explain": explain}

    elif endpoint == "late_leaderboard":
        collection = "attendance_logs"
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = get_late_leaderboard_pipeline(month, limit, department)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": collection, "explain": explain}

    elif endpoint == "department_trend":
        collection = "attendance_logs"
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        try:
            f_dt = datetime.strptime(from_date, "%Y-%m-%d")
            t_dt = datetime.strptime(to_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")
        if t_dt < f_dt:
            raise HTTPException(status_code=422, detail="to date must be >= from date")
        if (t_dt - f_dt).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        dept_emp_codes = [
            doc["emp_code"]
            for doc in db.employees.find({"department": department}, {"emp_code": 1})
        ]
        pipeline = get_department_trend_pipeline(department, from_date, to_date, dept_emp_codes)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": collection, "explain": explain}


