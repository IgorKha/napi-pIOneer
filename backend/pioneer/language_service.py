"""One warm Jedi worker, bounded requests and idle release for the 512 MB board."""
import asyncio
import json
import os
import sys
from pioneer.config import REPO_ROOT


class LanguageService:
    def __init__(self, lock):
        self.lock, self.process, self.idle = lock, None, None
        self.idle_seconds = 20

    async def close(self):
        if self.idle and self.idle is not asyncio.current_task():
            self.idle.cancel()
        self.idle = None
        process, self.process = self.process, None
        if process and process.returncode is None:
            process.kill()
            await process.wait()

    async def _release_idle(self, process):
        await asyncio.sleep(self.idle_seconds)
        async with self.lock:
            if self.process is process: await self.close()

    async def analyze(self, body):
        if self.idle: self.idle.cancel();self.idle = None
        if self.process is None or self.process.returncode is not None:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'pioneer.language_worker', '--serve',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=128*1024,
                env={**os.environ, 'MALLOC_ARENA_MAX':'1', 'PYTHONPATH':str(REPO_ROOT/'backend')})
        process = self.process
        async def request():
            process.stdin.write(json.dumps(body).encode()+b'\n')
            await process.stdin.drain()
            line = await process.stdout.readline()
            if not line: raise ValueError('языковой worker завершился')
            result = json.loads(line)
            if 'error' in result: raise ValueError('не удалось проанализировать Python')
            return result
        try:
            return await asyncio.wait_for(request(),12)
        except BaseException:
            await self.close()
            raise
        finally:
            if self.process is process:
                self.idle = asyncio.create_task(self._release_idle(process))
