import asyncio
import logging
from typing import Optional

from .db import Database
from .docker_control import DockerControl, DockerControlError
from .evolution import EvolutionClient


logger = logging.getLogger(__name__)


class EvolutionPowerManager:
    def __init__(self, evolution: EvolutionClient, docker: Optional[DockerControl] = None):
        self.evolution = evolution
        self.docker = docker
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.docker is not None

    async def start(self):
        if self.docker:
            await self.docker.start()

    async def close(self):
        if self.docker:
            await self.docker.close()

    async def container_state(self) -> str:
        if not self.docker:
            return "unmanaged"
        return await self.docker.state()

    async def ensure_running(self):
        if not self.docker:
            await self.evolution.start()
            return

        async with self._lock:
            state = await self.docker.state()
            if state != "running":
                logger.info("Ligando Evolution API container (%s -> running)", state)
                await self.docker.start_container()
                await self.docker.wait_until_running()
            await self.evolution.start()
            await self._wait_for_api()

    async def stop_container(self):
        if not self.docker:
            raise DockerControlError("Controle Docker nao configurado")

        async with self._lock:
            state = await self.docker.state()
            if state == "running":
                logger.info("Parando Evolution API container")
                await self.evolution.close()
                await self.docker.stop_container()

    async def stop_if_idle(self, db: Database):
        if not self.docker:
            return
        if await db.count_running_or_paused_campaigns() > 0:
            return
        try:
            await self.stop_container()
        except DockerControlError:
            logger.warning("Nao foi possivel parar Evolution API automaticamente", exc_info=True)

    async def _wait_for_api(self, timeout_seconds: int = 90):
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_error = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                await self.evolution.info()
                return
            except Exception as exc:
                last_error = exc
                try:
                    await self.evolution.license_status()
                    return
                except Exception as license_exc:
                    last_error = license_exc
                await asyncio.sleep(2)
        raise DockerControlError(f"Evolution API nao respondeu em {timeout_seconds}s: {last_error}")
