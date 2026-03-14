import asyncio
import json
import os
import re
from datetime import datetime, timezone

import httpx
import yaml
import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import FastAPI, Request

load_dotenv()

BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
API_BASE       = f"https://api.telegram.org/bot{BOT_TOKEN}"
ALLOWED_IDS    = set(int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip())
MANAGERS_CONFIG = os.getenv("MANAGERS_CONFIG", "managers.yaml")
PORT           = int(os.getenv("PORT", 8001))
ALERT_CHAT_ID  = int(os.getenv("ALERT_CHAT_ID", "0")) or next(iter(ALLOWED_IDS), None)
CHECK_INTERVAL = int(os.getenv("MANAGER_CHECK_INTERVAL", "300"))  # seconds

with open(MANAGERS_CONFIG, encoding="utf-8") as f:
    _config = yaml.safe_load(f)

MANAGERS: list[dict] = _config.get("managers", [])

# Redis connection pool per manager, keyed by manager name
_redis: dict[str, aioredis.Redis] = {}

app = FastAPI()


async def _check_manager(m: dict) -> tuple[bool, str]:
    """
    Checks manager health:
    - HTTP /health endpoint (if health_url is set)
    - Worker heartbeat via Redis (only if queue is non-empty)
    Returns (ok, status_line).
    """
    name = m["name"]
    lines: list[str] = []
    ok = True

    # HTTP health check
    health_url = m.get("health_url")
    if health_url:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(health_url)
            if r.status_code == 200:
                data = r.json()
                lines.append(f"бот ✅ (очередь: {data.get('queue', '?')})")
            else:
                lines.append(f"бот ❌ HTTP {r.status_code}")
                ok = False
        except Exception as e:
            lines.append(f"бот ❌ {e}")
            ok = False
    else:
        lines.append("бот — нет health_url")

    # Worker heartbeat check via Redis (skip if queue is empty)
    r = _redis.get(name)
    if r:
        try:
            queue_len = await r.llen(m.get("task_queue", "claude:tasks"))
            in_progress = await r.exists(m.get("progress_key", "claude:in_progress"))
            has_work = queue_len > 0 or in_progress

            if has_work:
                hb = await r.exists(m.get("heartbeat_key", "claude:worker:heartbeat"))
                if hb:
                    lines.append(f"воркер ✅ (задач: {queue_len})")
                else:
                    lines.append(f"воркер ❌ нет heartbeat (задач в очереди: {queue_len})")
                    ok = False
            else:
                lines.append("воркер — очередь пуста, проверка пропущена")
        except Exception as e:
            lines.append(f"воркер ❌ Redis: {e}")
            ok = False

    return ok, f"*{name}*: " + " | ".join(lines)


async def managers_watchdog() -> None:
    """Checks all managers every CHECK_INTERVAL seconds and reports to Telegram."""
    await asyncio.sleep(60)  # initial delay after startup
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        results: list[str] = []
        any_error = False
        for m in MANAGERS:
            try:
                ok, line = await _check_manager(m)
                results.append(("✅" if ok else "❌") + " " + line)
                if not ok:
                    any_error = True
            except Exception as e:
                results.append(f"❌ *{m['name']}*: неожиданная ошибка: {e}")
                any_error = True

        if ALERT_CHAT_ID:
            icon = "🚨" if any_error else "✅"
            text = f"{icon} *Проверка менеджеров*\n\n" + "\n".join(results)
            await send(ALERT_CHAT_ID, text)


@app.on_event("startup")
async def startup() -> None:
    for m in MANAGERS:
        _redis[m["name"]] = aioredis.from_url(
            m.get("redis_url", "redis://localhost:6379"),
            encoding="utf-8",
            decode_responses=True,
        )
    asyncio.create_task(managers_watchdog())


@app.on_event("shutdown")
async def shutdown() -> None:
    for r in _redis.values():
        await r.aclose()


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

