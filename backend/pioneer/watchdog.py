"""Opt-in Linux watchdog. Only the healthy event loop feeds; normal shutdown disarms."""
import array
import asyncio
import fcntl
import gzip
import os
from pathlib import Path

GETSUPPORT = 0x80285700
SETTIMEOUT = 0xc0045706
MAGICCLOSE = 0x0100


class Watchdog:
    def __init__(self, cfg):
        self.device = Path('/dev/watchdog0')
        self.sysfs = cfg.kernel_sys_dir / 'class/watchdog/watchdog0'
        self.health = None
        self.fd, self.task, self.timeout, self.error = None, None, 60, ''

    def status(self):
        def read(name):
            try:return (self.sysfs / name).read_text().strip()
            except OSError:return None
        nowayout = read('nowayout')
        if nowayout is None:
            try:
                with gzip.open('/proc/config.gz', 'rt') as f: config = f.read(256*1024)
                if '# CONFIG_WATCHDOG_NOWAYOUT is not set' in config and (self.sysfs/'device/driver').resolve().name=='dw_wdt':
                    cmdline=Path('/proc/cmdline').read_text()
                    override=next((v.split('=',1)[1] for v in cmdline.split() if v.startswith('dw_wdt.nowayout=')),None)
                    nowayout='0' if override is None or override in ('0','N','n') else '1'
                elif 'CONFIG_WATCHDOG_NOWAYOUT=y' in config: nowayout = '1'
            except OSError: pass
        return {'available': self.device.exists(), 'armed': self.fd is not None,
                'timeout': self.timeout, 'identity': read('identity'), 'nowayout': nowayout,
                'error': self.error, 'policy': 'event-loop heartbeat; crash/stall → board reset; orderly stop → disarm; arm again after restart'}

    async def arm(self, timeout, confirmed):
        if not confirmed: raise ValueError('подтвердите возможность аппаратной перезагрузки платы')
        if self.fd is not None: raise ValueError('watchdog уже включён')
        if not isinstance(timeout, int) or not 30 <= timeout <= 180:
            raise ValueError('таймаут watchdog 30…180 с')
        # Never experiment with non-disarmable hardware: read before opening activates it.
        if self.status()['nowayout'] != '0':
            raise ValueError('watchdog с nowayout=1/неизвестным nowayout включать нельзя')
        fd = os.open(self.device, os.O_WRONLY | os.O_CLOEXEC)
        try:
            info = bytearray(40);fcntl.ioctl(fd, GETSUPPORT, info, True)
            if not int.from_bytes(info[:4], 'little') & MAGICCLOSE:
                raise ValueError('драйвер не поддерживает безопасное magic close')
            value = array.array('i', [timeout]);fcntl.ioctl(fd, SETTIMEOUT, value, True)
            if value[0] < 30: raise ValueError('реальный таймаут watchdog меньше 30 с')
            self.timeout, self.fd, self.error = value[0], fd, ''
            os.write(fd, b'.')
            self.task = asyncio.create_task(self._feed())
        except BaseException:
            try:
                value = array.array('i', [1])  # WDIOS_DISABLECARD, including failed arm
                try: fcntl.ioctl(fd, 0x80045704, value, True)
                except OSError: os.write(fd, b'V')
            finally:
                os.close(fd);self.fd = None
            raise
        return self.status()

    async def _feed(self):
        try:
            while True:
                await asyncio.sleep(self.timeout / 4)
                if self.health:
                    await asyncio.wait_for(asyncio.to_thread(self.health), 5)
                os.write(self.fd, b'.')
        except asyncio.CancelledError:raise
        except Exception:
            # Keep fd open, do not magic-close on a fault: watchdog must expire.
            self.error = 'не удалось обновить watchdog; возможна аппаратная перезагрузка'

    async def disarm(self):
        if self.task:
            self.task.cancel()
            try:await self.task
            except asyncio.CancelledError:pass
            self.task = None
        if self.fd is not None:
            os.write(self.fd, b'V');os.close(self.fd);self.fd = None
        return self.status()
