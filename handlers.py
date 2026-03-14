from config import MANAGERS
from managers import find_by_name, find_by_project
from redis_store import all_tasks, last_result, push_task, queue_length
from telegram import send


async def handle_help(chat_id: int) -> None:
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
        "• <code>как дела</code> — проверить здоровье всех менеджеров\n"
        "• /managers — список менеджеров с длиной очередей",
    )


async def handle_managers(chat_id: int) -> None:
    lines: list[str] = []
    for m in MANAGERS:
        q_len = await queue_length(m)
        projects = ", ".join(m.get("projects", [])) or "—"
        queue_str = str(q_len) if q_len >= 0 else "недоступен"
        lines.append(
            f"• <b>{m['name']}</b> — {m.get('description', '')}\n"
            f"  Проекты: {projects}\n"
            f"  В очереди: {queue_str}"
        )
    await send(chat_id, "👥 <b>Менеджеры:</b>\n\n" + "\n\n".join(lines))


async def handle_who_does(chat_id: int, project: str) -> None:
    found = find_by_project(project)
    if not found:
        await send(chat_id, f"🤷 Никто из менеджеров не работает над <code>{project}</code>")
        return
    verb = "делает" if len(found) == 1 else "делают"
    names = ", ".join(f"<b>{m['name']}</b>" for m in found)
    details = "\n".join(f"  • {m['name']}: {m.get('description', '')}" for m in found)
    await send(chat_id, f"🙋 Я! {names} {verb} <code>{project}</code>\n\n{details}")


async def handle_tasks(chat_id: int) -> None:
    blocks: list[str] = []
    total = 0
    for m in MANAGERS:
        tasks = await all_tasks(m)
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


async def handle_status(chat_id: int, name_query: str) -> None:
    found = find_by_name(name_query)
    if not found:
        await send(chat_id, f"🤷 Менеджер <code>{name_query}</code> не найден")
        return
    m = found[0]
    result = await last_result(m)
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


async def handle_route_task(
    chat_id: int, message_id: int, manager_query: str, task_text: str
) -> None:
    found = find_by_name(manager_query)
    if not found:
        await send(chat_id, f"🤷 Менеджер <code>{manager_query}</code> не найден")
        return
    m = found[0]
    try:
        task_num = await push_task(m, task_text, chat_id, message_id)
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
