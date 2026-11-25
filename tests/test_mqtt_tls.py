"""배포 브로커(TLS 8883, 자체 서명 CA) 접속 설정."""

from __future__ import annotations

import pytest

from edge_agent import mqtt_client
from edge_agent.config import MqttConfig, load_config

DEPLOY = """
[mqtt]
host = "203.0.113.10"
port = 8883
username = "room-001"
password = "secret"
tls = true
ca_file = "{ca}"
"""


def write_config(tmp_path, ca: str):
    cfg = tmp_path / "config" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text(DEPLOY.format(ca=ca), encoding="utf-8")
    return cfg


def test_relative_ca_file_is_resolved_next_to_config(tmp_path):
    cfg = write_config(tmp_path, "ca.crt")
    (cfg.parent / "ca.crt").write_text("dummy", encoding="utf-8")

    loaded = load_config(cfg)

    assert loaded.mqtt.ca_file == str((cfg.parent / "ca.crt").resolve())
    assert (loaded.mqtt.port, loaded.mqtt.username, loaded.mqtt.tls) == (8883, "room-001", True)


def test_missing_ca_file_fails_at_startup(tmp_path):
    cfg = write_config(tmp_path, "nope.crt")

    with pytest.raises(FileNotFoundError, match="nope.crt"):
        load_config(cfg)


def test_ca_file_without_tls_is_rejected(tmp_path):
    cfg = tmp_path / "config.toml"
    (tmp_path / "ca.crt").write_text("dummy", encoding="utf-8")
    cfg.write_text('[mqtt]\ntls = false\nca_file = "ca.crt"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="tls"):
        load_config(cfg)


class RecordingClient:
    def __init__(self, *args, **kwargs) -> None:
        self.calls: dict[str, tuple] = {}

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls[name] = (args, kwargs)

        return record


@pytest.mark.parametrize("ca_file", ["/home/pi/edge_agent/config/ca.crt", None])
def test_tls_uses_ca_file_and_login(monkeypatch, ca_file):
    monkeypatch.setattr(mqtt_client.mqtt, "Client", RecordingClient)
    cfg = MqttConfig(host="203.0.113.10", port=8883, username="room-001", password="pw", tls=True, ca_file=ca_file)

    client = mqtt_client.EdgeMqttClient(cfg, "room-001", lambda: None, lambda: None, lambda m: None)

    calls = client._client.calls
    assert calls["tls_set"][1]["ca_certs"] == ca_file  # None 이면 시스템 기본 CA
    assert calls["username_pw_set"][0] == ("room-001", "pw")
