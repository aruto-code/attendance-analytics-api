# Employee Attendance & Analytics API

Production-grade FastAPI and MongoDB service for tracking employee attendance, enforcing business rules (R1–R10), audit logging regularizations, and generating analytics via MongoDB aggregation pipelines.

## Submission Overview
- **`app/main.py`**: Complete FastAPI service implementation including CRUD, business logic, aggregations, and explain endpoints.
- **`REVIEW.md`**: Detailed defect audit log of starter code fixes.
- **`DECISIONS.md`**: Architectural & MongoDB design decision explanations.
- **`requirements.txt`**: Project dependencies (`fastapi`, `uvicorn`, `pymongo`, `pydantic`, `python-dotenv`).

## Quick Start & Local Setup

```bash
# 1. Environment & Dependencies
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure Database Environment (.env)
# Set MONGO_URI and MONGO_DB environment variables
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

# 3. Seed Sample Data (Optional)
python sample_seed.py

# 4. Start the Application
uvicorn app.main:app --port 8000 --reload
```

## API Documentation & Verification
- Interactive Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- Health Probe: `GET http://localhost:8000/health`
- Admin Query Explain: `GET http://localhost:8000/admin/explain/{endpoint}`
