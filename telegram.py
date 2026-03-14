import httpx

from config import API_BASE


async def send(chat_id: int, text: str, reply_to: int | None = None) -> dict:
    payload: dict = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{API_BASE}/sendMessage", json=payload)
        return r.json()
