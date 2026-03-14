from config import MANAGERS


def find_by_project(keyword: str) -> list[dict]:
    kw = keyword.lower().strip()
    return [
        m for m in MANAGERS
        if kw in m["name"].lower()
        or any(kw in p.lower() for p in m.get("projects", []))
    ]


def find_by_name(keyword: str) -> list[dict]:
    kw = keyword.lower().strip()
    return [m for m in MANAGERS if kw in m["name"].lower()]
