import json

from fastapi.testclient import TestClient
from app.config import Settings
from app.contracts import validate_definition
from app.main import create_app
from app import main
import pytest


def test_liveness_does_not_probe_database(monkeypatch):
    def unexpected(*args):
        raise AssertionError("Liveness must not access the DB")
    monkeypatch.setattr(main, "probe_database", unexpected)
    with TestClient(create_app()) as client:
        assert client.get("/healthz").json()["status"] == "alive"
        assert client.get("/healthz").json()["live_models_enabled"] is False



def test_health_reports_each_app_configuration_without_backend_access(monkeypatch, tmp_path):
    def unexpected(*args):
        raise AssertionError("Liveness accessed a dependency")

    monkeypatch.setattr(main, 'make_engine', unexpected)
    monkeypatch.setattr(main, 'probe_database', unexpected)
    policy = tmp_path / 'policy.json'
    budget = validate_definition('BudgetPolicy', {
        'policy_version': 'synthetic-health.v1', 'currency': 'USD', 'limit': '1',
        'max_model_calls': 1, 'max_tool_calls': 1, 'max_duration_seconds': 60, 'max_repair_calls': 0,
    })
    policy.write_text(json.dumps({'run': budget, 'project_limit': '1', 'quote_usd': '0.1',
                                  'input_per_million': '1', 'output_per_million': '1',
                                  'pricing_version': 'synthetic-health.v1', 'budget_scope': 'synthetic-health'}))
    disabled = create_app(Settings(executor_mode='disabled', live_models_enabled=False))
    enabled = create_app(Settings(executor_mode='openclaw', live_models_enabled=True, policy_path=str(policy)))
    # Bypass lifespan to isolate the requests from startup reconciliation.
    disabled_client, enabled_client = TestClient(disabled), TestClient(enabled)
    try:
        for client, expected in ((disabled_client, False), (enabled_client, True), (disabled_client, False)):
            response = client.get('/healthz')
            assert response.status_code == 200
            assert response.json()['live_models_enabled'] is expected
    finally:
        disabled_client.close()
        enabled_client.close()


def test_real_unreachable_database_is_sanitized():
    settings = Settings(db_host="127.0.0.1", db_port=1, db_user="private-user", db_password="private-password")
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["reason"] == "database_unavailable"
        for secret in ("private-user", "private-password", "127.0.0.1", "psycopg", "Traceback"):
            assert secret not in response.text
        assert client.get("/healthz").status_code == 200


def test_pending_migrations_are_not_ready(monkeypatch):
    monkeypatch.setattr(main, "probe_database", lambda engine: {
        "database": "available", "migrations": "pending", "reason": "migration_required"})
    with TestClient(create_app()) as client:
        assert client.get("/readyz").status_code == 503


def test_future_operations_remain_unimplemented():
    with TestClient(create_app()) as client:
        assert client.get("/v1/projects/prj_demo/artifact-versions/av_note-v1").status_code == 401
        assert client.post("/v1/projects", json={"title": "Project"}).status_code == 401
        assert client.get("/openapi.json").status_code == 404


def test_live_model_flag_fails_closed():
    with pytest.raises(ValueError, match="unavailable"):
        Settings(live_models_enabled=True)
