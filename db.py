import asyncpg

pool: asyncpg.Pool | None = None


async def init(dsn: str) -> None:
    global pool
    pool = await asyncpg.create_pool(dsn)
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS sets (
            id         SERIAL PRIMARY KEY,
            kind       TEXT NOT NULL,                      -- single | mutual | night
            status     TEXT NOT NULL DEFAULT 'draft',      -- draft | scheduled | done | cancelled
            start_at   TIMESTAMPTZ,
            p2_at      TIMESTAMPTZ,
            end_at     TIMESTAMPTZ,
            next_role  TEXT NOT NULL DEFAULT 'warmup',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id         SERIAL PRIMARY KEY,
            set_id     INT NOT NULL REFERENCES sets(id) ON DELETE CASCADE,
            role       TEXT NOT NULL,                      -- warmup | post | reminder
            part       INT NOT NULL DEFAULT 1,             -- для ночи: 1 или 2
            pos        DOUBLE PRECISION NOT NULL,
            text_html  TEXT NOT NULL DEFAULT '',
            media_type TEXT,                               -- photo | video | animation | document
            file_id    TEXT,
            btn_text   TEXT,
            btn_url    TEXT,
            custom_ttl INT,                                -- NULL авто, 0 не удалять, >0 секунд после публикации
            send_at    TIMESTAMPTZ,
            delete_at  TIMESTAMPTZ,
            status     TEXT NOT NULL DEFAULT 'draft',      -- draft | pending | sent | deleted | failed
            ch_msg_ids TEXT,
            sent_at    TIMESTAMPTZ
        )
        """
    )
    await pool.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS src_chat BIGINT")
    await pool.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS src_msg BIGINT")
    await pool.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS text_msg BIGINT")
    await pool.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS media_msg BIGINT")
    await pool.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS via TEXT")
    await pool.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS text_ts BIGINT")
    await pool.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS media_ts BIGINT")
    await pool.execute("ALTER TABLE sets ADD COLUMN IF NOT EXISTS paused BOOLEAN NOT NULL DEFAULT false")
    await pool.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS media_url TEXT")
    await pool.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS btns TEXT")
    await pool.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS ents TEXT")
    await pool.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS plain TEXT")
    await pool.execute("ALTER TABLE sets ADD COLUMN IF NOT EXISTS repeat TEXT")
    await pool.execute("CREATE TABLE IF NOT EXISTS files (token TEXT PRIMARY KEY, data BYTEA NOT NULL, mime TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    await pool.execute("DELETE FROM files f WHERE f.created_at < now() - interval '30 days' AND NOT EXISTS (SELECT 1 FROM items i WHERE i.media_url LIKE '%/m/' || f.token || '.jpg')")
    await pool.execute("CREATE TABLE IF NOT EXISTS snaps (item_id INT NOT NULL, kind TEXT NOT NULL, views INT NOT NULL DEFAULT 0, "
                       "forwards INT NOT NULL DEFAULT 0, reactions INT NOT NULL DEFAULT 0, replies INT NOT NULL DEFAULT 0, "
                       "taken_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (item_id, kind))")
    await pool.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


async def get_setting(key: str, default: str | None = None) -> str | None:
    v = await pool.fetchval("SELECT value FROM settings WHERE key=$1", key)
    return default if v is None else v


async def set_setting(key: str, value) -> None:
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1,$2) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        key, str(value),
    )


async def _upd(table: str, row_id: int, fields: dict) -> None:
    keys = list(fields)
    sets = ", ".join(f"{k}=${i + 2}" for i, k in enumerate(keys))
    await pool.execute(f"UPDATE {table} SET {sets} WHERE id=$1", row_id, *[fields[k] for k in keys])


# ---------- наборы ----------

async def new_set(kind: str) -> int:
    nxt = "post" if kind == "single" else "warmup"
    return await pool.fetchval("INSERT INTO sets (kind, next_role) VALUES ($1,$2) RETURNING id", kind, nxt)


async def get_set(set_id: int) -> dict | None:
    r = await pool.fetchrow("SELECT * FROM sets WHERE id=$1", set_id)
    return dict(r) if r else None


async def upd_set(set_id: int, **f) -> None:
    await _upd("sets", set_id, f)


async def delete_set(set_id: int) -> None:
    await pool.execute("DELETE FROM sets WHERE id=$1", set_id)


async def active_sets() -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT s.*, (SELECT count(*) FROM items i WHERE i.set_id=s.id) AS n,
               (SELECT min(send_at) FROM items i WHERE i.set_id=s.id AND i.status='pending') AS next_at
        FROM sets s WHERE s.status IN ('draft','scheduled') ORDER BY s.id
        """
    )
    return [dict(r) for r in rows]


