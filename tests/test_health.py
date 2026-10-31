from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from ask_ymca.db import get_db
from ask_ymca.main import app


class HealthySession:
    def execute(self, statement):
        return 1


class UnavailableSession:
    def execute(self, statement):
        raise OperationalError("SELECT 1", {}, RuntimeError("database down"))


def test_health_reports_database_readiness():
    app.dependency_overrides[get_db] = lambda: HealthySession()
    try:
        response = TestClient(app).get("/api/v1/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_reports_database_failure_as_unavailable():
    app.dependency_overrides[get_db] = lambda: UnavailableSession()
    try:
        response = TestClient(app).get("/api/v1/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "status": "unavailable",
        "database": "unavailable",
    }
