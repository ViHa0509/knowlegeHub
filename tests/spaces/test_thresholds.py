"""/thresholds: per-model code values, stored overrides, env wins (KH 7.1)."""
from __future__ import annotations

from app import config, ctx_config, thresholds
from app.spaces import mapper


def test_code_values_per_model(client, monkeypatch):
    j = client.get("/thresholds", params={"model": "bge-m3"}).json()
    by = {t["name"]: t for t in j["thresholds"]}
    assert j["model"] == "bge-m3" and by["KH_STEP_FLOOR"]["code"] == 0.56 and by["KH_MAP_FLOOR"]["value"] == 0.45
    assert by["KH_TURN_LESSON_FLOOR"]["source"] == "code" and by["KH_TURN_LESSON_FLOOR"]["stored"] is None
    small = {t["name"]: t["value"] for t in client.get("/thresholds").json()["thresholds"]}
    assert small["KH_STEP_FLOOR"] == 0.68 and small["KH_TURN_LESSON_FLOOR"] == 0.6


def test_store_apply_clear(client, monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "bge-m3")
    try:
        assert client.put("/thresholds", json={"name": "KH_NOPE", "value": 0.5}).status_code == 404
        assert client.put("/thresholds", json={"name": "KH_MAP_FLOOR", "value": 2}).status_code == 422
        r = client.put("/thresholds", json={"name": "KH_MAP_FLOOR", "value": 0.4})
        assert r.status_code == 200 and r.json()["stored"] == 0.4
        assert mapper.MAP_FLOOR == 0.4
        thresholds.apply()
        assert ctx_config.STEP_FLOOR == 0.56
        by = {t["name"]: t for t in client.get("/thresholds").json()["thresholds"]}
        assert by["KH_MAP_FLOOR"]["source"] == "stored"
        monkeypatch.setenv("KH_MAP_FLOOR", "0.3")
        assert thresholds.value("KH_MAP_FLOOR") == 0.3
        monkeypatch.delenv("KH_MAP_FLOOR")
        d = client.delete("/thresholds/KH_MAP_FLOOR").json()
        assert d == {"name": "KH_MAP_FLOOR", "cleared": True, "value": 0.45}
        assert client.put("/thresholds", json={"name": "KH_MAP_K", "value": 12}).json()["value"] == 12
        assert mapper.MAP_K == 12
        client.delete("/thresholds/KH_MAP_K")
    finally:
        monkeypatch.setattr(config, "EMBEDDING_MODEL", "bge-small-en-v1.5")
        thresholds.apply()
    assert ctx_config.STEP_FLOOR == 0.68 and mapper.MAP_FLOOR == 0.55
