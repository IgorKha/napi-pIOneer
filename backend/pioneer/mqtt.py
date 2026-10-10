"""MQTT-транспорт удалённых команд и телеметрии (idea 003 п.3–4), клиент paho-mqtt (vendor).

Топики (base = "pioneer", dev = id платы):
- <base>/<dev>/status — "online" / "offline" (retained, LWT);
- <base>/<dev>/cmd/<команда> — запрос {"id", "args", "ttl_ms"}; ответ — в MQTT 5 response_topic
  (с correlation_data), иначе в <base>/<dev>/resp/<команда>; тело {"id", "ok", "result"|"error"};
- <base>/<dev>/state/<имя> — состояния (retained), <base>/<dev>/event/<имя> — нода «Публикация»;
- <base>/<dev>/describe — команды и состояния запущенных проектов (retained).

Команды выполняются тем же путём, что HTTP (pioneer.remote): только объявленные, с проверкой
аргументов, ответ — нодой «Ответ» или подтверждение по завершении волны.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
TOPIC_PART = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass
class MqttSettings:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 1883
    username: str = ""
    password: str = ""  # ссылка {{secret:…}}
    tls: bool = False
    device_id: str = ""
    base: str = "pioneer"

    def validate(self) -> None:
        if not self.host.strip():
            raise ValueError("адрес брокера пуст")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("порт 1…65535")
        for name, v in (("id платы", self.device_id), ("префикс", self.base)):
            if not TOPIC_PART.match(v):
                raise ValueError(f"{name}: латиница, цифры, . _ - (без / # +)")
        if self.password and "{{secret:" not in self.password:
            raise ValueError("пароль задайте ссылкой на секрет: {{secret:имя}}")


def default_device_id() -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", socket.gethostname())[:64] or "napi"


class SettingsStore:
    """data/settings.json — настройки сервиса, меняемые из UI (раздел mqtt)."""

    def __init__(self, data_dir: Path):
        self.file = data_dir / "settings.json"

    def _load(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def mqtt(self) -> MqttSettings:
        raw = self._load().get("mqtt", {})
        s = MqttSettings(**{k: v for k, v in raw.items() if k in MqttSettings.__dataclass_fields__})
        s.device_id = s.device_id or default_device_id()
        return s

    def save_mqtt(self, s: MqttSettings) -> None:
        s.device_id = s.device_id or default_device_id()
        s.validate()
        data = self._load()
        data["mqtt"] = asdict(s)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.replace(self.file)


class MqttBridge:
    def __init__(self, settings: SettingsStore, secrets, remote, client_factory=None):
        self.settings, self.secrets, self.remote = settings, secrets, remote
        self._factory = client_factory or _paho_client
        self.client = None
        self.cfg: MqttSettings | None = None
        self.connected = False
        self.error: str | None = None
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(4, thread_name_prefix="mqtt-cmd")
        self.links: list = []  # ноды «Связь»: (runtime, node)
        remote.state_listeners.append(self._on_state)

    # ---- жизненный цикл
    def start(self) -> None:
        self.apply(self.settings.mqtt())

    def apply(self, cfg: MqttSettings) -> None:
        with self._lock:
            self.stop()
            self.cfg, self.error = cfg, None
            if not cfg.enabled:
                return
            try:
                password = self.secrets.resolve(cfg.password) if cfg.password else None
            except Exception as e:
                self.error = str(e)
                return
            c = self._factory(f"pioneer-{cfg.device_id}")
            c.on_connect, c.on_disconnect, c.on_message = self._on_connect, self._on_disconnect, self._on_message
            c.will_set(self.topic("status"), "offline", qos=1, retain=True)
            if cfg.username:
                c.username_pw_set(cfg.username, password)
            if cfg.tls:
                c.tls_set()
            c.reconnect_delay_set(1, 30)
            try:
                c.connect_async(cfg.host, int(cfg.port), keepalive=30)
                c.loop_start()
            except Exception as e:  # адрес не резолвится и т.п.
                self.error = str(e)
                return
            self.client = c

    def stop(self) -> None:
        with self._lock:
            c, self.client = self.client, None
            if c is not None:
                try:
                    c.publish(self.topic("status"), "offline", qos=1, retain=True)
                    c.disconnect()
                    c.loop_stop()
                except Exception:
                    pass
            self._set_connected(False)

    def status(self) -> dict:
        cfg = self.cfg or self.settings.mqtt()
        return {"enabled": cfg.enabled, "connected": self.connected, "error": self.error,
                "broker": f"{cfg.host}:{cfg.port}", "prefix": f"{cfg.base}/{cfg.device_id}"}

    def topic(self, *parts: str) -> str:
        return "/".join([self.cfg.base, self.cfg.device_id, *parts])

    # ---- колбэки paho (поток клиента)
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            self.error = f"брокер отказал: {reason_code}"
            return
        self.error = None
        self._set_connected(True)
        client.publish(self.topic("status"), "online", qos=1, retain=True)
        client.subscribe(self.topic("cmd", "+"), qos=1)
        self.publish_describe()
        for st in self.remote.describe()["states"]:
            self._on_state(st["name"], {k: v for k, v in st.items() if k not in ("name", "project")})

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._set_connected(False)

    def _on_message(self, client, userdata, msg):
        name = msg.topic.rsplit("/", 1)[-1]
        self._pool.submit(self._command, msg, name)

    def _command(self, msg, name: str) -> None:
        try:
            body = json.loads(msg.payload.decode("utf-8")) if msg.payload else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        resp, _ = self.remote.execute_command(name, body, "mqtt", "mqtt")
        props = getattr(msg, "properties", None)
        topic = getattr(props, "ResponseTopic", None) or self.topic("resp", name)
        out_props = None
        corr = getattr(props, "CorrelationData", None)
        if corr is not None:
            out_props = _publish_props(corr)
        c = self.client
        if c is not None:
            c.publish(topic, json.dumps(resp, ensure_ascii=False, default=str), qos=1, properties=out_props)

    # ---- публикация
    def publish_describe(self) -> None:
        c = self.client
        if c is not None and self.cfg:
            c.publish(self.topic("describe"), json.dumps(self.remote.describe(), ensure_ascii=False, default=str),
                      qos=1, retain=True)

    def _on_state(self, name: str, current: dict) -> None:
        c = self.client
        if c is not None and self.connected:
            c.publish(self.topic("state", name), json.dumps(current, ensure_ascii=False, default=str), qos=1, retain=True)

    def publish_event(self, name: str, payload: Any, retain: bool = False) -> None:
        from pioneer.engine.node import NodeError
        if not TOPIC_PART.match(name or ""):
            raise NodeError("имя события: латиница, цифры, . _ -")
        c = self.client
        if c is None or not self.cfg:
            raise NodeError("MQTT не настроен — страница «Настройки»")
        c.publish(self.topic("event", name), json.dumps(payload, ensure_ascii=False, default=str), qos=1, retain=retain)

    # ---- ноды «Связь»
    def _set_connected(self, on: bool) -> None:
        if on == self.connected:
            return
        self.connected = on
        from pioneer.engine.types import Origin
        for rt, node in list(self.links):
            if rt.running:
                rt.engine.inject_call(rt.project_id, Origin("mqtt", "link"), lambda n=node: n.link_changed(on), node=node)


def _publish_props(corr: bytes):
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties
    p = Properties(PacketTypes.PUBLISH)
    p.CorrelationData = corr
    return p


def _paho_client(client_id: str):
    import paho.mqtt.client as mqtt
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv5)
