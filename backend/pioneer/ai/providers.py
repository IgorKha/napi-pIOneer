"""Lazy official SDK adapters. One request, no invisible retries; cancellation closes HTTP."""

from __future__ import annotations

from typing import Protocol
import asyncio
import json
import sys
import os


def tool_outputs(kind, calls, outputs):
    if kind == "anthropic":
        return [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": c["id"], "content": o}
                    for c, o in zip(calls, outputs)
                ],
            }
        ]
    return [
        {"role": "tool", "tool_call_id": c["id"], "content": o}
        if kind == "custom"
        else {"type": "function_call_output", "call_id": c["id"], "output": o}
        for c, o in zip(calls, outputs)
    ]


class LLMProvider(Protocol):
    def stream(self, messages: list[dict], system: str, tools: list[dict], budget: int): ...
    def results(self, calls: list[dict], outputs: list[str]) -> list[dict]: ...
    async def close(self): ...


class OpenAIProvider:
    def __init__(self, settings, key):
        from openai import AsyncOpenAI

        self.settings = settings
        self.custom = settings.provider == "custom"
        self.client = AsyncOpenAI(
            api_key=key,
            base_url=settings.base_url if self.custom else None,
            timeout=90,
            max_retries=0,
        )

    async def stream(self, messages, system, tools, budget):
        s = self.settings
        if self.custom:
            # Compatibility endpoints generally implement Chat Completions, not Responses.
            opts = dict(
                model=s.model,
                messages=[{"role": "system", "content": system}, *messages],
                tools=[{"type": "function", "function": t} for t in tools],
                max_tokens=budget,
                stream=True,
                stream_options={"include_usage": True},
            )
            if s.effort != "default":
                opts["reasoning_effort"] = s.effort
            calls, content, usage, finish = {}, "", {}, None
            async with await self.client.chat.completions.create(**opts) as stream:
                async for chunk in stream:
                    if chunk.usage:
                        usage = {
                            "input": chunk.usage.prompt_tokens,
                            "output": chunk.usage.completion_tokens,
                        }
                    if not chunk.choices:
                        continue
                    d = chunk.choices[0].delta
                    finish = chunk.choices[0].finish_reason or finish
                    if d.content:
                        content += d.content
                        yield {"delta": d.content}
                    for c in d.tool_calls or []:
                        call = calls.setdefault(
                            c.index,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if c.id:
                            call["id"] = c.id
                        if c.function:
                            if c.function.name:
                                call["function"]["name"] = c.function.name
                            call["function"]["arguments"] += c.function.arguments or ""
            wire = {"role": "assistant", "content": content or None}
            if calls:
                wire["tool_calls"] = list(calls.values())
            yield {
                "result": [wire],
                "calls": [{"id": c["id"], **c["function"]} for c in calls.values()],
                "usage": usage,
                "incomplete": finish in ("length", "content_filter"),
            }
            return
        opts = dict(
            model=s.model,
            instructions=system,
            input=messages,
            tools=[{"type": "function", **t} for t in tools],
            max_output_tokens=budget,
            stream=True,
            store=False,
            include=["reasoning.encrypted_content"],
        )
        if s.search == "builtin":
            opts["tools"].append({"type": "web_search", "search_context_size": "low"})
            opts["max_tool_calls"] = 3
        if s.effort != "default":
            opts["reasoning"] = {"effort": s.effort}
        completed = None
        async with await self.client.responses.create(**opts) as stream:
            async for event in stream:
                if event.type in ("response.output_text.delta", "response.refusal.delta"):
                    yield {"delta": event.delta}
                elif event.type == "response.output_text.annotation.added":
                    a = event.annotation
                    if isinstance(a, dict) and a.get("type") == "url_citation":
                        yield {"citation": {"url": a["url"], "title": a.get("title", a["url"])}}
                elif event.type in ("response.completed", "response.incomplete"):
                    completed = event.response
                elif event.type in ("error", "response.failed"):
                    raise RuntimeError("provider_stream_failed")
        if completed is None:
            raise RuntimeError("provider_stream_incomplete")
        output = [i.model_dump(exclude_none=True) for i in completed.output]
        calls = [
            {"id": i.call_id, "name": i.name, "arguments": i.arguments}
            for i in completed.output
            if i.type == "function_call"
        ]
        u = completed.usage
        usage = (
            {
                "input": u.input_tokens,
                "output": u.output_tokens,
                "cached": u.input_tokens_details.cached_tokens if u.input_tokens_details else 0,
            }
            if u
            else {}
        )
        yield {
            "result": output,
            "calls": calls,
            "usage": usage,
            "incomplete": completed.status != "completed",
        }

    def results(self, calls, outputs):
        return tool_outputs(self.settings.provider, calls, outputs)

    async def close(self):
        await self.client.close()


class AnthropicProvider:
    def __init__(self, settings, key):
        from anthropic import AsyncAnthropic

        self.settings = settings
        self.client = AsyncAnthropic(api_key=key, timeout=90, max_retries=0)

    async def stream(self, messages, system, tools, budget):
        s = self.settings
        opts = dict(
            model=s.model,
            messages=messages,
            max_tokens=budget,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["parameters"],
                }
                for t in tools
            ],
        )
        if s.search == "builtin":
            opts["tools"] += [
                {"type": "web_search_20260209", "name": "web_search", "max_uses": 3},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3},
            ]
        if s.effort != "default":
            opts.update(thinking={"type": "adaptive"}, output_config={"effort": s.effort})
        async with self.client.messages.stream(**opts) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    yield {"delta": event.delta.text}
                elif event.type == "content_block_delta" and event.delta.type == "citations_delta":
                    a = event.delta.citation
                    if getattr(a, "url", None):
                        yield {"citation": {"url": a.url, "title": getattr(a, "title", a.url)}}
            msg = await stream.get_final_message()
        import json

        content = [i.model_dump(exclude_none=True) for i in msg.content]
        calls = [
            {"id": i.id, "name": i.name, "arguments": json.dumps(i.input)}
            for i in msg.content
            if i.type == "tool_use"
        ]
        u = msg.usage
        yield {
            "result": [{"role": "assistant", "content": content}],
            "calls": calls,
            "usage": {
                "input": u.input_tokens,
                "output": u.output_tokens,
                "cached": u.cache_read_input_tokens or 0,
                "cache_write": u.cache_creation_input_tokens or 0,
            },
            "incomplete": msg.stop_reason == "max_tokens",
        }

    def results(self, calls, outputs):
        return tool_outputs("anthropic", calls, outputs)

    async def close(self):
        await self.client.close()


