from __future__ import annotations

import re

import db


def _like(q: str) -> str:
    return f"%{q.strip()}%"


async def search_everything(query: str, limit_each: int = 8) -> dict[str, list]:
    q = (query or "").strip()
    if not q:
        return {"orders": [], "users": [], "products": []}

    digits = re.sub(r"\D", "", q)
    numeric_id = int(digits) if digits and len(digits) <= 18 else None
    order_params = []
    order_where = []
    public_code = q.lstrip('#').strip().upper()
    if public_code:
        order_where.append("UPPER(COALESCE(public_code,''))=?")
        order_params.append(public_code)
    if digits:
        order_where.append("REPLACE(REPLACE(REPLACE(phone,' ',''),'-',''),'+','') LIKE ?")
        order_params.append(f"%{digits}%")
    if numeric_id is not None:
        order_where[:0] = ["id=?", "user_id=?"]
        order_params[:0] = [numeric_id, numeric_id]
    order_where += ["LOWER(COALESCE(username,'')) LIKE LOWER(?)", "LOWER(COALESCE(customer_name,'')) LIKE LOWER(?)"]
    order_params += [_like(q.lstrip("@")), _like(q)]
    orders = await db.fetchall(
        f"SELECT * FROM orders WHERE {' OR '.join(order_where)} ORDER BY id DESC LIMIT ?",
        tuple(order_params) + (limit_each,),
    )

    user_where = ["LOWER(username) LIKE LOWER(?)", "LOWER(full_name) LIKE LOWER(?)"]
    user_params: list = [_like(q.lstrip("@")), _like(q)]
    if numeric_id is not None:
        user_where.insert(0, "user_id=?")
        user_params.insert(0, numeric_id)
    users = await db.fetchall(
        f"SELECT * FROM users WHERE {' OR '.join(user_where)} ORDER BY last_seen_at DESC LIMIT ?",
        tuple(user_params) + (limit_each,),
    )

    product_where = ["LOWER(name) LIKE LOWER(?)", "LOWER(category) LIKE LOWER(?)", "LOWER(description) LIKE LOWER(?)"]
    product_params: list = [_like(q), _like(q), _like(q)]
    if numeric_id is not None:
        product_where.insert(0, "id=?")
        product_params.insert(0, numeric_id)
    products = await db.fetchall(
        f"SELECT * FROM products WHERE {' OR '.join(product_where)} ORDER BY id DESC LIMIT ?",
        tuple(product_params) + (limit_each,),
    )
    return {"orders": list(orders), "users": list(users), "products": list(products)}
