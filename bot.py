import asyncio
import re

from fastapi import FastAPI, Request

from config import ALLOWED_IDS, MANAGERS, PORT
from handlers import (
    handle_help,
    handle_managers,
    handle_route_task,
    handle_status,
    handle_tasks,
    handle_who_does,
)
from redis_store import close_redis, init_redis
from telegram import send
from watchdog import managers_watchdog, run_check

app = FastAPI()


@app.on_event("startup")
async def startup() -> None:
    init_redis()
    asyncio.create_task(managers_watchdog())


@app.on_event("shutdown")
async def shutdown() -> None:
    await close_redis()


async def handle_message(chat_id: int, user_id: int, text: str, message_id: int) -> None:
    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        await send(chat_id, "⛔ Нет доступа.")
        return

    tl = text.lower().strip()

    if tl in ("/start", "/help"):
        await handle_help(chat_id)
        return

    if tl == "/managers":
        await handle_managers(chat_id)
        return

    m = re.search(r"кто\s+(?:делает|работает\s+над|занимается)\s+(.+?)[\?\!\.\s]*$", tl)
    if m:
        await handle_who_does(chat_id, m.group(1).strip().rstrip("?!. "))
        return

    # "задача для X: текст" — checked before generic "задач" keyword
    m = re.match(r"задача для (.+?):\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if m:
        await handle_route_task(chat_id, message_id, m.group(1).strip(), m.group(2).strip())
        return

    if any(kw in tl for kw in ("задач", "очередь", "незавершён", "не завершён")):
        await handle_tasks(chat_id)
        return

    m = re.search(r"статус\s+(.+)", tl)
    if m:
        await handle_status(chat_id, m.group(1).strip())
        return

    if tl in ("/status", "/health"):
        await run_check(chat_id)
        return

    await send(chat_id, "🤔 Не понял команду. Напишите /help для справки.")


@app.post("/webhook")
async def webhook(request: Request) -> dict:
    data = await request.json()
    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return {"ok": True}
    text = msg.get("text", "").strip()
    if not text:
        return {"ok": True}
    asyncio.create_task(
        handle_message(
            chat_id=msg["chat"]["id"],
            user_id=msg["from"]["id"],
            text=text,
            message_id=msg["message_id"],
        )
    )
    return {"ok": True}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "managers": len(MANAGERS)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT, reload=False)