async def send(chat_id: int, text: str, reply_to: int | None = None) -> dict:
    payload: dict = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{API_BASE}/sendMessage", json=payload)
        return r.json()


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

async def _queue_length(m: dict) -> int:
    r = _redis.get(m["name"])
    if not r:
        return -1
    try:
        return await r.llen(m.get("task_queue", "claude:tasks"))
    except Exception:
        return -1


async def _all_tasks(m: dict) -> list[dict]:
    r = _redis.get(m["name"])
    if not r:
        return []
    try:
        items = await r.lrange(m.get("task_queue", "claude:tasks"), 0, -1)
        return [json.loads(i) for i in items]
    except Exception:
        return []


async def _last_result(m: dict) -> dict | None:
    r = _redis.get(m["name"])
    if not r:
        return None
    try:
        raw = await r.get(m.get("last_result", "claude:last_result"))
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _push_task(m: dict, prompt: str, chat_id: int, message_id: int) -> int:
    """Put task into pending state. Returns task_num."""
    r = _redis.get(m["name"])
    if not r:
        raise RuntimeError(f"No Redis connection for manager {m['name']}")
    task_num = await r.incr(m.get("task_counter", "claude:task_counter"))
    task = {
        "task_id": f"{chat_id}:{message_id}:{datetime.now(timezone.utc).isoformat()}",
        "task_num": task_num,
        "prompt": prompt,
        "chat_id": chat_id,
        "message_id": message_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    pending_key = m.get("pending_prefix", "claude:pending:") + str(task_num)
    await r.set(pending_key, json.dumps(task))
    return task_num


# ---------------------------------------------------------------------------
# Manager lookup
# ---------------------------------------------------------------------------

def _find_by_project(keyword: str) -> list[dict]:
    kw = keyword.lower().strip()
    return [
        m for m in MANAGERS
        if kw in m["name"].lower()
        or any(kw in p.lower() for p in m.get("projects", []))
    ]


def _find_by_name(keyword: str) -> list[dict]:
    kw = keyword.lower().strip()
    return [m for m in MANAGERS if kw in m["name"].lower()]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def _handle_help(chat_id: int) -> None:
    managers_lines = "\n".join(
        f"  • <b>{m['name']}</b> — {m.get('description', '')}" for m in MANAGERS
    )
    await send(
        chat_id,
        f"🎼 <b>Дирижёр</b>\n\nУправляю {len(MANAGERS)} менеджерами:\n{managers_lines}\n\n"
        "<b>Команды:</b>\n"
        "• <code>кто делает [проект]?</code> — найти ответственного\n"
        "• <code>задачи</code> — все незавершённые задачи\n"
        "• <code>статус [менеджер]</code> — последний результат\n"
        "• <code>задача для [менеджер]: [текст]</code> — отправить задачу\n"
        "• /managers — список менеджеров с длиной очередей",
    )


async def _handle_managers(chat_id: int) -> None:
    lines: list[str] = []
    for m in MANAGERS:
        q_len = await _queue_length(m)
        projects = ", ".join(m.get("projects", [])) or "—"
        queue_str = str(q_len) if q_len >= 0 else "недоступен"
        lines.append(
            f"• <b>{m['name']}</b> — {m.get('description', '')}\n"
            f"  Проекты: {projects}\n"
            f"  В очереди: {queue_str}"
        )
    await send(chat_id, "👥 <b>Менеджеры:</b>\n\n" + "\n\n".join(lines))


async def _handle_who_does(chat_id: int, project: str) -> None:
    found = _find_by_project(project)
    if not found:
        await send(chat_id, f"🤷 Никто из менеджеров не работает над <code>{project}</code>")
        return
    verb = "делает" if len(found) == 1 else "делают"
    names = ", ".join(f"<b>{m['name']}</b>" for m in found)
    details = "\n".join(f"  • {m['name']}: {m.get('description', '')}" for m in found)
    await send(chat_id, f"🙋 Я! {names} {verb} <code>{project}</code>\n\n{details}")


async def _handle_tasks(chat_id: int) -> None:
    blocks: list[str] = []
    total = 0
    for m in MANAGERS:
        tasks = await _all_tasks(m)
        total += len(tasks)
        if not tasks:
            blocks.append(f"<b>{m['name']}</b>: очередь пуста")
        else:
            task_lines = "\n".join(
                f"  {i + 1}. {t.get('prompt', '')[:100]}"
                for i, t in enumerate(tasks)
            )
            blocks.append(f"<b>{m['name']}</b> ({len(tasks)} задач):\n{task_lines}")
    header = f"📋 Всего незавершённых задач: <b>{total}</b>\n\n"
    await send(chat_id, header + "\n\n".join(blocks))


async def _handle_status(chat_id: int, name_query: str) -> None:
    found = _find_by_name(name_query)
    if not found:
        await send(chat_id, f"🤷 Менеджер <code>{name_query}</code> не найден")
        return
    m = found[0]
    result = await _last_result(m)
    if not result:
        await send(chat_id, f"📊 <b>{m['name']}</b>: нет данных о последней задаче")
        return
    status = "✅ успех" if result.get("success") else "❌ ошибка"
    elapsed = result.get("elapsed", "?")
    prompt = result.get("prompt", "")[:120]
    finished = result.get("finished", "")[:19]
    await send(
        chat_id,
        f"📊 <b>{m['name']}</b>\n"
        f"Статус: {status}\n"
        f"Время выполнения: {elapsed}с\n"
        f"Завершено: {finished}\n"
        f"Задача: <code>{prompt}</code>",
    )


async def _handle_route_task(
    chat_id: int, message_id: int, manager_query: str, task_text: str
) -> None:
    found = _find_by_name(manager_query)
    if not found:
        await send(chat_id, f"🤷 Менеджер <code>{manager_query}</code> не найден")
        return
    m = found[0]
    try:
        task_num = await _push_task(m, task_text, chat_id, message_id)
        await send(
            chat_id,
            f"📋 <b>{m['name']}</b> — задача <b>#{task_num}</b>:\n"
            f"<code>{task_text[:200]}</code>\n\n"
            f"Подтвердить: <code>/ok_{task_num}</code>\n"
            f"Отменить: <code>/cancel_{task_num}</code>",
            reply_to=message_id,
        )
    except Exception as e:
        await send(chat_id, f"❌ Не удалось создать задачу: {e}")


# ---------------------------------------------------------------------------
# Main message dispatcher
# ---------------------------------------------------------------------------

async def handle_message(chat_id: int, user_id: int, text: str, message_id: int) -> None:
    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        await send(chat_id, "⛔ Нет доступа.")
        return

    tl = text.lower().strip()

    # /start or /help
    if tl in ("/start", "/help"):
        await _handle_help(chat_id)
        return

    # /managers
    if tl == "/managers":
        await _handle_managers(chat_id)
        return

    # "кто делает X?" / "кто работает над X?" / "кто занимается X?"
    m = re.search(r"кто\s+(?:делает|работает\s+над|занимается)\s+(.+?)[\?\!\.\s]*$", tl)
    if m:
        await _handle_who_does(chat_id, m.group(1).strip().rstrip("?!. "))
        return

    # "задача для X: текст" — must be checked before generic "задач" keyword
    m = re.match(r"задача для (.+?):\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if m:
        await _handle_route_task(chat_id, message_id, m.group(1).strip(), m.group(2).strip())
        return

    # "задачи" / "незавершённые" / "что в очереди" / "очередь"
    if any(kw in tl for kw in ("задач", "очередь", "незавершён", "не завершён")):
        await _handle_tasks(chat_id)
        return

    # "статус X"
    m = re.search(r"статус\s+(.+)", tl)
    if m:
        await _handle_status(chat_id, m.group(1).strip())
        return

    await send(chat_id, "🤔 Не понял команду. Напишите /help для справки.")


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

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
