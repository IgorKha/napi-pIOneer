"""AI reads installed APIs without imports; drafts become usable visual nodes."""

import asyncio
import json

import pytest
from pioneer.ai import AISettings
from pioneer.ai.tools import Tools
from pioneer.api.app import API
from test_api import client, anon  # noqa: F401
from test_circuitpython import link_drivers
from test_ai import pid, events, setup_ai


def test_driver_context_and_read_only_tools(client, tmp_path):  # noqa: F811
    manager = client.app.state.adafruit
    manager.site.mkdir()
    dist = manager.site / "adafruit_circuitpython_test-1.2.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        "Name: adafruit-circuitpython-test\nVersion: 1.2\nSummary: Test sensor\n"
    )
    (dist / "RECORD").write_text("adafruit_test.py,,\n../../outside.py,,\n")
    marker = tmp_path / "must-not-execute"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\nclass Sensor: pass\n"
    (manager.site / "adafruit_test.py").write_text(source)
    state = manager.site / ".pioneer-adafruit.json"
    state.write_text(
        json.dumps({"requested": {}, "versions": {"adafruit-circuitpython-test": "1.2"}})
    )
    project = pid(client)
    ai = client.app.state.ai
    assert "adafruit-circuitpython-test" in ai.knowledge(client.app)
    assert "setup()" in ai.knowledge(client.app) and "SDK Adafruit/CircuitPython" in ai.knowledge(
        client.app
    )
    for scope in (project, "script:test", "block:test"):
        tools = Tools(client.app, scope, ai.redact, AISettings(search="off"))
        assert "get_adafruit_driver" in {t["name"] for t in tools.schemas()}
        overview = asyncio.run(
            tools.call(
                "get_adafruit_driver", {"package": "adafruit-circuitpython-test", "path": ""}
            )
        )
        assert overview["version"] == "1.2" and "adafruit_test.py" in overview["files"]
        result = asyncio.run(
            tools.call(
                "get_adafruit_driver",
                {"package": "adafruit-circuitpython-test", "path": "adafruit_test.py"},
            )
        )
        assert result["untrusted_source"] == source and not marker.exists()
        for path in ("../../outside.py", "/etc/passwd", "not-owned.py"):
            with pytest.raises(ValueError):
                asyncio.run(
                    tools.call(
                        "get_adafruit_driver",
                        {"package": "adafruit-circuitpython-test", "path": path},
                    )
                )
        with pytest.raises(ValueError, match="не установлен"):
            asyncio.run(
                tools.call(
                    "get_adafruit_driver", {"package": "adafruit-circuitpython-absent", "path": ""}
                )
            )
    assert not client.app.state.hw.res.claims
    state.write_text(
        json.dumps({"requested": {}, "versions": {"adafruit-circuitpython-test": "1.3"}})
    )
    assert "1.3" in ai.knowledge(client.app)  # Each request refreshes the inventory.


class AdafruitProvider:
    def __init__(self, *_):
        self.turn = 0

    async def stream(self, messages, system, tools, budget):
        assert "adafruit-circuitpython-bme280" in system
        assert "get_adafruit_driver" in {t["name"] for t in tools}
        if self.turn == 0:
            call = {
                "name": "get_adafruit_driver",
                "arguments": json.dumps({"package": "adafruit-circuitpython-bme280", "path": ""}),
            }
        elif self.turn == 1:
            overview = json.loads(messages[-1]["content"])
            assert overview["version"] and "block_example" in overview
            call = (
                {
                    "name": "draft_block",
                    "arguments": json.dumps(
                        {
                            "name": "AI BME280",
                            "code": overview["block_example"],
                            "summary": "Драйвер → визуальная нода",
                        }
                    ),
                }
                if self.turn == 1
                else None
            )
        else:
            call = None
        self.turn += 1
        yield {
            "result": [{"role": "assistant", "content": "Черновик"}],
            "calls": [{**call, "id": str(self.turn)}] if call else [],
            "usage": {"input": 1, "output": 1},
        }

    def results(self, calls, outputs):
        # The final round only needs the draft result.
        return [{"role": "user", "content": outputs[0]}]

    async def close(self):
        pass


def test_ai_driver_draft_accept_and_visual_graph(client, tmp_path):  # noqa: F811
    from conftest import wait_for

    link_drivers(tmp_path)
    project = pid(client)
    setup_ai(client)
    client.app.state.ai.provider_factory = AdafruitProvider
    response = client.post(
        API + f"/ai/chats/{project}/messages",
        json={"text": "Собери визуальную ноду из установленного BME280"},
    )
    ev = events(response)
    assert not any(e["type"] == "error" for e in ev), ev
    artifact = next(e["artifact"] for e in ev if e["type"] == "artifact")
    assert artifact["kind"] == "draft" and not client.app.state.blocks.list()
    block = client.post(
        API + "/blocks", json={"name": artifact["name"], "code": artifact["code"]}
    ).json()
    assert block["error"] is None, block
    tools = Tools(client.app, project, lambda v: v, AISettings(search="off"))
    schema = tools._call("get_node_type", {"type": block["types"][0]})
    assert any(s["name"] == "temperature" for s in schema["outputs"])
    ops = [{"op": "add_node", "id": "sensor", "type": block["types"][0], "params": {"period": 0.1}}]
    model = client.app.state.hw.hal.add("bme280")
    proposal = tools._call("propose_ops", {"ops_json": json.dumps(ops), "summary": "BME280"})
    assert proposal["proposed"]
    accepted = client.post(API + f"/projects/{project}/ops", json={"ops": ops, "rev": 0})
    assert accepted.status_code == 200, accepted.text
    pm = client.app.state.projects
    pm.start(project)
    runtime = pm.engine.runtimes[project]
    assert runtime.sample("sensor", "temperature").value == pytest.approx(25, abs=0.01)
    client.app.state.hw.hal.action(model.id, "set", {"temperature": 33})
    assert wait_for(
        lambda: abs((runtime.sample("sensor", "temperature").value or 0) - 33) < 0.01, 3
    )
    pm.stop(project)
    assert not client.app.state.hw.res.claims
