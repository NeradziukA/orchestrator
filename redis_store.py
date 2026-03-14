import json
from datetime import datetime, timezone

import redis.asyncio as aioredis

from config import MANAGERS

# Redis connection pool per manager, keyed by manager name
_redis: dict[str, aioredis.Redis] = {}


def init_redis() -> None:
    for m in MANAGERS:
        _redis[m["name"]] = aioredis.from_url(
            m.get("redis_url", "redis://localhost:6379"),
            encoding="utf-8",
            decode_responses=True,
        )


async def close_redis() -> None:
    for r in _redis.values():
        await r.aclose()


def get(manager_name: str) -> aioredis.Redis | None:
    return _redis.get(manager_name)


async def queue_length(m: dict) -> int:
    r = _redis.get(m["name"])
    if not r:
        return -1
    try:
        return await r.llen(m.get("task_queue", "claude:tasks"))
    except Exception:
        return -1


async def all_tasks(m: dict) -> list[dict]:
    r = _redis.get(m["name"])
    if not r:
        return []
    try:
        items = await r.lrange(m.get("task_queue", "claude:tasks"), 0, -1)
        return [json.loads(i) for i in items]
    except Exception:
        return []


async def last_result(m: dict) -> dict | None:
    r = _redis.get(m["name"])
    if not r:
        return None
    try:
        raw = await r.get(m.get("last_result", "claude:last_result"))
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def push_task(m: dict, prompt: str, chat_id: int, message_id: int) -> int:
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
    # Signal the manager bot to send confirmation request in its own chat
    await r.rpush(m.get("notify_queue", "claude:notify"), str(task_num))
    return task_num
