"""Opt-in state, portable copies and confirmed boot-file transactions."""
import json
import stat
from pathlib import Path
import pytest
from pioneer.config import Config
from pioneer.engine.runtime import Origin
from pioneer.overlays import OverlayError, OverlayManager
from pioneer.projects import ProjectManager
from pioneer.nodes import registry
from test_api import client, anon  # noqa: F401
from pioneer.api.app import API


def test_checkpoint_restore_and_invalidate(tmp_path, hw):
    pm = ProjectManager(tmp_path, hw, registry())
    pid = pm.create('State').id
    pm.apply_ops(pid, [{'op': 'add_node', 'id': 't', 'type': 'builtin.toggle'}])
    pm.update_meta(pid, save_state=True)
    pm.start(pid)
    def toggle():
        rt = pm.engine.runtimes[pid]; n = rt.nodes['t']
        rt.run_wave(Origin('test'), None, lambda: n.on_event('toggle', None), n)
    pm.engine.call(toggle)
    assert pm.engine.call(lambda: pm.engine.runtimes[pid].sample('t', 'out').value) is True
    pm.stop(pid)
    path = tmp_path / 'state' / f'{pid}.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    pm.start(pid)
    assert pm.engine.call(lambda: pm.engine.runtimes[pid].sample('t', 'out').value) is True
    pm.stop(pid)
    pm.apply_ops(pid, [{'op': 'set_param', 'node': 't', 'name': 'initial', 'value': True}])
    saved = json.loads(path.read_text()); saved['t']['values']['out'] = False
    path.write_text(json.dumps(saved))
    pm.start(pid)
    assert pm.engine.call(lambda: pm.engine.runtimes[pid].sample('t', 'out').value) is True
    pm.update_meta(pid, save_state=False)
    pm.close()
    assert not path.exists()


def test_export_import_stopped_copy(client):
    pid = client.post(f'{API}/projects', json={'name': 'Original'}).json()['id']
    client.put(f'{API}/projects/{pid}', json={'autostart': True, 'save_state': True})
    body = client.get(f'{API}/projects/{pid}/export').json()
    imported = client.post(f'{API}/projects/import', json=body)
    assert imported.status_code == 200
    other = imported.json()
    assert other['id'] != pid and not other['autostart'] and not other['save_state']
    assert client.get(f'{API}/projects/{other["id"]}').json()['status']['state'] == 'stopped'
    assert client.get(f'{API}/projects/{pid}').json()['autostart']
    body['graph']['nodes'] = [{'id': 'a', 'type': 'builtin.toggle'}] * 2
    assert client.post(f'{API}/projects/import', json=body).status_code == 422
    assert client.post(f'{API}/projects/import', content=b'x' * (1024 * 1024 + 1)).status_code == 413


def test_overlay_transaction(tmp_path, board, hw):
    data = tmp_path / 'data'; data.mkdir()
    overlays = tmp_path / 'overlays'; overlays.mkdir()
    path = tmp_path / 'uEnv.txt'
    original = 'bootargs=keep\nuser_overlays=my-overlay\noverlays=' + ' '.join(Config().default_overlays) + '\n'
    path.write_text(original)
    om = OverlayManager(Config(data_dir=data, uenv_file=path, overlays_dir=overlays), board, hw)
    plan = om.preview(['uart4'], [])
    assert path.read_text() == original and plan['missing'] and plan['diff']
    with pytest.raises(OverlayError, match='нет .dtbo'):
        om.apply(['uart4'], [], plan['hash'])
    for overlay in plan['overlays']:
        (overlays / f'{overlay}.dtbo').touch()
    path.write_text(original + '# edited\n')
    with pytest.raises(OverlayError) as stale:
        om.apply(['uart4'], [], plan['hash'])
    assert stale.value.code == 409
    path.write_text(original)
    hw.acquire('busy', 'Busy owner', 'J10-5', 'gpio_out', initial=False)
    with pytest.raises(OverlayError, match='владельцев'):
        om.apply(['uart4'], [], plan['hash'])
    hw.release('busy', 'J10-5')
    result = om.apply(['uart4'], [], plan['hash'])
    backup = Path(result['backup'])
    assert backup.read_text() == original and stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert 'bootargs=keep\nuser_overlays=my-overlay\n' in path.read_text()
    assert 'rk3308-uart4' in path.read_text()
    assert not om.preview(['uart4'], [])['needs_reboot']


def test_boot_api_guards(client):
    assert client.post(f'{API}/board/interfaces/apply', json={'hash': 'x'}).status_code == 422
    assert client.post(f'{API}/board/reboot', json={'confirmation': 'REBOOT'}).status_code == 422
    assert client.put(f'{API}/ai/chats/no-such/artifacts/nope', json={'status': 'accepted'}).status_code == 404
