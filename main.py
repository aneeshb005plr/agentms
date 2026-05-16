# main.py — uvicorn entry point
# Run: uvicorn main:app --reload --port 8080

from app.main import create_app

app = create_app()