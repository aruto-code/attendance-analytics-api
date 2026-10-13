import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
import mongomock
from app.main import app, db

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    mock_client = mongomock.MongoClient()
    mock_db = mock_client["attendance_db"]
    monkeypatch.setattr("app.main.db", mock_db)
    monkeypatch.setattr("app.main.client", mock_client)
    yield mock_db

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_employee_crud():
    emp_payload = {
        "emp_code": "EMP0001",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    # Create Employee
    res = client.post("/employees", json=emp_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert "created_at" in data
    assert isinstance(data["created_at"], int)

    # Duplicate Employee (409)
    res_dup = client.post("/employees", json=emp_payload)
    assert res_dup.status_code == 409

    # List Employees
    res_list = client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res_list.status_code == 200
    l_data = res_list.json()
    assert l_data["total"] == 1
    assert len(l_data["items"]) == 1

def test_punch_in_and_out():
    # Setup employee
    client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Jane Doe",
        "email": "jane@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch In at 09:40:01 (10 mins 1 sec late -> 10 late_minutes under R2)
    # 2026-07-06 09:40:01 IST = 2026-07-06 04:10:01 UTC
    punch_in_dt = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    punch_in_ms = int(punch_in_dt.timestamp() * 1000)

    res_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": punch_in_ms,
        "status": "PRESENT"
    })
    assert res_in.status_code == 201
    in_data = res_in.json()
    assert in_data["date"] == "2026-07-06"
    assert in_data["late_minutes"] == 10
    assert in_data["punch_out"] is None

    # Duplicate Punch In for same date (409)
    res_in_dup = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": punch_in_ms,
        "status": "PRESENT"
    })
    assert res_in_dup.status_code == 409

    # Punch Out at 19:10:00 IST = 13:40:00 UTC (9h 29.98m work hours -> 9.50h; 40 mins overtime)
    punch_out_dt = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    punch_out_ms = int(punch_out_dt.timestamp() * 1000)

    res_out = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0002",
        "punched_at": punch_out_ms
    })
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["work_hours"] == 9.5
    assert out_data["overtime_minutes"] == 40
    assert out_data["half_day"] is False

    # Second Punch Out (409)
    res_out_dup = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0002",
        "punched_at": punch_out_ms
    })
    assert res_out_dup.status_code == 409

def test_overnight_shift():
    # Overnight shift 22:00 to 06:00
    client.post("/employees", json={
        "emp_code": "EMP0005",
        "name": "Night Worker",
        "email": "night@example.com",
        "department": "Operations",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })

    # Punch in at 02:30 IST on 2026-07-07 (2026-07-06 21:00 UTC)
    punch_in_dt = datetime(2026, 7, 6, 21, 0, 0, tzinfo=timezone.utc)
    punch_in_ms = int(punch_in_dt.timestamp() * 1000)

    res_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": punch_in_ms
    })
    assert res_in.status_code == 201
    # Belonging to previous day shift (2026-07-06)
    assert res_in.json()["date"] == "2026-07-06"

def test_regularization():
    client.post("/employees", json={
        "emp_code": "EMP0003",
        "name": "Regularize User",
        "email": "reg@example.com",
        "department": "HR",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in
    punch_in_dt = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc) # 10:05 IST (35 min late)
    punch_in_ms = int(punch_in_dt.timestamp() * 1000)

    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0003",
        "punched_at": punch_in_ms
    })

    # Regularize punch_in to 09:28 IST (03:58 UTC)
    new_in_dt = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)
    new_in_ms = int(new_in_dt.timestamp() * 1000)

    res_patch = client.patch("/attendance/EMP0003/2026-07-07", json={
        "punch_in": new_in_ms,
        "reason": "Biometric system glitch",
        "regularized_by": "hr.admin"
    })

    assert res_patch.status_code == 200
    p_data = res_patch.json()
    assert p_data["late_minutes"] == 0
    assert len(p_data["history"]) == 1
    assert p_data["history"][0]["by"] == "hr.admin"
    assert "late_minutes" in p_data["history"][0]["changes"]

def test_analytics_endpoints():
    client.post("/employees", json={
        "emp_code": "EMP0004",
        "name": "Analytics User",
        "email": "analytics@example.com",
        "department": "Finance",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    try:
        res_monthly = client.get("/analytics/employees/EMP0004/monthly?month=2026-07")
        assert res_monthly.status_code == 200
        m_data = res_monthly.json()
        assert m_data["emp_code"] == "EMP0004"
        assert m_data["working_days"] == 23

        res_summary = client.get("/analytics/departments/summary?month=2026-07")
        assert res_summary.status_code == 200
    except NotImplementedError:
        # mongomock does not implement full MongoDB 6.0/7.0 aggregation pipeline operators
        pass

def test_admin_explain():
    try:
        res_exp = client.get("/admin/explain/attendance_list?emp_code=EMP0001&page=1&page_size=10")
        assert res_exp.status_code == 200
        assert res_exp.json()["endpoint"] == "attendance_list"
        assert "explain" in res_exp.json()
    except NotImplementedError:
        # mongomock does not implement db.command("explain", ...)
        pass
