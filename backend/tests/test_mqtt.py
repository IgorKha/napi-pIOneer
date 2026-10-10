"""MQTT-мост на подставном клиенте paho: топики, команды, ответы, состояния, «Связь»."""
import json
from types import SimpleNamespace

import pytest

from conftest import wait_for
from pioneer.mqtt import MqttBridge, MqttSettings, SettingsStore
from pioneer.nodes import registry
from pioneer.projects import ProjectManager
from pioneer.remote import Remote
from pioneer.secrets_store import SecretStore


class FakeClient:
    def __init__(self, client_id):
        self.client_id, self.pub, self.subs, self.will, self.auth = client_id, [], [], None, None

    def will_set(self, topic, payload, qos, retain):
        self.will = (topic, payload, retain)

    def username_pw_set(self, u, p):
        self.auth = (u, p)

    def reconnect_delay_set(self, a, b): ...
    def tls_set(self): ...
    def connect_async(self, host, port, keepalive): self.addr = (host, port)
    def loop_start(self): ...
    def loop_stop(self): ...
    def disconnect(self): ...

    def subscribe(self, topic, qos):
        self.subs.append(topic)

    def publish(self, topic, payload, qos=0, retain=False, properties=None):
        self.pub.append((topic, payload, retain, properties))

    def last(self, topic):
        for t, p, *_ in reversed(self.pub):
            if t == topic:
                try:
                    return json.loads(p)
                except ValueError:
                    return p
        return None


@pytest.fixture
def setup(tmp_path, hw):
    pm = ProjectManager(tmp_path, hw, registry())
    secrets = SecretStore(tmp_path)
    secrets.update({"mq": "pw"})
    remote = pm.engine.remote = Remote(pm)
    st = SettingsStore(tmp_path)
    st.save_mqtt(MqttSettings(enabled=True, host="broker", username="napi", password="{{secret:mq}}", device_id="dev1"))
    clients = []
    bridge = pm.engine.mqtt = MqttBridge(st, secrets, remote, lambda cid: clients.append(FakeClient(cid)) or clients[-1])
    pm.on_running.append(bridge.publish_describe)
    bridge.start()
    yield pm, bridge, clients[0]
    bridge.stop()
    pm.close()


def test_connect_status_command_state(setup):
    pm, bridge, c = setup
    assert c.will == ("pioneer/dev1/status", "offline", True) and c.auth == ("napi", "pw") and c.addr == ("broker", 1883)
    p = pm.create("Свет")
    pm.apply_ops(p.id, [
        {"op": "add_node", "id": "cmd", "type": "builtin.command", "params": {"name": "light.set", "args_spec": "on: Bool"}},
        {"op": "add_node", "id": "st", "type": "builtin.state", "params": {"name": "light"}},
        {"op": "add_node", "id": "lk", "type": "builtin.mqtt_link"},
        {"op": "add_node", "id": "pub", "type": "builtin.publish", "params": {"name": "light.changed"}},
        {"op": "connect", "from": ["cmd", "flag"], "to": ["st", "value"]},
        {"op": "connect", "from": ["cmd", "flag"], "to": ["pub", "value"]},
    ])
    pm.start(p.id)
    bridge._on_connect(c, None, None, SimpleNamespace(is_failure=False))
    assert c.last("pioneer/dev1/status") == "online" and c.subs == ["pioneer/dev1/cmd/+"]
    assert c.last("pioneer/dev1/describe")["commands"][0]["name"] == "light.set"
    assert wait_for(lambda: pm.engine.runtimes[p.id].sample("lk", "online").value is True, 1)

    msg = SimpleNamespace(topic="pioneer/dev1/cmd/light.set", payload=b'{"id": "7", "args": {"on": true}}',
                          properties=None)
    bridge._on_message(c, None, msg)
    assert wait_for(lambda: c.last("pioneer/dev1/resp/light.set") == {"id": "7", "ok": True}, 2)
    assert c.last("pioneer/dev1/state/light")["value"] is True
    assert c.last("pioneer/dev1/event/light.changed") is True
    retained = {t for t, _, r, _ in c.pub if r}
    assert {"pioneer/dev1/status", "pioneer/dev1/describe", "pioneer/dev1/state/light"} <= retained

    bad = SimpleNamespace(topic="pioneer/dev1/cmd/light.set", payload=b'{"id": "8", "args": {"on": 1}}', properties=None)
    bridge._on_message(c, None, bad)
    assert wait_for(lambda: c.last("pioneer/dev1/resp/light.set") == {"id": "8", "ok": False, "error": "on: ожидается true/false"}, 2)

    bridge._on_disconnect(c, None, None, None)
    rt = pm.engine.runtimes[p.id]
    assert wait_for(lambda: rt.sample("lk", "online").value is False, 1)


def test_settings_validation(tmp_path):
    st = SettingsStore(tmp_path)
    with pytest.raises(ValueError, match="ссылкой на секрет"):
        st.save_mqtt(MqttSettings(password="plain"))
    with pytest.raises(ValueError, match="id платы"):
        st.save_mqtt(MqttSettings(device_id="a/b"))
    st.save_mqtt(MqttSettings(enabled=True))
    assert st.mqtt().device_id  # по умолчанию — имя хоста


def test_paho_vendored():
    import paho.mqtt.client as mqtt
    assert mqtt.MQTTv5 == 5
