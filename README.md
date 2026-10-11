# HR Attendance & Analytics API

## How to Run

1. Install dependencies:
   ```bash
   pip install -r requirements.txt

Set your environment variables (the application expects these to connect to MongoDB):

MONGO_URI (e.g., mongodb://localhost:27017 or an Atlas URI)

MONGO_DB (e.g., attendance_db)
 
 START APP: uvicorn app.main:app --port 8000