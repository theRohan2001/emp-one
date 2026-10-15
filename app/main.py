"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

"""
import os
import calendar
import json
from contextlib import asynccontextmanager
from bson import json_util
from datetime import datetime, timedelta, timezone, date, time
from typing import Optional, Literal, Annotated, Any
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator, AfterValidator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, ConnectionFailure

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)
MAX_DAYS = 92

# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", DESCENDING)], name="ix_dep_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_joined")

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_date_emp"

    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_emp_punch_in"
    )
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def str_to_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)

def compute_late_minutes(punch_in: datetime, att_date: date, shift_start: str) -> int:
    start_datetime = datetime.combine(att_date, str_to_hhmm(shift_start), tzinfo=IST)
    seconds = (punch_in - start_datetime).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: exact seconds / 3600, rounded to 2 decimals HALF-UP.
    Decimal arithmetic on whole seconds: no float error, no banker's rounding."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_overtime(punch_out: datetime, att_date: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes (floor) between shift_end and punch_out, only when >= 30, else 0."""

    end = datetime.combine(date.fromisoformat(att_date), str_to_hhmm(shift_end), tzinfo=IST)
    if str_to_hhmm(shift_end) <= str_to_hhmm(shift_start):
        end += timedelta(days=1)               
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0

def attendance_list_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """The find command of GET /attendance: filter, sort, skip and limit in one place."""
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return {
        "find": "attendance_logs",
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> integer epoch milliseconds (exact, no float math)."""
    if dt is None:
        return None
    if dt.tzinfo is None: 
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)

def _ms(v):
    """Convert datetimes to epoch ms; pass everything else (None, ints, strings) through."""
    return to_epoch_ms(v) if isinstance(v, datetime) else v

def epoch_to_datetime(epoch_ms: Optional[int]) -> datetime:
    """Punch instant as UTC-aware datetime, truncated to whole seconds (R1)."""
    if epoch_ms is None:  
        return datetime.now(timezone.utc).replace(microsecond=0)
    return datetime.fromtimestamp(epoch_ms // 1000, tz=timezone.utc)


def attendance_date(ts: datetime, shift_start: str, shift_end: str) -> date:
    """R1: the IST calendar date this punch-in belongs to (overnight shifts included)."""
    ist = ts.astimezone(IST)
    d = ist.date()
    overnight = str_to_hhmm(shift_end) <= str_to_hhmm(shift_start)
    if overnight and ist.time() < str_to_hhmm(shift_end):
        d -= timedelta(days=1)
    return d

def check_real_date(v: str) -> str:
    date.fromisoformat(v)
    return v

def offset_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def derive(status: str, punch_in, punch_out, att_date: date, emp: dict):

    if status in {"ABSENT", "LEAVE"}:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late_minutes = compute_late_minutes(punch_in, att_date, emp["shift_start"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late_minutes, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    overtime_minutes = compute_overtime(punch_out, att_date.isoformat(), emp["shift_start"], emp["shift_end"])

    return{
        "work_hours": float(work_hours), 
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes, 
        "half_day": work_hours < Decimal("4.50")
    }

def attendance_regularization(attendance: dict, emp: dict, body) -> tuple[dict, dict]:
    old_status = attendance["status"]
    old_punch_in = offset_aware(attendance.get("punch_in"))
    old_punch_out = offset_aware(attendance.get("punch_out"))
    new_status = body.status if body.status is not None else old_status

    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times are not required as the employee is absent or on leave")
        new_punch_in = new_punch_out = None

    else:
        new_punch_in = epoch_to_datetime(body.punch_in) if body.punch_in is not None else old_punch_in
        new_punch_out = epoch_to_datetime(body.punch_out) if body.punch_out is not None else old_punch_out

        if new_punch_in is None:
            raise HTTPException(422, "presence status requires a punch_in")
        if attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"]).isoformat() != attendance["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_punch_out is not None and (new_punch_out <= new_punch_in or (new_punch_out - new_punch_in) > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24hours of it")

    if (new_status, new_punch_in, new_punch_out) == (old_status, old_punch_in, old_punch_out):
        raise HTTPException(422, "request changes nothing")

    old = {
        "status": old_status, "punch_in": old_punch_in, "punch_out": old_punch_out,
        "work_hours": attendance["work_hours"],
        "late_minutes": attendance.get("late_minutes", 0),
        "overtime_minutes": attendance.get("overtime_minutes", 0),
        "half_day": attendance.get("half_day", False)
    }

    att_date = date.fromisoformat(attendance["date"])

    new = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        **derive(new_status, new_punch_in, new_punch_out, att_date, emp)
    }

    changes = {
        change: {"from": old[change], "to": new[change]} for change in new if new[change] != old[change]
    }

    return new, changes

def count_working_days(month: str, joined_on: str) -> int:
    year, mon = int(month[:4]), int(month[5:7])
    last_date = date(year, mon, calendar.monthrange(year, mon)[1])
    start_date = max(date(year, mon, 1), date.fromisoformat(joined_on))
    days = 0
    d = start_date

    while d <= last_date:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days

def round_half_up(value: Decimal, places: str = "0.01") -> Decimal:
    """R8: 2 decimals HALF-UP (not banker's rounding, not float rounding)."""
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)

def employee_monthly_pipeline(emp_code: Optional[str], month: Optional[str]) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},

        {"$addFields": {"dow": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", PRESENCE]}, {"$lte": ["$dow", 5]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]

def department_summary_pipeline(department: Optional[str] , month: str) -> list:

    year, mon = int(month[:4]), int(month[5:7])
    first = f"{month}-01"
    last = f"{month}-{calendar.monthrange(year, mon)[1]:02d}" 

    emp_match: dict[str, Any] = {"joined_on": {"$lte": last}}     
    if department is not None:
        emp_match["department"] = department                      
     
    is_present = {"$in": ["$status", PRESENCE]}
    has_hours = {"$and": [is_present, {"$isNumber": "$work_hours"}]}
     
    per_employee_logs = [
        {"$match": {"$expr": {"$and": [
            {"$eq": ["$emp_code", "$$code"]},        
            {"$gte": ["$date", first]},
            {"$lte": ["$date", last]},
        ]}}},
        {"$addFields": {"dow": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [is_present, {"$lte": ["$dow", 5]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},               # $sum ignores missing fields
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "hours_hundredths": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
            "hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
        }},
    ]
     
    metrics = ["present_days", "late_count", "total_late_minutes", "leave_count", "on_duty_count",
                "hours_hundredths", "hours_count"]
    return [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": per_employee_logs, "as": "logs"}},
        {"$addFields": {m: {"$sum": f"$logs.{m}"} for m in metrics}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, **{m: {"$sum": f"${m}"} for m in metrics}}},
        {"$sort": {"_id": ASCENDING}},
    ]

def late_leaderboard_pipeline(department: Optional[str], month: str, limit: Optional[int]) -> list:
    year, mon = int(month[:4]), int(month[5:7])
    first = f"{month}-01"
    last = f"{month}-{calendar.monthrange(year, mon)[1]:02d}"
     
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]   
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline

def _utc_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
 
 
def trend_pipeline(department: Optional[str], emp_codes: list, d_from: date, d_to: date) -> list:
    start = _utc_midnight(d_from)
    end_exclusive = _utc_midnight(d_to + timedelta(days=1))
    sfrom, sto = d_from.isoformat(), d_to.isoformat()
    is_present = {"$in": ["$status", PRESENCE]}

    day_counts = [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": sfrom, "$lte": sto}}},
        {"$group": {
            "_id": "$date",
            "present_halves": {"$sum": {"$cond": [
                is_present, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "present_halves": 1, "late_count": 1,
                      "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
    ]
 
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "day": {"$literal": start},
                      "present_halves": {"$literal": 0}, "late_count": {"$literal": 0}}},
        {"$unionWith": {"coll": "attendance_logs", "pipeline": day_counts}},
        {"$group": {"_id": "$day", "present_halves": {"$sum": "$present_halves"}, "late_count": {"$sum": "$late_count"}}},
        {"$project": {"_id": 0, "day": "$_id", "present_halves": 1, "late_count": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start, end_exclusive]}}},
        {"$addFields": {
            "present_halves": {"$ifNull": ["$present_halves", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},   # Mon-Fri
        }},

        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                         {"$count": "n"}],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$addFields": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$toInt": {"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$present_halves", 10000]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]}]}}},
            None]}}},
        {"$addFields": {"has_rate": {"$cond": [{"$ne": ["$rate_int", None]}, 1, 0]}}},

        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "win_sum": {"$sum": "$rate_int", "window": {"documents": [-6, 0]}},
            "win_cnt": {"$sum": "$has_rate", "window": {"documents": [-6, 0]}},
        }}},

        {"$addFields": {"moving_int": {"$cond": [
            {"$gt": ["$win_cnt", 0]},
            {"$toInt": {"$floor": {"$divide": [
                {"$add": [{"$multiply": [2, "$win_sum"]}, "$win_cnt"]},
                {"$multiply": [2, "$win_cnt"]}]}}},
            None]}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "late_count": 1,
            "present_count": {"$divide": ["$present_halves", 2]},
            "attendance_rate": {"$cond": [{"$eq": ["$rate_int", None]}, None, {"$divide": ["$rate_int", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$moving_int", None]}, None, {"$divide": ["$moving_int", 10000]}]},
        }},
        {"$sort": {"date": 1}},
    ]
 
 
def parse_trend_range(date_from: str, date_to: str) -> tuple:
    """Validated (from, to) dates; 422 when to < from or the range exceeds 92 days. Shared with /admin/explain."""
    d_from, d_to = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if d_to < d_from:
        raise HTTPException(422, "to must not be before from")
    if (d_to - d_from).days + 1 > MAX_DAYS:                 # inclusive day count: 92 rows allowed, 93 rejected
        raise HTTPException(422, f"range must not exceed {MAX_DAYS} days")
    return d_from, d_to

def _need(**params):
    """422 when a parameter this endpoint requires is missing."""
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")
 
 
def _explain(command: dict) -> dict:
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(raw))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
## Request Models
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="24-hour HH:MM, IST.")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="24-hour HH:MM, IST. <= shift_start means an overnight shift.")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="First day of employment, YYYY-MM-DD.")

    @field_validator("joined_on")
    @classmethod
    def check_real_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a real calender date (YYYY-MM-DD)")
        return v

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD"), AfterValidator(check_real_date)]

MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000
EpochMillis = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]

Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] 
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"] 

class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"

class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None

class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=200)

## Response Models
class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: EpochMillis

    @field_validator("created_at", mode="before")
    @classmethod
    def created_at_to_ms(cls, v):
        return to_epoch_ms(v) if isinstance(v, datetime) else v

class EmployeePage(BaseModel):
    items: list[EmployeeOut]
    total: int
    page: int
    page_size: int


class Changes(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    
    from_: Any = Field(alias="from")
    to: Any

class HistoryEntry(BaseModel):
    at: EpochMillis
    by: str
    reason: str
    changes: dict[str, Changes]

    @model_validator(mode="before")
    @classmethod
    def datetime_to_epoch_ms(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        data["at"] = _ms(data.get("at"))
        data["changes"] = {
            key: {side: (_ms(ch.get(side)) if key in ("punch_in", "punch_out") else ch.get(side))
                  for side in ("from", "to")}
            for key, ch in data.get("changes", {}).items()
        }
        return data

class AttendanceRecord(BaseModel):
    emp_code: str
    date: str = Field(description="The attendance day (R1), YYYY-MM-DD.")
    status: Status
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    work_hours: Optional[float] = Field(default=None, description="Computed (R4). Null until punch-out.")
    late_minutes: int = Field(default=0, description="Computed (R2).")
    overtime_minutes: int = Field(default=0, description="Computed (R3).")
    half_day: bool = Field(default=False, description="Computed (R5).")
    history: list[HistoryEntry] = Field(default_factory=list)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def punchs_to_epoch_ms(cls, v):
        return _ms(v)


class AttendancePage(BaseModel):
    items: list[AttendanceRecord]
    total: int
    page: int
    page_size: int

class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]

class DepartmentSummary(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int

class DepartmentSummaryPage(BaseModel):
    month: str
    items: list[DepartmentSummary]

class Leaderboard(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int

class LeaderboardPage(BaseModel):
    month: str
    items: list[Leaderboard]

class Trend(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]

class TrendPage(BaseModel):
    department: str
    items: list[Trend]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"], summary="Readiness probe")
def health():
    try:
        client.admin.command('ping')
        return {"status": "ok"} 
    except ConnectionFailure:
        raise HTTPException(503, "MongDB server is not available")


@app.post(
        "/employees", 
        status_code=201, 
        response_model=EmployeeOut, 
        tags=["employees"],
        summary="Create an employee"
)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "employee already exist")
    return doc


@app.get(
        "/employees", 
        response_model=EmployeePage, 
        tags=["employees"],
        summary="List employees"
)
def list_employees(
    department: Optional[str] = None, 
    page: int = Query(default=1, ge=1), 
    page_size: int = Query(default=20, ge=1, le=100)
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size ## documented 
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord, tags= ["attendance"], summary="Record a punch-in")
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee doesn't exist") ## documented
    
    ts = epoch_to_datetime(body.punched_at)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": d.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for the day")
    
    return doc


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"], summary="List attendance records")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[IsoDate] = None,
    date_to: Optional[IsoDate] = None,
    status: Optional[Status] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    attendance_cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {"items": list(attendance_cursor), "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #

@app.post(
        "/attendance/punch-out", 
        response_model=AttendanceRecord, 
        tags=["attendance"],
        summary="Record a punch-out"
)
def punchOut(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = epoch_to_datetime(body.punched_at)

    attendance = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)]
    )

    if attendance is None:
        raise HTTPException(404, "no punch-in found")

    punch_in = attendance["punch_in"]
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)

    if ts <= punch_in or (ts - punch_in) > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")

    work_hours = compute_work_hours(punch_in, ts)
    doc = {
        "punch_out": ts,
        "work_hours": float(work_hours),
        "overtime_minutes": compute_overtime(ts, attendance["date"], emp["shift_start"], emp["shift_end"]),
        "half_day": work_hours < Decimal("4.50")
    }

    updated = db.attendance_logs.find_one_and_update(
        {"_id": attendance["_id"], "punch_out": None}, 
        {'$set': doc},
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(409, "already punched out")
    
    return updated

@app.patch(
        "/attendance/{emp_code}/{date}", 
        response_model=AttendanceRecord, 
        tags=["attendance"],
        summary="Regularize (manually correct) one day's record"
)
def regularizeAttendance(
    emp_code: str,
    date: IsoDate,
    body: RegularizeRequest
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    attendance = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})

    if attendance is None:
        raise HTTPException(404, "employee attendance record not found on this date")

    doc, changes = attendance_regularization(attendance, emp, body)

    entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    updated = db.attendance_logs.find_one_and_update(
            {"_id": attendance["_id"]}, 
            {'$push': {
                "history": entry
            },
            "$set": doc
            },
            return_document=ReturnDocument.AFTER
        )

    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return updated