async def drop_empty_drafts(except_id: int | None = None) -> None:
    await pool.execute(
        "DELETE FROM sets s WHERE s.status='draft' AND s.id IS DISTINCT FROM $1::int "
        "AND NOT EXISTS (SELECT 1 FROM items i WHERE i.set_id=s.id)",
        except_id,
    )


# ---------- элементы ----------

async def add_item(set_id: int, role: str, part: int, fields: dict, status: str, after_id: int | None = None) -> int:
    if after_id:
        cur = await pool.fetchval("SELECT pos FROM items WHERE id=$1", after_id)
        nxt = await pool.fetchval("SELECT min(pos) FROM items WHERE set_id=$1 AND pos>$2", set_id, cur)
        pos = cur + 1 if nxt is None else (cur + nxt) / 2
    else:
        pos = (await pool.fetchval("SELECT coalesce(max(pos),0) FROM items WHERE set_id=$1", set_id)) + 1
    return await pool.fetchval(
        "INSERT INTO items (set_id, role, part, pos, text_html, media_type, file_id, status, src_chat, src_msg, "
        "text_msg, media_msg, text_ts, media_ts, btn_text, btn_url, media_url, btns, ents, plain) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20) RETURNING id",
        set_id, role, part, pos, fields.get("text_html", ""), fields.get("media_type"), fields.get("file_id"), status,
        fields.get("src_chat"), fields.get("src_msg"), fields.get("text_msg"), fields.get("media_msg"),
        fields.get("text_ts"), fields.get("media_ts"), fields.get("btn_text"), fields.get("btn_url"), fields.get("media_url"), fields.get("btns"), fields.get("ents"), fields.get("plain"),
    )


async def items_of(set_id: int) -> list[dict]:
    rows = await pool.fetch("SELECT * FROM items WHERE set_id=$1 ORDER BY pos", set_id)
    return [dict(r) for r in rows]


async def get_item(item_id: int) -> dict | None:
    r = await pool.fetchrow("SELECT * FROM items WHERE id=$1", item_id)
    return dict(r) if r else None


async def upd_item(item_id: int, **f) -> None:
    await _upd("items", item_id, f)


async def del_item(item_id: int) -> None:
    await pool.execute("DELETE FROM items WHERE id=$1 AND status IN ('draft','pending')", item_id)


async def move(item_id: int, direction: int) -> bool:
    it = await get_item(item_id)
    rows = await pool.fetch(
        "SELECT id, pos FROM items WHERE set_id=$1 AND status IN ('draft','pending') ORDER BY pos", it["set_id"]
    )
    ids = [r["id"] for r in rows]
    if item_id not in ids:
        return False
    i = ids.index(item_id)
    j = i + direction
    if j < 0 or j >= len(rows):
        return False
    await pool.execute("UPDATE items SET pos=$2 WHERE id=$1", rows[i]["id"], rows[j]["pos"])
    await pool.execute("UPDATE items SET pos=$2 WHERE id=$1", rows[j]["id"], rows[i]["pos"])
    return True


async def mark_sent(item_id: int, msg_ids: list[int], via: str = "bot") -> None:
    await pool.execute(
        "UPDATE items SET status='sent', sent_at=now(), ch_msg_ids=$2, via=$3 WHERE id=$1",
        item_id, ",".join(map(str, msg_ids)), via,
    )


async def due_sends() -> list[dict]:
    rows = await pool.fetch(
        "SELECT i.* FROM items i JOIN sets s ON s.id=i.set_id "
        "WHERE s.status='scheduled' AND NOT s.paused AND i.status='pending' AND i.send_at <= now() ORDER BY i.send_at, i.pos"
    )
    return [dict(r) for r in rows]


