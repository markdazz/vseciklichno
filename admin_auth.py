from __future__ import annotations

import db


async def admin_role(user_id: int) -> str | None:
    row = await db.fetchone("SELECT role FROM admin_users WHERE user_id=?", (user_id,))
    return row["role"] if row else None


async def is_admin(user_id: int) -> bool:
    return (await admin_role(user_id)) is not None
