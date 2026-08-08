import asyncio

from adapters.kalshi_adapter import KalshiAdapter


async def main():
    print("Initializing KalshiAdapter...")
    adapter = KalshiAdapter()
    print("Connecting...")
    connected = await adapter.connect()
    if not connected:
        print("Failed to connect!")
        return

    print("Connected successfully!")
    print("Calling adapter.get_transfers()...")
    transfers = await adapter.get_transfers()
    print("Parsed transfers count:", len(transfers))
    for t in transfers[:5]:
        print(
            "  Transfer: "
            f"id={t.id}, type={t.type}, amount={t.amount}, "
            f"status={t.status}, created_at={t.created_at}"
        )

    await adapter.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
