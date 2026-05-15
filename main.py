# main.py (root)
# Uvicorn entry point — DO NOT put application logic here.
# All logic lives in app/main.py via create_app() factory.
#
# Run locally:
#   uvicorn main:app --reload --port 8000
#
# Run in production (Dockerfile CMD):
#   uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1

from app.main import create_app

app = create_app()