async def due_deletions() -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM items WHERE status='sent' AND delete_at IS NOT NULL AND delete_at <= now()"
    )
    return [dict(r) for r in rows]


async def unfinished_sets() -> list[int]:
    """Запущенные наборы, где уже нечего ни публиковать, ни удалять."""
    rows = await pool.fetch(
        """
        SELECT s.id FROM sets s WHERE s.status='scheduled' AND NOT EXISTS (
            SELECT 1 FROM items i WHERE i.set_id=s.id
              AND (i.status='pending' OR (i.status='sent' AND i.delete_at IS NOT NULL)))
        """
    )
    return [r["id"] for r in rows]


async def timeline() -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT i.*, s.kind FROM items i JOIN sets s ON s.id=i.set_id
        WHERE s.status='scheduled' AND (i.status='pending' OR (i.status='sent' AND i.delete_at IS NOT NULL))
        """
    )
    return [dict(r) for r in rows]


async def put_file(token: str, data: bytes, mime: str) -> None:
    await pool.execute("INSERT INTO files (token, data, mime) VALUES ($1,$2,$3) ON CONFLICT (token) DO NOTHING", token, data, mime)


async def get_file(token: str):
    r = await pool.fetchrow("SELECT data, mime FROM files WHERE token=$1", token)
    return (bytes(r["data"]), r["mime"]) if r else None


async def clone_set(set_id: int, start_at) -> int:
    """Копия набора для следующего повтора: те же сообщения, кнопки, таймеры; ничего ещё не опубликовано."""
    src = await get_set(set_id)
    new_id = await pool.fetchval(
        "INSERT INTO sets (kind, next_role, start_at, repeat) VALUES ($1,$2,$3,$4) RETURNING id",
        src["kind"], src["next_role"], start_at, src["repeat"])
    await pool.execute(
        "INSERT INTO items (set_id, role, part, pos, text_html, media_type, file_id, status, src_chat, src_msg, text_msg, media_msg, "
        "text_ts, media_ts, btn_text, btn_url, btns, custom_ttl, media_url, ents, plain) "
        "SELECT $2, role, part, pos, text_html, media_type, file_id, 'draft', src_chat, src_msg, text_msg, media_msg, "
        "text_ts, media_ts, btn_text, btn_url, btns, custom_ttl, media_url, ents, plain FROM items WHERE set_id=$1 AND status<>'failed'",
        set_id, new_id)
    return new_id


SNAP_AGES = (("1h", 60), ("6h", 360), ("24h", 1440))


async def due_snaps(limit: int = 20) -> list[dict]:
    """Опубликованные посты, у которых подошло время снимка (час/6 часов/сутки) и снимка ещё нет; старше 2 часов после срока не догоняем."""
    out = []
    for kind, mins in SNAP_AGES:
        rows = await pool.fetch(
            "SELECT i.id, i.ch_msg_ids, $1::text AS kind FROM items i WHERE i.status='sent' AND i.ch_msg_ids IS NOT NULL "
            "AND i.sent_at <= now() - make_interval(mins => $2::int) AND i.sent_at > now() - make_interval(mins => $2::int + 120) "
            "AND NOT EXISTS (SELECT 1 FROM snaps s WHERE s.item_id=i.id AND s.kind=$1::text) LIMIT $3::int", kind, mins, limit)
        out += [dict(r) for r in rows]
    return out[:limit]


async def save_snap(item_id: int, kind: str, views: int, forwards: int, reactions: int, replies: int) -> None:
    await pool.execute(
        "INSERT INTO snaps (item_id, kind, views, forwards, reactions, replies) VALUES ($1,$2,$3,$4,$5,$6) "
        "ON CONFLICT (item_id, kind) DO NOTHING", item_id, kind, views, forwards, reactions, replies)


async def snap_rows(days: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT sn.kind AS sk, sn.views, sn.forwards, sn.reactions, sn.replies, i.role, i.media_type, i.sent_at, "
        "i.plain AS text, s.kind AS set_kind FROM snaps sn JOIN items i ON i.id=sn.item_id JOIN sets s ON s.id=i.set_id "
        "WHERE sn.views >= 0 AND i.sent_at > now() - make_interval(days => $1::int)", days)
    return [dict(r) for r in rows]
