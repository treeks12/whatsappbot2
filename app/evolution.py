import asyncio
import base64
from pathlib import Path
from typing import Any, Dict, Optional

import aiofiles
import aiohttp


class EvolutionError(RuntimeError):
    pass


class EvolutionClient:
    def __init__(self, base_url: str, api_key: str, max_parallel_media_uploads: int = 2):
        self.base_url = base_url.rstrip("/")
        self.headers = {"apikey": api_key, "Content-Type": "application/json"}
        self.timeout = aiohttp.ClientTimeout(total=120, connect=20, sock_connect=20, sock_read=90)
        self.connector: Optional[aiohttp.TCPConnector] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.media_semaphore = asyncio.Semaphore(max(1, max_parallel_media_uploads))

    async def start(self):
        if self.session is None or self.session.closed:
            self.connector = aiohttp.TCPConnector(limit=12, limit_per_host=8, ttl_dns_cache=300)
            self.session = aiohttp.ClientSession(timeout=self.timeout, connector=self.connector)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def _request(self, method: str, path: str, *, json: Optional[dict] = None) -> Dict[str, Any]:
        if self.session is None or self.session.closed:
            await self.start()

        url = f"{self.base_url}{path}"
        async with self.session.request(method, url, json=json, headers=self.headers) as response:
            try:
                payload = await response.json()
            except Exception:
                payload = {"raw": await response.text()}

            if response.status >= 400:
                raise EvolutionError(f"{method} {path} failed: {response.status} {payload}")

            return payload

    async def create_instance(self, instance_name: str) -> Dict[str, Any]:
        payload = {
            "instanceName": instance_name,
            "qrcode": True,
            "integration": "WHATSAPP-BAILEYS",
        }
        return await self._request("POST", "/instance/create", json=payload)

    async def delete_instance(self, instance_name: str) -> Dict[str, Any]:
        return await self._request("DELETE", f"/instance/delete/{instance_name}")

    async def connect_instance(self, instance_name: str) -> Dict[str, Any]:
        return await self._request("GET", f"/instance/connect/{instance_name}")

    async def disconnect_instance(self, instance_name: str) -> bool:
        # Evolution v2 exposes logout endpoints, but logout can unpair the phone.
        # For "disconnect without losing session" the safe local action is stopping
        # the Evolution container, handled by the caller as fallback.
        return False

    async def info(self) -> Dict[str, Any]:
        return await self._request("GET", "/")

    async def connection_state(self, instance_name: str) -> str:
        try:
            payload = await self._request("GET", f"/instance/connectionState/{instance_name}")
        except Exception:
            return "close"

        state = payload.get("instance", {}).get("state") or payload.get("state") or payload.get("connectionStatus")
        return state or "close"

    async def license_status(self) -> Dict[str, Any]:
        return await self._request("GET", "/license/status")

    async def ensure_fresh_qr(self, instance_name: str) -> str:
        state = await self.connection_state(instance_name)
        if state == "open":
            return ""

        try:
            create_payload = await self.create_instance(instance_name)
        except EvolutionError as exc:
            if not _is_existing_instance_error(exc):
                raise
            qr = await self._wait_existing_instance(instance_name)
            if qr is not None:
                return qr
            raise EvolutionError(
                f"Instancia {instance_name} ja existe, mas a Evolution nao retornou QR. "
                "Ela pode estar em inicializacao ou presa em um estado antigo. "
                "O bot nao apaga/recria instancias automaticamente para preservar a sessao."
            )

        qr = _extract_qr(create_payload)
        if qr:
            return qr

        connect_payload = await self.connect_instance(instance_name)
        qr = _extract_qr(connect_payload)
        if not qr:
            raise EvolutionError(f"QR Code not returned for {instance_name}: {connect_payload}")
        return qr

    async def _wait_existing_instance(self, instance_name: str, timeout_seconds: int = 45) -> Optional[str]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            state = await self.connection_state(instance_name)
            if state == "open":
                return ""

            try:
                connect_payload = await self.connect_instance(instance_name)
            except EvolutionError as exc:
                await asyncio.sleep(2)
                continue

            qr = _extract_qr(connect_payload)
            if qr:
                return qr

            nested_state = (
                connect_payload.get("instance", {}).get("state")
                or connect_payload.get("state")
                or connect_payload.get("connectionStatus")
            )
            if nested_state == "open":
                return ""

            await asyncio.sleep(2)

        return None

    async def wait_until_open(self, instance_name: str, timeout_seconds: int = 75) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            state = await self.connection_state(instance_name)
            if state == "open":
                return True
            try:
                payload = await self.connect_instance(instance_name)
            except EvolutionError:
                await asyncio.sleep(2)
                continue
            nested_state = (
                payload.get("instance", {}).get("state")
                or payload.get("state")
                or payload.get("connectionStatus")
            )
            if nested_state == "open":
                return True
            await asyncio.sleep(2)
        return False

    async def send_text(self, instance_name: str, phone: str, text: str):
        return await self._request(
            "POST",
            f"/message/sendText/{instance_name}",
            json={"number": normalize_phone(phone), "text": text},
        )

    async def send_media(
        self,
        instance_name: str,
        phone: str,
        media_path: str,
        mime_type: str,
        file_name: str,
        caption: str = "",
        media_base64: Optional[str] = None,
    ):
        async with self.media_semaphore:
            media = media_base64 if media_base64 is not None else await file_to_base64(media_path)
            payload = {
                "number": normalize_phone(phone),
                "mediatype": "image",
                "mimetype": mime_type,
                "caption": caption,
                "media": media,
                "fileName": file_name,
            }
            del media
            result = await self._request(
                "POST",
                f"/message/sendMedia/{instance_name}",
                json=payload,
            )
            del payload
            return result


def normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith("0") and len(digits) in (11, 12):
        digits = digits[1:]
    elif digits.startswith("0") and len(digits) in (13, 14):
        digits = digits[3:]
    if len(digits) in (10, 11):
        return f"55{digits}"
    if digits.startswith("550") and len(digits) in (13, 14):
        return "55" + digits[3:]
    return digits


async def file_to_base64(path: str) -> str:
    async with aiofiles.open(path, "rb") as f:
        raw = await f.read()
    encoded = base64.b64encode(raw).decode("ascii")
    del raw
    return encoded


async def file_to_data_uri(path: str, mime_type: str) -> str:
    encoded = await file_to_base64(path)
    return f"data:{mime_type};base64,{encoded}"


def _extract_qr(payload: Dict[str, Any]) -> str:
    direct = payload.get("base64") or payload.get("qrcode")
    if isinstance(direct, str):
        return direct.split(",", 1)[-1] if direct.startswith("data:image") else direct
    if isinstance(direct, dict):
        nested = direct.get("base64") or direct.get("code")
        if isinstance(nested, str):
            return nested.split(",", 1)[-1] if nested.startswith("data:image") else nested
    return ""


def _is_existing_instance_error(exc: EvolutionError) -> bool:
    message = str(exc).lower()
    return "already in use" in message or "already exists" in message