def direct_provider(settings, key) -> LLMProvider:
    return (
        AnthropicProvider(settings, key)
        if settings.provider == "anthropic"
        else OpenAIProvider(settings, key)
    )


class ProviderError(Exception):
    def __init__(self, status=None):
        self.status_code = status
        super().__init__("provider worker failed")


class SubprocessProvider:
    """SDK models stay outside core RSS; worker exists only during one chat request."""

    def __init__(self, settings, key):
        self.settings, self.key, self.process = settings, key, None

    async def stream(self, messages, system, tools, budget):
        if self.process is None:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pioneer.ai.provider_worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=2 * 1024 * 1024,
                env={**os.environ, "MALLOC_ARENA_MAX": "1"},
            )
            init = {"settings": self.settings.model_dump(), "key": self.key}
            self.process.stdin.write((json.dumps(init) + "\n").encode())
            self.key = ""  # child gets the key only through its private pipe
        command = {"messages": messages, "system": system, "tools": tools, "budget": budget}
        self.process.stdin.write((json.dumps(command, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()
        while line := await self.process.stdout.readline():
            part = json.loads(line)
            if "provider_error" in part:
                raise ProviderError(part.get("status"))
            if part.get("end"):
                return
            yield part
        raise ProviderError()

    def results(self, calls, outputs):
        return tool_outputs(self.settings.provider, calls, outputs)

    async def close(self):
        p = self.process
        if p is None or p.returncode is not None:
            return
        p.stdin.close()
        try:
            await asyncio.wait_for(p.wait(), 1)
        except TimeoutError:
            p.terminate()
            try:
                await asyncio.wait_for(p.wait(), 1)
            except TimeoutError:
                p.kill()
                await p.wait()


def create_provider(settings, key) -> LLMProvider:
    return (
        SubprocessProvider(settings, key)
        if sys.platform == "linux"
        else direct_provider(settings, key)
    )
