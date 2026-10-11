"""Worker-side CircuitPython API over Pioneer hardware RPC."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

SHIMS = Path(__file__).parent
_context = ContextVar("circuitpython_context", default=None)
_script = None


class Context:
    def __init__(self, call):
        self._call, self.defaults = call, {}

    def call(self, op, **args):
        result = self._call(op, **args)
        if isinstance(result, dict) and "_circuitpython_errno" in result:
            raise OSError(result["_circuitpython_errno"], result["message"])
        return result


def current():
    global _script
    context = _context.get()
    if context is None:
        if _script is None:
            from pioneer.sdk import _rpc

            _script = Context(_rpc.call)
        context = _script
    return context


def rpc(action, **args):
    return current().call("circuitpython", action=action, **args)


@contextmanager
def activate(context):
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


def project_root(root):
    root = Path(root)
    # A block entry may live in a subdirectory of its CodeTree.
    return next((p for p in (root, *root.parents) if (p / "pioneer.json").is_file()), root)


def site(root):
    return project_root(root).parent.parent / "drivers" / "adafruit" / "site"


def paths(root):
    return [str(SHIMS), str(site(root).resolve())]


def check_lock(root):
    import json

    root = project_root(root)
    lock = root / "adafruit.lock.json"
    if not lock.exists():
        return
    installed = site(root) / ".pioneer-adafruit.json"
    versions = json.loads(installed.read_text())["versions"] if installed.exists() else {}
    for package, version in json.loads(lock.read_text()).get("drivers", {}).items():
        if versions.get(package) != version:
            raise RuntimeError(
                f"{package}: lock={version}, установлен={versions.get(package, 'нет')}; обновите подключение через мастер"
            )
