import asyncpg

pool: asyncpg.Pool | None = None


async def init(dsn: str) -> None:
    global pool
    pool = await asyncpg.create_pool(dsn)
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id           SERIAL PRIMARY KEY,
            pos          DOUBLE PRECISION NOT NULL,
            kind         TEXT NOT NULL,                 -- warmup | post | reminder
            src_chat     BIGINT NOT NULL,
            src_msg      BIGINT NOT NULL,
            preview      TEXT NOT NULL DEFAULT '',
            btn_text     TEXT,
            btn_url      TEXT,
            delete_after INT NOT NULL DEFAULT 0,        -- секунд після публікації, 0 = не видаляти
            status       TEXT NOT NULL DEFAULT 'pending', -- pending | sent | failed | deleted
            sent_at      TIMESTAMPTZ,
            ch_msg_id    BIGINT,
            delete_at    TIMESTAMPTZ
        )
        """
    )
    await pool.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


async def get_setting(key: str, default: str | None = None) -> str | None:
    v = await pool.fetchval("SELECT value FROM settings WHERE key=$1", key)
    return default if v is None else v


async def set_setting(key: str, value: str) -> None:
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1,$2) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        key, str(value),
    )


async def add(kind, src_chat, src_msg, preview, delete_after, after_id=None) -> int:
    if after_id:
        cur = await pool.fetchval("SELECT pos FROM items WHERE id=$1", after_id)
        nxt = await pool.fetchval("SELECT min(pos) FROM items WHERE status='pending' AND pos>$1", cur)
        pos = cur + 1 if nxt is None else (cur + nxt) / 2
    else:
        pos = (await pool.fetchval("SELECT coalesce(max(pos),0) FROM items")) + 1
    return await pool.fetchval(
        "INSERT INTO items (pos, kind, src_chat, src_msg, preview, delete_after) "
        "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
        pos, kind, src_chat, src_msg, preview, delete_after,
    )


async def get(item_id: int) -> dict | None:
    r = await pool.fetchrow("SELECT * FROM items WHERE id=$1", item_id)
    return dict(r) if r else None


async def pending() -> list[dict]:
    return [dict(r) for r in await pool.fetch("SELECT * FROM items WHERE status='pending' ORDER BY pos")]


async def recent_sent(limit: int = 5) -> list[dict]:
    rows = await pool.fetch("SELECT * FROM items WHERE sent_at IS NOT NULL ORDER BY sent_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


async def tail_kinds(n: int = 2) -> list[str]:
    rows = await pool.fetch("SELECT kind FROM items WHERE status='pending' ORDER BY pos DESC LIMIT $1", n)
    return [r["kind"] for r in rows]


async def update(item_id: int, **fields) -> None:
    keys = list(fields)
    sets = ", ".join(f"{k}=${i + 2}" for i, k in enumerate(keys))
    await pool.execute(f"UPDATE items SET {sets} WHERE id=$1", item_id, *[fields[k] for k in keys])


async def remove(item_id: int) -> None:
    await pool.execute("DELETE FROM items WHERE id=$1 AND status='pending'", item_id)


async def clear_pending() -> None:
    await pool.execute("DELETE FROM items WHERE status='pending'")


async def move(item_id: int, direction: int) -> bool:
    """direction -1 вгору, +1 вниз (серед pending)."""
    items = await pending()
    ids = [i["id"] for i in items]
    if item_id not in ids:
        return False
    idx = ids.index(item_id)
    j = idx + direction
    if j < 0 or j >= len(items):
        return False
    a, b = items[idx], items[j]
    await pool.execute("UPDATE items SET pos=$2 WHERE id=$1", a["id"], b["pos"])
    await pool.execute("UPDATE items SET pos=$2 WHERE id=$1", b["id"], a["pos"])
    return True


async def mark_sent(item_id: int, ch_msg_id: int, delete_after: int) -> None:
    await pool.execute(
        "UPDATE items SET status='sent', sent_at=now(), ch_msg_id=$2, "
        "delete_at = CASE WHEN $3::int > 0 THEN now() + make_interval(secs => $3::int) END WHERE id=$1",
        item_id, ch_msg_id, delete_after,
    )


async def due_deletions() -> list[dict]:
    rows = await pool.fetch("SELECT * FROM items WHERE status='sent' AND delete_at IS NOT NULL AND delete_at <= now()")
    return [dict(r) for r in rows]
