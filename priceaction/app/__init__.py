"""FastAPI application package."""
from .server import app


def create_app():
    return app
