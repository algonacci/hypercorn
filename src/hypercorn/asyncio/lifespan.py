from __future__ import annotations

import asyncio
from functools import partial

from ..config import Config
from ..typing import AppWrapper, ASGIReceiveEvent, ASGISendEvent, LifespanScope
from ..utils import LifespanFailureError, LifespanTimeoutError


class UnexpectedMessageError(Exception):
    pass


class Lifespan:
    def __init__(self, app: AppWrapper, config: Config, loop: asyncio.AbstractEventLoop) -> None:
        self.app = app
        self.config = config
        self.startup = asyncio.Event()
        self.shutdown = asyncio.Event()
        self.app_queue: asyncio.Queue = asyncio.Queue(config.max_app_queue_size)
        self.supported = True
        self.loop = loop

        # This mimics the Trio nursery.start task_status and is
        # required to ensure the support has been checked before
        # waiting on timeouts.
        self._started = asyncio.Event()

    async def handle_lifespan(self) -> None:
        self._started.set()
        scope: LifespanScope = {"type": "lifespan", "asgi": {"spec_version": "2.0"}}
        try:
            await self.app(
                scope, self.asgi_receive, self.asgi_send, partial(self.loop.run_in_executor, None)
            )
        except LifespanFailureError:
            # Lifespan failures should crash the server
            raise
        except Exception:
            self.supported = False
            if not self.startup.is_set():
                await self.config.log.warning(
                    "ASGI Framework Lifespan error, continuing without Lifespan support"
                )
            elif not self.shutdown.is_set():
                await self.config.log.exception(
                    "ASGI Framework Lifespan error, shutdown without Lifespan support"
                )
            else:
                await self.config.log.exception("ASGI Framework Lifespan errored after shutdown.")
        finally:
            self.startup.set()
            self.shutdown.set()

    async def wait_for_startup(self) -> None:
        await self._started.wait()
        if not self.supported:
            return

        await self.app_queue.put({"type": "lifespan.startup"})
        try:
            await asyncio.wait_for(self.startup.wait(), timeout=self.config.startup_timeout)
        except asyncio.TimeoutError as error:
            raise LifespanTimeoutError("startup") from error

    async def wait_for_shutdown(self) -> None:
        await self._started.wait()
        if not self.supported:
            return

        await self.app_queue.put({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(self.shutdown.wait(), timeout=self.config.shutdown_timeout)
        except asyncio.TimeoutError as error:
            raise LifespanTimeoutError("shutdown") from error

    async def asgi_receive(self) -> ASGIReceiveEvent:
        return await self.app_queue.get()

    async def asgi_send(self, message: ASGISendEvent) -> None:
        if message["type"] == "lifespan.startup.complete":
            self.startup.set()
        elif message["type"] == "lifespan.shutdown.complete":
            self.shutdown.set()
        elif message["type"] == "lifespan.startup.failed":
            self.startup.set()
            raise LifespanFailureError("startup", message["message"])
        elif message["type"] == "lifespan.shutdown.failed":
            self.shutdown.set()
            raise LifespanFailureError("shutdown", message["message"])
        else:
            raise UnexpectedMessageError(message["type"])
