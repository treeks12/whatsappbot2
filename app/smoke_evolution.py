import asyncio
import os
import time

from dotenv import load_dotenv

from .evolution import EvolutionClient, EvolutionError


async def main():
    load_dotenv()
    base_url = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
    api_key = os.getenv("EVOLUTION_API_KEY", "")
    async with EvolutionClient(base_url, api_key) as client:
        print(f"Checking Evolution API at {base_url}")
        root = await client._request("GET", "/")
        print(f"root.version={root.get('version')} clientName={root.get('clientName')}")

        try:
            license_status = await client.license_status()
            print(f"license.status={license_status.get('status')} instance_id={license_status.get('instance_id')}")
        except EvolutionError as exc:
            print(f"license.status=unavailable ({exc})")

        try:
            instances = await client._request("GET", "/instance/fetchInstances")
            print(f"fetchInstances.ok count={len(instances) if isinstance(instances, list) else 'n/a'}")
        except EvolutionError as exc:
            print(f"fetchInstances.blocked {exc}")
            return

        probe_name = f"smoke_probe_{int(time.time())}"
        try:
            created = await client.create_instance(probe_name)
            print(f"createInstance.ok keys={','.join(created.keys())}")
            connected = await client.connect_instance(probe_name)
            print(f"connectInstance.ok keys={','.join(connected.keys())}")
        finally:
            try:
                await client.delete_instance(probe_name)
                print("deleteInstance.ok")
            except Exception as exc:
                print(f"deleteInstance.warn {exc}")


if __name__ == "__main__":
    asyncio.run(main())