@app.get(
        "/analytics/employees/{emp_code}/monthly", 
        response_model=EmployeeMonthly, 
        tags=["analytics"],
        summary="Monthly attendance summary for one employee"
)
def employeeMonthly(
    emp_code: str, 
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    working_days = count_working_days(month, emp["joined_on"])

    docs = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    agg = docs[0] if docs else {}

    present_days = Decimal(str(agg.get("present_days", 0)))

    if working_days == 0:
        pct = None
    else:
        pct = float(round_half_up(present_days / Decimal(working_days) * 100))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(round_half_up(present_days)),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }

@app.get(
        "/analytics/departments/summary", 
        response_model=DepartmentSummaryPage, 
        tags=["analytics"],
        summary="Department summary for a month"
)
def departmentSummary(
    department: Optional[str],
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):

    items = []
    for row in db.employees.aggregate(department_summary_pipeline(department, month)):
        count = row["hours_count"]
        if count == 0:
            avg = None
        else:
            avg = float(round_half_up(Decimal(row["hours_hundredths"]) / Decimal(count) / 100))

        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": float(round_half_up(Decimal(str(row["present_days"])))),
            "avg_work_hours": avg,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })

    return {"month": month, "items": items}

@app.get(
        "/analytics/leaderboard/late", 
        response_model=LeaderboardPage, 
        tags=["analytics"],
        summary="Late-comers leaderboard for a month"
)
def lateLeaderboard(
    department: Optional[str] = None,
    limit: Optional[int] = Query(default=10, ge=1, le=50),
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(department, month, limit)))

    return {"month": month, "items": items}

