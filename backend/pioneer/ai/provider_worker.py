"""Private JSON pipe, SDK only; no tool execution or hardware access in this worker."""

import asyncio
import json
import sys

if sys.platform == "linux":
    import os
    import resource

    # Napi-C has ~414 MiB usable and runs other services. Fail this worker,
    # rather than exhaust the host, if an SDK/request exceeds its budget.
    resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
    import threading

    threading.stack_size(512 * 1024)
    os.nice(10)

from pioneer.ai import AISettings
from pioneer.ai.providers import direct_provider


async def main():
    provider = None
    try:
        init = json.loads(sys.stdin.readline())
        provider = direct_provider(AISettings(**init["settings"]), init["key"])
        init.clear()
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            async for part in provider.stream(**command):
                print(json.dumps(part, ensure_ascii=False), flush=True)
            print('{"end":true}', flush=True)
    except Exception as e:
        # Never send SDK exception strings/requests/headers back to core or journal.
        print(
            json.dumps({"provider_error": True, "status": getattr(e, "status_code", None)}),
            flush=True,
        )
    finally:
        if provider:
            await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
