"""Read-only service diagnostics; no secrets, code or environment variables."""
import os
import platform
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pioneer import __version__

router = APIRouter(prefix='/system')


def proc_values(path):
    try:
        values = {}
        for line in path.read_text().splitlines():
            key, sep, raw = line.partition(':')
            parts = raw.split()
            if sep and parts and parts[0].isdigit():
                values[key] = int(parts[0]) * 1024
        return values
    except OSError:
        return {}


def snapshot(app):
    s = app.state
    memory = proc_values(Path('/proc/meminfo'))
    process = proc_values(Path('/proc/self/status'))
    v = os.statvfs(s.cfg.data_dir)
    projects, scripts = s.projects.list(), s.scripts.list()
    warnings = []
    rss, available = process.get('VmRSS'), memory.get('MemAvailable')
    if rss is not None and rss > 120 * 1024**2: warnings.append('Память core превысила бюджет 120 МиБ')
    if available is not None and available < 64 * 1024**2: warnings.append('На плате осталось меньше 64 МиБ свободной памяти')
    if v.f_bavail * v.f_frsize < 64 * 1024**2: warnings.append('Для данных осталось меньше 64 МиБ на диске')
    try:
        drivers = [{'id': p['id'], 'loaded': p['loaded'], 'autoload': p['autoload']}
                   for p in s.kernel_packages.catalog()['packages']]
    except (OSError, ValueError):
        drivers = []
        warnings.append('Не удалось прочитать каталог драйверов')
    return {'version': __version__, 'hal': s.hal.name, 'kernel': platform.release(),
            'uptime_seconds': round(time.monotonic() - s.started),
            'memory': {'core_rss_bytes': rss, 'available_bytes': available, 'total_bytes': memory.get('MemTotal')},
            'disk': {'free_bytes': v.f_bavail * v.f_frsize, 'total_bytes': v.f_blocks * v.f_frsize},
            'projects': {'total': len(projects), 'running': sum(p['status']['state'] == 'running' for p in projects)},
            'scripts': {'total': len(scripts), 'running': sum(x['status']['state'] == 'running' for x in scripts)},
            'claims': len(s.hw.res.claims), 'mqtt_connected': s.mqtt.connected,
            'ai_busy': s.ai.lock.locked(), 'drivers': drivers, 'warnings': warnings}


@router.get('')
def status(request: Request):
    return JSONResponse(snapshot(request.app), headers={'Cache-Control': 'no-store'})


@router.get('/report')
def report(request: Request):
    return JSONResponse(snapshot(request.app), headers={'Cache-Control': 'no-store',
                        'Content-Disposition': 'attachment; filename="pioneer-diagnostics.json"'})
