import asyncio
from typing import Any, Optional
from urllib.parse import quote

import aiohttp


class DockerControlError(RuntimeError):
    pass


class DockerControl:
    def __init__(self, socket_path: str, container_name: str):
        self.socket_path = socket_path
        self.container_name = container_name
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if self.session is None or self.session.closed:
            connector = aiohttp.UnixConnector(path=self.socket_path)
            timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=45)
            self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _request(self, method: str, path: str, *, ok_statuses: tuple[int, ...] = (200,)) -> Any:
        if self.session is None or self.session.closed:
            await self.start()

        assert self.session is not None
        url = f"http://docker{path}"
        try:
            async with self.session.request(method, url) as response:
                body = await response.text()
                if response.status not in ok_statuses:
                    raise DockerControlError(f"Docker {method} {path} failed: {response.status} {body[:300]}")
                if not body:
                    return {}
                try:
                    return await response.json()
                except Exception:
                    return {"raw": body}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise DockerControlError(f"Docker {method} {path} error: {exc}") from exc

    def _container_path(self, suffix: str) -> str:
        return f"/containers/{quote(self.container_name, safe='')}{suffix}"

    async def state(self) -> str:
        try:
            payload = await self._request("GET", self._container_path("/json"))
        except DockerControlError as exc:
            if "404" in str(exc):
                return "missing"
            raise

        state = payload.get("State", {})
        if state.get("Running"):
            return "running"
        if state.get("Paused"):
            return "paused"
        if state.get("Restarting"):
            return "restarting"
        return state.get("Status") or "stopped"

    async def is_running(self) -> bool:
        return await self.state() == "running"

    async def start_container(self):
        await self._request("POST", self._container_path("/start"), ok_statuses=(204, 304))

    async def stop_container(self, timeout_seconds: int = 20):
        try:
            await self._request(
                "POST",
                self._container_path(f"/stop?t={max(1, timeout_seconds)}"),
                ok_statuses=(204, 304),
            )
        except DockerControlError:
            if await self.state() != "running":
                return
            raise

    async def wait_until_running(self, timeout_seconds: int = 60):
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if await self.is_running():
                return
            await asyncio.sleep(1)
        raise DockerControlError(f"Container {self.container_name} nao ficou running em {timeout_seconds}s")
