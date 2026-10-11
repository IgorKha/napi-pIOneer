"""Assistant tools cannot execute code or mutate projects. Providers stream normalized turns."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from pioneer.ai import AISettings
from pioneer.ai.providers import OpenAIProvider, AnthropicProvider
from pioneer.ai.tools import Tools, public_get
from pioneer.api.app import API
from test_api import anon, client  # noqa: F401


OPS = [{"op": "add_node", "id": "button", "type": "builtin.ui_button@1", "position": [0, 0]},
       {"op": "add_node", "id": "counter", "type": "builtin.counter@1", "position": [240, 0]},
       {"op": "connect", "from": ["button", "click"], "to": ["counter", "up"]}]


def pid(client):
    return client.post(API + "/projects", json={"name": "AI"}).json()["id"]


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


class FakeProvider:
    instances = []

    def __init__(self, settings, key):
        self.turn = 0
        self.closed = False
        self.instances.append(self)

    async def stream(self, messages, system, tools, budget):
        assert "live-secret-value" not in system + json.dumps(messages)
        assert "propose_ops" in {t["name"] for t in tools}
        self.turn += 1
        calls = ([{"id": "call-1", "name": "propose_ops", "arguments": json.dumps({"ops_json": json.dumps(OPS), "summary": "Кнопка и счётчик"})}]
                 if self.turn == 1 else [])
        yield {"delta": "Проверяю. " if calls else "Предложение готово."}
        yield {"result": [{"role": "assistant", "content": "ответ"}], "calls": calls, "usage": {"input": 100, "output": 20}}

    def results(self, calls, outputs):
        return [{"role": "user", "content": outputs[0]}]

    async def close(self):
        self.closed = True


def setup_ai(client):
    client.app.state.ai.provider_factory = FakeProvider
    return client.put(API + "/ai/settings", json={"provider": "openai", "model": "gpt-5.4-mini", "search": "off", "api_key": "live-secret-value"})


def test_ai_settings_never_echo_key(client, anon):
    r = setup_ai(client)
    assert r.status_code == 200 and r.json()["configured"]
    assert "live-secret-value" not in r.text
    assert "live-secret-value" not in client.get(API + "/ai/settings").text
    secret = client.app.state.secrets.file
    assert secret.stat().st_mode & 0o777 == 0o600
    assert "api_key" not in json.loads(client.app.state.ai.file.read_text())
    r = client.put(API + "/ai/settings", json={"provider": "wrong", "api_key": "live-secret-value"})
    assert r.status_code == 422 and "live-secret-value" not in r.text
    client.post(API + "/auth/logout")
    assert client.get(API + "/ai/settings").status_code == 401


def test_stream_proposal_history_accept_undo_stale(client):
    project = pid(client)
    setup_ai(client)
    before = client.get(API + f"/projects/{project}").json()
    r = client.post(API + f"/ai/chats/{project}/messages", json={"text": "Собери кнопку со счётчиком; live-secret-value"})
    ev = events(r)
    assert [e["text"] for e in ev if e["type"] == "delta"] == ["Проверяю. ", "Предложение готово."]
    artifact = next(e["artifact"] for e in ev if e["type"] == "artifact")
    assert artifact["validation"]["can_start"] and artifact["rev"] == 0
    assert client.get(API + f"/projects/{project}").json()["graph"] == before["graph"]
    assert client.app.state.hw.res.claims == {} and FakeProvider.instances[-1].closed
    hist = client.get(API + f"/ai/chats/{project}")
    assert "live-secret-value" not in hist.text
    assert hist.json()["messages"][-1]["usage"]["output"] == 40
    accepted = client.post(API + f"/projects/{project}/ops", json={"ops": artifact["ops"], "rev": artifact["rev"]})
    assert accepted.status_code == 200
    stale = client.post(API + f"/projects/{project}/ops", json={"ops": artifact["ops"], "rev": artifact["rev"]})
    assert stale.status_code == 409
    assert client.post(API + f"/projects/{project}/ops", json={"ops": accepted.json()["inverse"], "rev": 1}).status_code == 200
    assert client.get(API + f"/projects/{project}").json()["graph"]["nodes"] == []
    assert client.put(API + f"/ai/chats/{project}/artifacts/{artifact['id']}", json={"status": "accepted"}).status_code == 200
    assert client.get(API + f"/ai/chats/{project}").json()["messages"][-1]["artifacts"][0]["status"] == "accepted"
    assert client.delete(API + f"/ai/chats/{project}").status_code == 200
    assert client.get(API + f"/ai/chats/{project}").json()["messages"] == []


def test_tools_no_execution_mutation_or_secrets(client, tmp_path):
    project = pid(client)
    setup_ai(client)
    ai = client.app.state.ai
    tools = Tools(client.app, project, ai.redact, AISettings(search="off"))
    async def check():
        for name in ("read_secrets", "start_project", "manual_pin", "shell", "save_block"):
            with pytest.raises(ValueError):
                await tools.call(name, {})
        marker = tmp_path / "should-not-exist"
        code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        result = await tools.call("draft_block", {"name": "test", "summary": "draft", "code": code})
        assert result["executed"] is False and not marker.exists()
        for ops in ([{"op": "add_node", "id": "script", "type": "builtin.script@1", "params": {"code": code}}],
                    [{"op": "set_param", "node": "a", "name": "code", "value": code}]):
            with pytest.raises(ValueError):
                await tools.call("validate_ops", {"ops_json": json.dumps(ops)})
        assert not marker.exists()
        board = await tools.call("get_board", {})
        assert board["hal"] == "bench" and board["adc"]["vref"] == 1.8
        assert "live-secret-value" not in json.dumps(await tools.call("get_project", {}))
        schema = await tools.call("get_node_type", {"type": "builtin.pulse@1"})
        assert schema["doc"] and schema["inputs"]
        with pytest.raises(ValueError):
            await tools.call("get_project", {"pid": "another"})
    asyncio.run(check())
    assert client.app.state.projects.get(project)["rev"] == 0
    assert client.app.state.blocks.list() == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1", "http://192.168.1.33", "http://169.254.169.254", "http://[::1]", "http://user:pass@example.com", "https://example.com:8100"])
def test_web_fetch_blocks_private_files_credentials(url):
    with pytest.raises(ValueError):
        public_get(url)


def test_tools_reject_invalid_ops(client):
    project = pid(client)
    tools = Tools(client.app, project, lambda x: x, AISettings(search="off"))
    async def check():
        result = await tools.call("propose_ops", {"summary": "bad", "ops_json": json.dumps([
            {"op": "add_node", "id": "bad", "type": "builtin.digital_out@1", "params": {"pin": "J9-3"}}])})
        assert result["proposed"] is False and result["validation"]["warnings"] and not tools.events
    asyncio.run(check())


def test_missing_key_busy_and_csrf(client):
    project = pid(client)
    assert client.post(API + f"/ai/chats/{project}/messages", json={"text": "Привет"}).status_code == 422
    setup_ai(client)
    client.app.state.ai.active = project
    assert client.delete(API + f"/ai/chats/{project}").status_code == 409
    client.app.state.ai.active = None
    assert client.post(API + "/ai/chats/../../etc/messages", json={"text": "x"}).status_code != 200
    client.headers.pop("X-Pioneer")
    assert client.put(API + "/ai/settings", json={}).status_code == 403


def test_provider_error_no_credentials_and_lock_released(client):
    project = pid(client)
    setup_ai(client)
    class Broken(FakeProvider):
        async def stream(self, *args):
            if False:
                yield
            raise RuntimeError("Authorization: Bearer live-secret-value")
    client.app.state.ai.provider_factory = Broken
    response = client.post(API + f"/ai/chats/{project}/messages", json={"text": "hello"})
    assert "live-secret-value" not in response.text and any(e["type"] == "error" for e in events(response))
    assert not client.app.state.ai.lock.locked() and Broken.instances[-1].closed


class Stream:
    def __init__(self, events):
        self.events = events
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def __aiter__(self):
        async def iterate():
            for e in self.events: yield e
        return iterate()


def test_openai_responses_stream_retains_reasoning_and_usage(monkeypatch):
    import openai
    seen = {}
    output = NS(type="function_call", call_id="call", name="get_board", arguments="{}",
                model_dump=lambda **kw: {"type": "function_call", "call_id": "call", "name": "get_board", "arguments": "{}"})
    reason = NS(type="reasoning", model_dump=lambda **kw: {"type": "reasoning", "encrypted_content": "opaque"})
    completed = NS(output=[reason, output], status="completed", usage=NS(input_tokens=5, output_tokens=8, input_tokens_details=NS(cached_tokens=2)))
    async def create(**kw):
        seen.update(kw)
        return Stream([NS(type="response.output_text.delta", delta="Hi"), NS(type="response.completed", response=completed)])
    async def close(): pass
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: NS(responses=NS(create=create), close=close))
    async def check():
        p = OpenAIProvider(AISettings(provider="openai", model="gpt-5.4-mini", effort="low"), "key")
        parts = [i async for i in p.stream([], "system", [], 900)]
        assert parts[0]["delta"] == "Hi"
        assert parts[-1]["result"][0]["encrypted_content"] == "opaque"
        assert parts[-1]["usage"] == {"input": 5, "output": 8, "cached": 2}
        assert p.results(parts[-1]["calls"], ["{}"]) == [{"type": "function_call_output", "call_id": "call", "output": "{}"}]
        assert seen["store"] is False and seen["max_output_tokens"] == 900
        assert seen["tools"][-1]["type"] == "web_search"
    asyncio.run(check())


def test_anthropic_stream_cache_tools_and_continuation(monkeypatch):
    import anthropic
    seen = {}
    msg = NS(content=[NS(type="tool_use", id="a", name="get_board", input={}, model_dump=lambda **kw: {"type": "tool_use", "id": "a", "name": "get_board", "input": {}})],
             usage=NS(input_tokens=10, output_tokens=20, cache_read_input_tokens=3, cache_creation_input_tokens=4), stop_reason="tool_use")
    class ClaudeStream(Stream):
        async def get_final_message(self): return msg
    def stream(**kw):
        seen.update(kw)
        return ClaudeStream([NS(type="content_block_delta", delta=NS(type="text_delta", text="Hi"))])
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: NS(messages=NS(stream=stream)))
    async def check():
        p = AnthropicProvider(AISettings(effort="medium"), "key")
        parts = [i async for i in p.stream([], "system", [], 900)]
        assert parts[0]["delta"] == "Hi" and parts[-1]["usage"]["cache_write"] == 4
        assert seen["system"][0]["cache_control"]["type"] == "ephemeral"
        assert seen["thinking"]["type"] == "adaptive"
        assert p.results(parts[-1]["calls"], ["{}"]) == [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "{}"}]}]
    asyncio.run(check())


def test_draft_rejects_socket_type_used_as_parameter(client):
    s = AISettings()
    tools = Tools(client.app, pid(client), lambda x: x, s)
    with pytest.raises(ValueError, match='Param принимает'):
        tools._call('draft_block', {'name': 'Wrong', 'summary': 'test', 'code': 'from pioneer.block import Param, Number\nfactor = Param(Number, 2)'})
    assert not tools.events


def test_inference_slot_held_until_provider_closed(client):
    setup_ai(client)
    ai = client.app.state.ai
    class Closing(FakeProvider):
        async def close(self):
            assert ai.lock.locked() and ai.active is not None
            await asyncio.sleep(.01)
            await super().close()
    ai.provider_factory = Closing
    response = client.post(API + f'/ai/chats/{pid(client)}/messages', json={'text': 'test'})
    assert any(e['type'] == 'done' for e in events(response))
    assert not ai.lock.locked() and ai.active is None and Closing.instances[-1].closed


def test_custom_stream_fragmented_calls_and_truncation(monkeypatch):
    import openai
    seen = {}
    def chunk(calls=None, text=None, finish=None, usage=None):
        return NS(usage=usage, choices=[NS(finish_reason=finish, delta=NS(content=text, tool_calls=calls))])
    fragments = [chunk([NS(index=0, id='call', function=None)]),
                 chunk([NS(index=0, id=None, function=NS(name='get_board', arguments='{'))]),
                 chunk([NS(index=0, id=None, function=NS(name=None, arguments='}'))], finish='tool_calls'),
                 NS(choices=[], usage=NS(prompt_tokens=12, completion_tokens=7))]
    async def create(**kw): seen.update(kw); return Stream(fragments)
    async def close(): pass
    monkeypatch.setattr(openai, 'AsyncOpenAI', lambda **kw: NS(chat=NS(completions=NS(create=create)), close=close))
    async def check():
        p = OpenAIProvider(AISettings(provider='custom', model='custom-model', search='off'), 'key')
        result = [part async for part in p.stream([], 'system', [], 900)][-1]
        assert result['calls'] == [{'id': 'call', 'name': 'get_board', 'arguments': '{}'}]
        assert result['usage']['input'] == 12 and not result['incomplete']
        assert p.results(result['calls'], ['{}']) == [{'role': 'tool', 'tool_call_id': 'call', 'content': '{}'}]
        assert seen['messages'][0] == {'role': 'system', 'content': 'system'}
        fragments[:] = [chunk(text='partial', finish='length')]
        assert [part async for part in p.stream([], 'system', [], 512)][-1]['incomplete']
    asyncio.run(check())


class ScriptProvider(FakeProvider):
    async def stream(self, messages, system, tools, budget):
        names = {t["name"] for t in tools}
        assert {"get_script", "read_file", "draft_file", "get_logs"} <= names
        assert not {"get_project", "propose_ops", "draft_block"} & names
        assert "Python-скриптом" in system
        self.turn += 1
        calls = [{"id": "file-1", "name": "draft_file", "arguments": json.dumps({
            "path": "main.py", "code": "raise RuntimeError('never execute')\n", "summary": "Тест файла"
        })}] if self.turn == 1 else []
        yield {"result": [{"role": "assistant", "content": "готово"}], "calls": calls, "usage": {"input": 10, "output": 10}}


def script_proposal(client, code="print('before')\n"):
    setup_ai(client)
    client.app.state.ai.provider_factory = ScriptProvider
    sid = client.post(API + "/scripts", json={"name": "AI script", "code": code}).json()["id"]
    scope = "script:" + sid
    r = client.post(API + f"/ai/chats/{scope}/messages", json={"text": "Предложи изменение файла"})
    assert r.status_code == 200
    artifact = next(e["artifact"] for e in events(r) if e["type"] == "artifact")
    return sid, scope, artifact


def test_script_chat_scoped_readonly_and_explicit_save(client, monkeypatch):
    sid, scope, a = script_proposal(client)
    sm = client.app.state.scripts
    assert sm.code(sid) == "print('before')\n"
    assert sm.status(sid).state == "stopped"
    assert a["original"] == sm.code(sid) and a["kind"] == "file"
    monkeypatch.setattr(sm, "saved", lambda *args: pytest.fail("AI must not invoke auto-restart"))
    r = client.put(API + f"/ai/chats/{scope}/artifacts/{a['id']}/file", json={"code": a["code"]})
    assert r.status_code == 200 and not r.json()["executed"]
    assert sm.code(sid) == a["code"] and sm.status(sid).state == "stopped"
    assert client.get(API + f"/ai/chats/{scope}").json()["messages"][-1]["artifacts"][0]["status"] == "accepted"
    assert client.put(API + f"/ai/chats/{scope}/artifacts/{a['id']}/file", json={"code": "print(2)"}).status_code == 409
    # Same hex id in graph and script has independent history and validated scope.
    graph = pid(client)
    assert client.app.state.ai.path(client.app, scope).name.startswith("script-")
    assert client.get(API + f"/ai/chats/{graph}").json()["messages"] == []
    assert client.get(API + "/ai/chats/script:invalid").status_code == 404
    assert not client.app.state.ai.lock.locked()


def test_script_file_stale_and_syntax_rejected(client):
    sid, scope, a = script_proposal(client)
    endpoint = API + f"/ai/chats/{scope}/artifacts/{a['id']}/file"
    assert client.put(endpoint, json={"code": "def :"}).status_code == 422
    client.put(API + f"/code/scripts/{sid}/file", json={"path": "main.py", "text": "print('manual')"})
    assert client.put(endpoint, json={"code": a["code"]}).status_code == 409
    assert client.app.state.scripts.code(sid) == "print('manual')"


def test_script_tools_safe_paths_new_files_redaction_and_limits(client):
    sid, scope, a = script_proposal(client)
    tools = Tools(client.app, scope, client.app.state.ai.redact, AISettings())
    assert tools._call("get_script", {})["id"] == sid
    assert tools._call("read_file", {"path": "main.py"})["untrusted_text"].startswith("print")
    from pioneer.codetree import TreeError
    for path in ("../outside.py", "/tmp/outside.py", "pioneer.json", "nested/../outside.py"):
        with pytest.raises(TreeError):
            tools._call("draft_file", {"path": path, "code": "print(1)", "summary": "bad"})
    for path, code in (("helper.txt", "print(1)"), ("helper.py", "x='" + "a" * 64000 + "'")):
        with pytest.raises(ValueError):
            tools._call("draft_file", {"path": path, "code": code, "summary": "bad"})
    tools._call("draft_file", {"path": "drivers/helper.py", "code": "TOKEN = 'live-secret-value'", "summary": "new"})
    draft = tools.events[-1]
    assert draft["base_hash"] is None and draft["original"] == ""
    assert "live-secret-value" not in json.dumps(draft)
    assert not client.app.state.scripts.tree(sid).path("drivers/helper.py").exists()
    with pytest.raises(ValueError):
        asyncio.run(tools.call("propose_ops", {"ops_json": "[]", "summary": "no"}))


class BlockProvider(FakeProvider):
    async def stream(self, messages, system, tools, budget):
        names = {t['name'] for t in tools}
        assert {'get_block', 'read_file', 'draft_file', 'get_node_type'} <= names
        assert not {'get_script', 'get_project', 'propose_ops', 'draft_block', 'get_logs'} & names
        assert 'кастомным Python-блоком' in system
        self.turn += 1
        calls = [{'id': 'context', 'name': 'get_block', 'arguments': '{}'},
                 {'id': 'file', 'name': 'read_file', 'arguments': '{"path":"block.py"}'},
                 {'id': 'draft', 'name': 'draft_file', 'arguments': json.dumps({
                     'path': 'block.py', 'code': '# AI change\n' + self.code, 'summary': 'Обновить блок'
                 })}] if self.turn == 1 else []
        yield {'result': [{'role': 'assistant', 'content': 'готово'}], 'calls': calls, 'usage': {'input': 10, 'output': 10}}


def block_proposal(client):
    setup_ai(client)
    b = client.post(API + '/blocks', json={'name': 'AI custom block'}).json()
    BlockProvider.code = client.app.state.blocks.get(b['id'])['code']
    client.app.state.ai.provider_factory = BlockProvider
    scope = 'block:' + b['id']
    r = client.post(API + f'/ai/chats/{scope}/messages', json={'text': 'Обнови текущий блок'})
    assert r.status_code == 200
    a = next(e['artifact'] for e in events(r) if e['type'] == 'artifact')
    return b['id'], scope, a


def test_block_chat_readonly_and_explicit_save_updates_schema(client):
    bid, scope, a = block_proposal(client)
    bm = client.app.state.blocks
    before = bm.get(bid)
    assert before['code'] == a['original'] and a['kind'] == 'file'
    assert a['code'] != before['code']
    assert client.app.state.ai.path(client.app, scope).name.startswith('block-')
    assert client.get(API + '/ai/chats/block:invalid').status_code == 404
    r = client.put(API + f'/ai/chats/{scope}/artifacts/{a["id"]}/file', json={'code': a['code']})
    assert r.status_code == 200 and not r.json()['executed']
    after = bm.get(bid)
    assert after['code'] == a['code'] and after['types'] == before['types'] and not after['error']
    assert client.get(API + f'/ai/chats/{scope}').json()['messages'][-1]['artifacts'][0]['status'] == 'accepted'
    assert client.put(API + f'/ai/chats/{scope}/artifacts/{a["id"]}/file', json={'code': a['code']}).status_code == 409


def test_block_ai_stale_scope_and_safe_paths(client):
    bid, scope, a = block_proposal(client)
    endpoint = API + f'/ai/chats/{scope}/artifacts/{a["id"]}/file'
    assert client.put(endpoint, json={'code': 'def :'}).status_code == 422
    other = client.post(API + '/blocks', json={'name': 'Other', 'code': '# empty'}).json()['id']
    assert client.put(API + f'/ai/chats/block:{other}/artifacts/{a["id"]}/file', json={'code': a['code']}).status_code == 404
    client.put(API + f'/code/blocks/{bid}/file', json={'path': 'block.py', 'text': '# manual\n' + a['original']})
    assert client.put(endpoint, json={'code': a['code']}).status_code == 409
    tools = Tools(client.app, scope, client.app.state.ai.redact, AISettings())
    from pioneer.codetree import TreeError
    with pytest.raises(TreeError):
        tools._call('read_file', {'path': '../outside.py'})
    with pytest.raises(ValueError):
        asyncio.run(tools.call('get_script', {}))
    tools._call('draft_file', {'path': 'helpers/new.py', 'code': 'x=1', 'summary': 'helper'})
    assert not tools.code_tree().path('helpers/new.py').exists()
