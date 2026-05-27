import asyncio

from .evolution import EvolutionClient, EvolutionError


class ExistingInstanceClient(EvolutionClient):
    def __init__(self, states, connect_payloads):
        super().__init__("http://evolution.invalid", "test")
        self.states = list(states)
        self.connect_payloads = list(connect_payloads)
        self.create_calls = 0
        self.connect_calls = 0

    async def create_instance(self, instance_name: str):
        self.create_calls += 1
        raise EvolutionError('POST /instance/create failed: 403 This name "vendor_test" is already in use.')

    async def connection_state(self, instance_name: str) -> str:
        if self.states:
            return self.states.pop(0)
        return "close"

    async def connect_instance(self, instance_name: str):
        self.connect_calls += 1
        if self.connect_payloads:
            return self.connect_payloads.pop(0)
        return {"instance": {"state": "close"}}


async def main():
    opened = ExistingInstanceClient(
        states=["close", "close", "open"],
        connect_payloads=[{"instance": {"state": "connecting"}}],
    )
    result = await opened.ensure_fresh_qr("vendor_test")
    assert result == ""
    assert opened.create_calls == 1
    assert opened.connect_calls == 1
    print("existing.open_after_wait.ok")

    qr = ExistingInstanceClient(
        states=["close"],
        connect_payloads=[{"qrcode": {"base64": "data:image/png;base64,abc123"}}],
    )
    result = await qr.ensure_fresh_qr("vendor_test")
    assert result == "abc123"
    assert qr.connect_calls == 1
    print("existing.qr_after_connect.ok")

    ready = ExistingInstanceClient(
        states=["close", "connecting", "open"],
        connect_payloads=[
            {"instance": {"state": "connecting"}},
            {"instance": {"state": "open"}},
        ],
    )
    assert await ready.wait_until_open("vendor_test", timeout_seconds=5)
    assert ready.connect_calls == 2
    print("existing.wait_until_open.ok")


if __name__ == "__main__":
    asyncio.run(main())
