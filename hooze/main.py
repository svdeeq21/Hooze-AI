"""ASGI entrypoint:  uvicorn hooze.main:app --host 0.0.0.0 --port $PORT"""
from hooze.api.app import create_app

app = create_app()
