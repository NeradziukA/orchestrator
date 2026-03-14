import asyncio
import json

import httpx

from config import ALERT_CHAT_ID, CHECK_INTERVAL, MANAGERS
from redis_store import get as redis_get
from telegram import send


async def check_manager(m: dict) -> tuple[bool, str]:
    """
    Checks manager health:
    - HTTP /health endpoint (if health_url is set)
    - Worker heartbeat via Redis (only if there is active work)
    Returns (ok, status_line).
    """
    name = m["name"]
    lines: list[str] = []
    ok = True

    # HTTP health check
    health_url = m.get("health_url")
    if health_url:
        try:
            async with httpx.AsyncClient(timeout=8, verify=False) as client:
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
    r = redis_get(name)
    if r:
        try:
            queue_len = await r.llen(m.get("task_queue", "claude:tasks"))
            progress_raw = await r.get(m.get("progress_key", "claude:in_progress"))
            has_work = queue_len > 0 or bool(progress_raw)

            if has_work:
                hb = await r.exists(m.get("heartbeat_key", "claude:worker:heartbeat"))
                status_parts = []
                if progress_raw:
                    try:
                        t = json.loads(progress_raw)
                        status_parts.append(f"в работе: #{t.get('task_num', '?')}")
                    except Exception:
                        status_parts.append("в работе")
                if queue_len:
                    status_parts.append(f"в очереди: {queue_len}")
                status_str = ", ".join(status_parts)
                if hb:
                    lines.append(f"воркер ✅ ({status_str})")
                else:
                    lines.append(f"воркер ❌ нет heartbeat ({status_str})")
                    ok = False
            else:
                lines.append("воркер — очередь пуста, проверка пропущена")
        except Exception as e:
            lines.append(f"воркер ❌ Redis: {e}")
            ok = False

    return ok, f"*{name}*: " + " | ".join(lines)


async def run_check(chat_id: int) -> None:
    """Run health check for all managers and send result to chat_id."""
    results: list[str] = []
    any_error = False
    for m in MANAGERS:
        try:
            ok, line = await check_manager(m)
            results.append(("✅" if ok else "❌") + " " + line)
            if not ok:
                any_error = True
        except Exception as e:
            results.append(f"❌ *{m['name']}*: неожиданная ошибка: {e}")
            any_error = True
    icon = "🚨" if any_error else "✅"
    await send(chat_id, f"{icon} *Проверка менеджеров*\n\n" + "\n".join(results))


async def managers_watchdog() -> None:
    """Checks all managers every CHECK_INTERVAL seconds.
    Only alerts if there is active work or an error — skips if all queues are empty.
    """
    await asyncio.sleep(60)  # initial delay after startup
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        if not ALERT_CHAT_ID:
            continue
        # Skip silent check if no manager has active work
        has_any_work = False
        for m in MANAGERS:
            r = redis_get(m["name"])
            if not r:
                continue
            try:
                queue_len = await r.llen(m.get("task_queue", "claude:tasks"))
                in_progress = await r.exists(m.get("progress_key", "claude:in_progress"))
                if queue_len > 0 or in_progress:
                    has_any_work = True
                    break
            except Exception:
                has_any_work = True  # Redis error counts as something to report
                break
        if has_any_work:
            await run_check(ALERT_CHAT_ID)
