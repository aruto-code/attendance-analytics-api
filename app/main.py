"""
Employee Attendance & Analytics API

Starts with: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB (read from environment, with local .env fallback)
"""
import os
import re
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Indexes created at startup
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_db_indexes():
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING)])

        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)])
        db.attendance_logs.create_index([("late_minutes", ASCENDING), ("date", ASCENDING)])
    except Exception as e:
        print(f"Index creation error: {e}")


# --------------------------------------------------------------------------- #
# Helper logic & rules R1 - R10
# --------------------------------------------------------------------------- #
def round_half_up(val: Union[float, Decimal, int], decimals: int = 2) -> float:
    if val is None:
        return None
    d = Decimal(str(val))
    pattern = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(pattern), rounding=ROUND_HALF_UP))


def validate_epoch_ms(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Timestamp must be an integer millisecond count")
    if v < 100000000000 or v > 4102444800000:
        raise ValueError("Timestamp out of range or provided in seconds instead of milliseconds")
    return v


def ensure_utc(dt: datetime) -> datetime:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ms_to_datetime(ms: int) -> datetime:
    # Truncate to whole seconds per R1 before storing/computing
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    dt = ensure_utc(dt)
    return int(dt.timestamp() * 1000)


def parse_time_str(time_str: str) -> tuple[int, int]:
    h, m = map(int, time_str.split(":"))
    return h, m


def get_ist_datetime(dt: datetime) -> datetime:
    dt = ensure_utc(dt)
    return dt.astimezone(IST)


def compute_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    ist_dt = get_ist_datetime(punch_in_dt)
    start_h, start_m = parse_time_str(shift_start)
    end_h, end_m = parse_time_str(shift_end)

    is_overnight = (end_h < start_h) or (end_h == start_h and end_m <= start_m)
    if is_overnight:
        end_time_minutes = end_h * 60 + end_m
        punch_time_minutes = ist_dt.hour * 60 + ist_dt.minute
        if punch_time_minutes < end_time_minutes:
            return (ist_dt.date() - timedelta(days=1)).isoformat()

    return ist_dt.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, date_str: str) -> int:
    ist_dt = get_ist_datetime(punch_in_dt)
    y, m, d = map(int, date_str.split("-"))
    sh, sm = parse_time_str(shift_start)

    shift_start_dt = datetime(y, m, d, sh, sm, 0, tzinfo=IST)
    diff_sec = int((ist_dt - shift_start_dt).total_seconds())

    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    p_in = ensure_utc(punch_in_dt)
    p_out = ensure_utc(punch_out_dt)
    diff_sec = int((p_out - p_in).total_seconds())
    return round_half_up(diff_sec / 3600.0, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_overtime_minutes(punch_out_dt: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    ist_out = get_ist_datetime(punch_out_dt)
    y, m, d = map(int, date_str.split("-"))
    start_h, start_m = parse_time_str(shift_start)
    end_h, end_m = parse_time_str(shift_end)

    is_overnight = (end_h < start_h) or (end_h == start_h and end_m <= start_m)

    if is_overnight:
        shift_end_dt = datetime(y, m, d, end_h, end_m, 0, tzinfo=IST) + timedelta(days=1)
    else:
        shift_end_dt = datetime(y, m, d, end_h, end_m, 0, tzinfo=IST)

    diff_sec = int((ist_out - shift_end_dt).total_seconds())
    if diff_sec <= 0:
        return 0

    ot_mins = diff_sec // 60
    return ot_mins if ot_mins >= 30 else 0


def serialize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return doc
    doc.pop("_id", None)
    for key, val in list(doc.items()):
        if isinstance(val, datetime):
            doc[key] = datetime_to_ms(val)
        elif key == "history" and isinstance(val, list):
            serialized_history = []
            for item in val:
                h_item = dict(item)
                if isinstance(h_item.get("at"), datetime):
                    h_item["at"] = datetime_to_ms(h_item["at"])
                if isinstance(h_item.get("changes"), dict):
                    c_dict = {}
                    for f_key, f_val in h_item["changes"].items():
                        c_entry = dict(f_val)
                        if isinstance(c_entry.get("from"), datetime):
                            c_entry["from"] = datetime_to_ms(c_entry["from"])
                        if isinstance(c_entry.get("to"), datetime):
                            c_entry["to"] = datetime_to_ms(c_entry["to"])
                        c_dict[f_key] = c_entry
                    h_item["changes"] = c_dict
                serialized_history.append(h_item)
            doc["history"] = serialized_history
    return doc


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[Any] = None
    status: str = Field("PRESENT")

    @field_validator("punched_at")
    def check_punched_at(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    def check_status(cls, v):
        if v not in ["PRESENT", "WFH", "ON_DUTY"]:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[Any] = None

    @field_validator("punched_at")
    def check_punched_at(cls, v):
        return validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[Any] = None
    punch_out: Optional[Any] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    def check_epoch_fields(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    def check_status(cls, v):
        if v is not None and v not in ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]:
            raise ValueError("Invalid status value")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize_doc(doc)


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
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punch_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_dt = ms_to_datetime(punch_ms)

    att_date = compute_attendance_date(punch_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": att_date}):
        raise HTTPException(409, "already punched in for this date")

    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punch_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_out_dt = ms_to_datetime(punch_ms)

    # Find candidate records where punch_in <= punch_out_dt
    cursor = db.attendance_logs.find(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}}
    ).sort("punch_in", DESCENDING).limit(1)

    records = list(cursor)
    if not records:
        raise HTTPException(404, "No punch-in found")

    record = records[0]
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")

    rec_punch_in = ensure_utc(record["punch_in"])
    delta_sec = (punch_out_dt - rec_punch_in).total_seconds()
    if delta_sec <= 0 or delta_sec > 86400:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    work_hrs = compute_work_hours(rec_punch_in, punch_out_dt)
    half_d = compute_half_day(work_hrs)
    ot_mins = compute_overtime_minutes(
        punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"]
    )

    update_fields = {
        "punch_out": punch_out_dt,
        "work_hours": work_hrs,
        "half_day": half_d,
        "overtime_minutes": ot_mins,
    }

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update_fields},
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(409, "Record already punched out")

    return serialize_doc(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

    if status and status not in ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]:
        raise HTTPException(422, "Invalid status parameter")

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
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]

    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch times for ABSENT or LEAVE status")
        target_punch_in = None
        target_punch_out = None
    else:
        # Presence status
        if body.punch_in is not None:
            target_punch_in = ms_to_datetime(body.punch_in)
        else:
            target_punch_in = ensure_utc(record.get("punch_in"))

        if not target_punch_in:
            raise HTTPException(422, "Presence status requires a punch_in time")

        # Validate that target_punch_in stays on the record's date
        p_date = compute_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if p_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            target_punch_out = ms_to_datetime(body.punch_out)
        else:
            target_punch_out = ensure_utc(record.get("punch_out"))

        if target_punch_out:
            delta = (target_punch_out - target_punch_in).total_seconds()
            if delta <= 0 or delta > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    # Compute target derived fields
    if new_status in ["ABSENT", "LEAVE"]:
        target_late = 0
        target_work_hours = None
        target_half_day = False
        target_overtime = 0
    else:
        target_late = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out:
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_half_day = compute_half_day(target_work_hours)
            target_overtime = compute_overtime_minutes(
                target_punch_out, emp["shift_start"], emp["shift_end"], date
            )
        else:
            target_work_hours = None
            target_half_day = False
            target_overtime = 0

    target_fields = {
        "status": new_status,
        "punch_in": target_punch_in,
        "punch_out": target_punch_out,
        "work_hours": target_work_hours,
        "late_minutes": target_late,
        "overtime_minutes": target_overtime,
        "half_day": target_half_day,
    }

    # Build changes dictionary
    changes = {}
    for f_name, f_val in target_fields.items():
        curr_val = record.get(f_name)
        if isinstance(curr_val, datetime):
            curr_val = ensure_utc(curr_val)
        if curr_val != f_val:
            changes[f_name] = {"from": curr_val, "to": f_val}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    curr_history = record.get("history", [])
    history_len = len(curr_history)

    # Update with optimistic concurrency control on history size
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": {"$size": history_len}},
        {
            "$set": target_fields,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(409, "Concurrent regularization conflict")

    return serialize_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (100% MongoDB Aggregations)
# --------------------------------------------------------------------------- #
def get_month_range(month_str: str) -> tuple[str, str, int]:
    y, m = map(int, month_str.split("-"))
    start_date = f"{month_str}-01"
    if m == 12:
        next_month_start = datetime(y + 1, 1, 1)
    else:
        next_month_start = datetime(y, m + 1, 1)
    last_day_dt = next_month_start - timedelta(days=1)
    end_date = last_day_dt.strftime("%Y-%m-%d")
    days_in_month = last_day_dt.day
    return start_date, end_date, days_in_month


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    start_date, end_date, days_in_month = get_month_range(month)
    joined_on = emp["joined_on"]

    # Calculate working days (Mon-Fri) in [max(start_date, joined_on), end_date]
    effective_start_str = max(start_date, joined_on)
    working_days = 0
    if effective_start_str <= end_date:
        sy, sm, sd = map(int, effective_start_str.split("-"))
        ey, em, ed = map(int, end_date.split("-"))
        curr = date(sy, sm, sd)
        end_d = date(ey, em, ed)
        while curr <= end_d:
            if curr.weekday() < 5:
                working_days += 1
            curr += timedelta(days=1)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        stats = res[0]
        present_days = round_half_up(stats["present_days"], 2)
        leave_days = stats["leave_days"]
        late_count = stats["late_count"]
        total_late = stats["total_late_minutes"]
        total_overtime = stats["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_overtime = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_overtime,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    start_date, end_date, _ = get_month_range(month)
    match_emp = {"joined_on": {"$lte": end_date}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
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
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "status": "$logs.status",
                "work_hours": "$logs.work_hours",
                "late_minutes": "$logs.late_minutes",
                "half_day": "$logs.half_day",
                "dayOfWeek": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        headcount = len(r["employees"])
        if headcount == 0:
            continue
        p_days = round_half_up(r["present_days"], 2)
        wh_count = r["work_hours_count"]
        avg_wh = round_half_up(r["total_work_hours"] / wh_count, 2) if wh_count > 0 else None

        items.append({
            "department": r["_id"],
            "headcount": headcount,
            "present_days": p_days,
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    start_date, end_date, _ = get_month_range(month)
    pipeline = [
        {"$match": {"date": {"$gte": start_date, "$lte": end_date}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    fy, fm, fd = map(int, from_date.split("-"))
    ty, tm, td = map(int, to_date.split("-"))
    from_dt = datetime(fy, fm, fd, tzinfo=timezone.utc)
    to_dt = datetime(ty, tm, td, tzinfo=timezone.utc) + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$project": {
                "date_obj": from_dt,
                "employees": 1,
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt],
                },
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_obj",
                        "timezone": "UTC",
                    }
                },
                "employees": 1,
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"curr_date": "$date"},
                "pipeline": [
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp_info",
                        }
                    },
                    {"$unwind": "$emp_info"},
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$curr_date"]},
                                    {"$eq": ["$emp_info.department", department]},
                                ]
                            }
                        }
                    },
                ],
                "as": "day_logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$day_logs",
                                    "as": "l",
                                    "cond": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                }
                            },
                            "as": "pl",
                            "in": {"$cond": [{"$eq": ["$$pl.half_day", True]}, 0.5, 1.0]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    if to_date < from_date:
        raise HTTPException(422, "from date cannot be after to date")

    fy, fm, fd = map(int, from_date.split("-"))
    ty, tm, td = map(int, to_date.split("-"))
    delta_days = (date(ty, tm, td) - date(fy, fm, fd)).days + 1
    if delta_days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    raw_items = list(db.employees.aggregate(pipeline))

    items = []
    for r in raw_items:
        att_rate = round_half_up(r["attendance_rate"], 4) if r["attendance_rate"] is not None else None
        m_avg = round_half_up(r["moving_avg_7d"], 4) if r["moving_avg_7d"] is not None else None
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": r["late_count"],
            "attendance_rate": att_rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(422, "Invalid endpoint name")

    if endpoint == "attendance_list":
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
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly explain")
        start_date, end_date, _ = get_month_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
            {
                "$project": {
                    "date": 1,
                    "status": 1,
                    "late_minutes": 1,
                    "overtime_minutes": 1,
                    "half_day": 1,
                    "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                }
            },
        ]
        cmd = {
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month parameter is required for department_summary explain")
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month parameter is required for late_leaderboard explain")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        cmd = {
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to parameters are required for department_trend explain")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        cmd = {
            "explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}
