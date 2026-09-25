from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts

logger = logging.getLogger(__name__)


class ToolExecutionError(RuntimeError):
    pass


class EvidenceGateway:
    def __init__(self, endpoint: str, team_api_key: str, contracts: Contracts) -> None:
        self._endpoint = endpoint
        self._team_api_key = team_api_key
        self._contracts = contracts
        self._session: ClientSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._call_count: int = 0

    async def _connect(self) -> None:
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except (Exception, BaseExceptionGroup) as e:
                logger.debug("Error closing prior exit stack: %s", e)
            finally:
                self._exit_stack = None
                self._session = None

        self._exit_stack = AsyncExitStack()
        headers = {"Authorization": f"Bearer {self._team_api_key}"}
        timeout = httpx2.Timeout(300.0, connect=60.0, write=60.0, pool=60.0)
        http_client = await self._exit_stack.enter_async_context(
            httpx2.AsyncClient(headers=headers, timeout=timeout)
        )
        read_stream, write_stream = await self._exit_stack.enter_async_context(
            streamable_http_client(self._endpoint, http_client=http_client)
        )
        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self._session = session
        self._call_count = 0

    async def close(self) -> None:
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except (Exception, BaseExceptionGroup) as e:
                logger.debug("Error closing exit stack: %s", e)
            finally:
                self._exit_stack = None
                self._session = None

    async def list_tools(self) -> list[str]:
        if self._session is None:
            await self._connect()
        assert self._session is not None
        response = await self._session.list_tools()
        return sorted(tool.name for tool in response.tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        self._call_count += 1
        if self._call_count > 60:
            try:
                await self._connect()
            except (Exception, BaseExceptionGroup) as exc:
                logger.debug("Proactive refresh failed, continuing: %s", exc)

        for attempt in range(3):
            try:
                if self._session is None:
                    await self._connect()
                assert self._session is not None
                result = await self._session.call_tool(tool_name, arguments=payload)
                is_error = getattr(result, "is_error", None)
                if is_error is None:
                    is_error = getattr(result, "isError", False)
                if is_error:
                    message = " ".join(
                        block.text for block in result.content if getattr(block, "text", None)
                    )
                    raise ToolExecutionError(f"MCP tool {tool_name} returned error: {message or 'unknown'}")

                evidence = getattr(result, "structuredContent", None)
                if evidence is None:
                    evidence = getattr(result, "structured_content", None)
                if evidence is None:
                    text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
                    if len(text_blocks) != 1:
                        raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
                    evidence = json.loads(text_blocks[0])

                self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
                return evidence
            except ToolExecutionError:
                raise
            except (Exception, BaseExceptionGroup) as exc:
                if attempt == 2:
                    raise RuntimeError(f"MCP network error after 3 attempts: {exc}") from exc
                logger.warning("MCP network error during %s (%s). Reconnecting in 1.5s...", tool_name, exc)
                self._session = None
                if self._exit_stack is not None:
                    try:
                        await self._exit_stack.aclose()
                    except (Exception, BaseExceptionGroup):
                        pass
                    self._exit_stack = None
                await asyncio.sleep(1.5)
                try:
                    await self._connect()
                except (Exception, BaseExceptionGroup) as reconnect_err:
                    logger.warning("Reconnect failed: %s", reconnect_err)


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    gateway = EvidenceGateway(endpoint, team_api_key, contracts)
    await gateway._connect()
    try:
        yield gateway
    finally:
        await gateway.close()
