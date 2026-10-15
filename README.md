# emp_one
### Employee Attendance & Analytics API

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7) or use environment variables set in the bash
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.
