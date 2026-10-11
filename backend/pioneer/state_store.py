"""Opt-in checkpoints for explicit built-in state, never timers/events/hardware handles."""
import hashlib
import json
import os
import threading
from pathlib import Path

from pioneer.engine.types import check_value


def signature(n):
    return hashlib.sha256(json.dumps([n.type, n.params, n.inputs], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class StateStore:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "state"
        self.dir.mkdir(exist_ok=True)
        self.lock = threading.RLock()

    def load(self, pid, graph, registry):
        try:
            path = self.dir / f"{pid}.json"
            if path.stat().st_size > 128 * 1024:
                return {}
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        out = {}
        if not isinstance(saved, dict):
            return out
        for nid, n in graph.nodes.items():
            record = saved.get(nid)
            if not isinstance(record, dict) or record.get("signature") != signature(n):
                continue
            nt = registry.for_node(n)
            values = record.get("values")
            if not isinstance(values, dict):
                continue
            try:
                out[nid] = {k: check_value(v, nt.outputs[k].type) for k, v in values.items()
                            if k in nt.cls.persistent_outputs and k in nt.outputs}
            except (ValueError, TypeError):
                continue
        return out

    @staticmethod
    def snapshot(rt):
        records = {}
        for nid, node in rt.nodes.items():
            values = {k: rt.sample(nid, k).value for k in node.persistent_outputs if rt.sample(nid, k) is not None}
            if values:
                records[nid] = {"signature": signature(rt.graph.nodes[nid]), "values": values}
        return records

    def save(self, pid, records):
        with self.lock:
            self._save(pid, records)

    def _save(self, pid, records):
        text = json.dumps(records, ensure_ascii=False, allow_nan=False)
        if len(text.encode()) > 128 * 1024:
            raise ValueError("состояние больше 128 КБ")
        path = self.dir / f"{pid}.json"
        if path.exists() and path.read_text() == text:
            return  # no flash writes for unchanged state
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