@app.get(
        "/analytics/departments/{department}/trend", 
        response_model=TrendPage, 
        tags=["analytics"], 
        summary="Daily attendance trend for a department"
)
def deapartmentTrend(
    department: str,
    date_from: Annotated[IsoDate, Query(alias="from")],
    date_to : Annotated[IsoDate, Query(alias="to")]
): 
    
    d_from, d_to = parse_trend_range(date_from, date_to)
 
    emp_codes = db.employees.distinct("emp_code", {"department": department})
    if not emp_codes:
        raise HTTPException(404, "department not found")
 
    items = list(db.employees.aggregate(trend_pipeline(department, emp_codes, d_from, d_to)))
    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}", tags=["admin"], summary="MongoDB explain for an endpoint's main query")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[IsoDate] = None,
    date_to: Optional[IsoDate] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[IsoDate], Query(alias="from")] = None,
    to: Optional[IsoDate] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        collection = "attendance_logs"
        command = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
 
    elif endpoint == "employee_monthly":
        _need(emp_code=emp_code, month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, month), "cursor": {}}
 
    elif endpoint == "department_summary":
        _need(month=month)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": department_summary_pipeline(department, month), "cursor": {}} # type: ignore
 
    elif endpoint == "late_leaderboard":
        _need(month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": late_leaderboard_pipeline(department, month, limit), "cursor": {}} # type: ignore
 
    else:  # department_trend
        _need(department=department, **{"from": from_, "to": to})
        d_from, d_to = parse_trend_range(str(from_), str(to))                    
        emp_codes = db.employees.distinct("emp_code", {"department": department})
        collection = "employees"
        command = {"aggregate": collection, "pipeline": trend_pipeline(department, emp_codes, d_from, d_to), "cursor": {}}
 
    return {"endpoint": endpoint, "collection": collection, "explain": _explain(command)}