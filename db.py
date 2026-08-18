from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
import logging
import secrets
import string
import aiosqlite

from config import settings

NOW = lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S')
log = logging.getLogger(__name__)

ORDER_CODE_ALPHABET = string.ascii_uppercase + string.digits


async def _new_public_order_code(conn) -> str:
    """Return a unique six-character public order code.

    Internal numeric IDs remain the database primary key; this code is the only
    order number shown to customers/admins.
    """
    for _ in range(200):
        code = ''.join(secrets.choice(ORDER_CODE_ALPHABET) for _ in range(6))
        cur = await conn.execute('SELECT 1 FROM orders WHERE public_code=? LIMIT 1', (code,))
        if await cur.fetchone() is None:
            return code
    raise RuntimeError('Could not allocate a unique public order code')


def public_order_ref(order_row) -> str:
    """Human-facing order reference like #DX12XX."""
    if not order_row:
        return '#??????'
    try:
        code = str(order_row['public_code'] or '').strip().upper()
    except Exception:
        code = ''
    return f'#{code}' if code else '#??????'



def parse_sizes(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for raw in (text or '').split(','):
        raw = raw.strip()
        if not raw:
            continue
        if ':' in raw:
            size, qty = raw.split(':', 1)
            try:
                stock = max(0, int(qty.strip()))
            except ValueError:
                stock = 0
        else:
            size, stock = raw, 10
        if size.strip():
            out[size.strip()] = stock
    return out


def sizes_to_text(data: dict[str, int]) -> str:
    return ','.join(f'{k}:{v}' for k, v in data.items())


def parse_variant_spec(text: str) -> dict[tuple[str, str], int]:
    # Черный:S=5,M=3; Белый:S=2,M=0
    result: dict[tuple[str, str], int] = {}
    for block in (text or '').split(';'):
        block = block.strip()
        if not block or ':' not in block:
            continue
        color, rest = block.split(':', 1)
        color = color.strip() or 'Основной'
        for pair in rest.split(','):
            pair = pair.strip()
            if not pair:
                continue
            if '=' in pair:
                size, qty = pair.split('=', 1)
            elif ':' in pair:
                size, qty = pair.split(':', 1)
            else:
                continue
            try:
                stock = max(0, int(qty.strip()))
            except ValueError:
                continue
            if size.strip():
                result[(color, size.strip())] = stock
    return result


async def _columns(db, table: str) -> set[str]:
    cur = await db.execute(f'PRAGMA table_info({table})')
    return {r[1] for r in await cur.fetchall()}


async def _add_column(db, table: str, name: str, ddl: str):
    if name not in await _columns(db, table):
        await db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')


async def init_db():
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute('PRAGMA journal_mode=WAL')
        await db.execute('PRAGMA synchronous=NORMAL')
        await db.execute('PRAGMA busy_timeout=30000')
        await db.executescript('''
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            description_html TEXT NOT NULL DEFAULT '',
            sizes TEXT NOT NULL DEFAULT 'ONE:10',
            photo_url TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS cart(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            size TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id,product_id,size)
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            customer_name TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            total INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Ожидает оплаты',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS order_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            size TEXT NOT NULL,
            qty INTEGER NOT NULL,
            price INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS legal_acceptances(
            user_id INTEGER PRIMARY KEY,
            legal_version TEXT NOT NULL,
            accepted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            full_name TEXT NOT NULL DEFAULT '',
            first_started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            broadcasts_enabled INTEGER NOT NULL DEFAULT 1,
            is_blocked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS delivery_profiles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            recipient_full_name TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            delivery_method TEXT NOT NULL DEFAULT '',
            postal_code TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '',
            street TEXT NOT NULL DEFAULT '',
            house TEXT NOT NULL DEFAULT '',
            building TEXT NOT NULL DEFAULT '',
            apartment TEXT NOT NULL DEFAULT '',
            cdek_type TEXT NOT NULL DEFAULT '',
            cdek_point TEXT NOT NULL DEFAULT '',
            delivery_comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_delivery_profiles_user
            ON delivery_profiles(user_id,last_used_at);

        CREATE TABLE IF NOT EXISTS broadcast_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            source_message_id INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            sent INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS product_variants(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            color TEXT NOT NULL DEFAULT 'Основной',
            size TEXT NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0,
            UNIQUE(product_id,color,size)
        );
        CREATE TABLE IF NOT EXISTS product_photos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS categories_config(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS category_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'photo',
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_category_media_category ON category_media(category_id,sort_order,id);
        CREATE TABLE IF NOT EXISTS size_chart_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'photo',
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_size_chart_media_order ON size_chart_media(sort_order,id);
        CREATE TABLE IF NOT EXISTS cart_items_v5(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            variant_id INTEGER NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id,variant_id)
        );
        CREATE TABLE IF NOT EXISTS promo_codes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            percent INTEGER NOT NULL,
            min_order INTEGER NOT NULL DEFAULT 0,
            max_uses INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS promo_usages(
            promo_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            used_at TEXT NOT NULL,
            UNIQUE(promo_id,order_id)
        );
        CREATE TABLE IF NOT EXISTS order_status_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            actor_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            rating INTEGER NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS restock_requests(
            user_id INTEGER NOT NULL,
            variant_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id,variant_id)
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ui_button_labels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            button_key TEXT UNIQUE NOT NULL,
            default_text TEXT NOT NULL,
            custom_text TEXT NOT NULL DEFAULT '',
            custom_emoji_id TEXT NOT NULL DEFAULT '',
            custom_style TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'inline',
            group_name TEXT NOT NULL DEFAULT 'common',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_ui_button_labels_group ON ui_button_labels(group_name,id);
        CREATE TABLE IF NOT EXISTS premium_emoji_rules(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fallback_text TEXT UNIQUE NOT NULL,
            custom_emoji_id TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS premium_emoji_packs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            set_name TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            sticker_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS premium_emoji_pack_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pack_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            custom_emoji_id TEXT UNIQUE NOT NULL,
            fallback_text TEXT NOT NULL DEFAULT '💎',
            file_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_premium_emoji_pack_items_pack
            ON premium_emoji_pack_items(pack_id,position,id);
        CREATE TABLE IF NOT EXISTS premium_emoji_placements(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pack_item_id INTEGER,
            custom_emoji_id TEXT NOT NULL,
            fallback_text TEXT NOT NULL DEFAULT '💎',
            match_text TEXT NOT NULL,
            position TEXT NOT NULL DEFAULT 'before',
            updated_at TEXT NOT NULL DEFAULT '',
            UNIQUE(custom_emoji_id,match_text,position)
        );
        CREATE INDEX IF NOT EXISTS idx_premium_emoji_placements_item
            ON premium_emoji_placements(pack_item_id,id);
        CREATE TABLE IF NOT EXISTS premium_emoji_favorites(
            admin_id INTEGER NOT NULL,
            pack_item_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(admin_id,pack_item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_premium_emoji_favorites_admin
            ON premium_emoji_favorites(admin_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS premium_emoji_recent(
            admin_id INTEGER NOT NULL,
            pack_item_id INTEGER NOT NULL,
            last_used_at TEXT NOT NULL,
            use_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(admin_id,pack_item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_premium_emoji_recent_admin
            ON premium_emoji_recent(admin_id,last_used_at DESC);
        CREATE TABLE IF NOT EXISTS inventory_reservations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            variant_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(order_id,variant_id)
        );
        CREATE INDEX IF NOT EXISTS idx_inventory_reservations_variant
            ON inventory_reservations(variant_id,expires_at);
        CREATE INDEX IF NOT EXISTS idx_inventory_reservations_order
            ON inventory_reservations(order_id);
        CREATE TABLE IF NOT EXISTS referrals(
            invitee_id INTEGER PRIMARY KEY,
            inviter_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            rewarded INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS privacy_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            processed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS admin_users(
            user_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        ''')

        await _add_column(db, 'ui_button_labels', 'custom_emoji_id', "TEXT NOT NULL DEFAULT ''")
        await _add_column(db, 'ui_button_labels', 'custom_style', "TEXT NOT NULL DEFAULT ''")

        for name, ddl in {
            'old_price':'INTEGER NOT NULL DEFAULT 0',
            'is_new':'INTEGER NOT NULL DEFAULT 0',
            'is_hit':'INTEGER NOT NULL DEFAULT 0',
            'weight_grams':f'INTEGER NOT NULL DEFAULT {settings.default_product_weight}',
            'description_html':"TEXT NOT NULL DEFAULT ''",
            'name_html':"TEXT NOT NULL DEFAULT ''",
            'availability_status':"TEXT NOT NULL DEFAULT 'in_stock'",
        }.items():
            await _add_column(db, 'products', name, ddl)

        for name, ddl in {
            'cart_updated_at':'TEXT',
            'cart_reminder_sent_at':'TEXT',
            'bonus_balance':'INTEGER NOT NULL DEFAULT 0',
            'welcome_sent_at':'TEXT',
        }.items():
            await _add_column(db, 'users', name, ddl)

        # FIX18: старые одноразовые маркеры массового импорта больше не используются.
        await db.execute("DELETE FROM settings WHERE key='builtin_premium_packs_v16_attempted'")

        order_cols = {
            'receipt_file_id':'TEXT','receipt_type':'TEXT','paid_at':'TEXT','confirmed_at':'TEXT',
            'recipient_full_name':'TEXT','delivery_method':'TEXT','postal_code':'TEXT','region':'TEXT','city':'TEXT',
            'street':'TEXT','house':'TEXT','building':'TEXT','apartment':'TEXT','cdek_type':'TEXT','cdek_point':'TEXT',
            'delivery_comment':'TEXT','tracking_number':'TEXT','tracking_sent_at':'TEXT',
            'subtotal':'INTEGER NOT NULL DEFAULT 0','discount_amount':'INTEGER NOT NULL DEFAULT 0',
            'loyalty_discount':'INTEGER NOT NULL DEFAULT 0','bonus_used':'INTEGER NOT NULL DEFAULT 0',
            'shipping_cost':'INTEGER NOT NULL DEFAULT 0','promo_code':'TEXT','payment_method':'TEXT',
            'telegram_payment_charge_id':'TEXT','provider_payment_charge_id':'TEXT','admin_note':'TEXT','delivered_at':'TEXT',
            'reservation_expires_at':'TEXT','benefits_applied_at':'TEXT',
            'payment_notice_sent_at':'TEXT','low_stock_alert_sent_at':'TEXT','public_code':'TEXT',
        }
        for name, ddl in order_cols.items():
            await _add_column(db, 'orders', name, ddl)
        await _add_column(db, 'order_items', 'color', 'TEXT')
        await _add_column(db, 'order_items', 'variant_id', 'INTEGER')
        await _add_column(db, 'product_photos', 'color', "TEXT NOT NULL DEFAULT ''")
        await _add_column(db, 'product_photos', 'media_type', "TEXT NOT NULL DEFAULT 'photo'")

        # Public order references: old numeric IDs stay internal, while every
        # existing/new order gets a stable random six-character code.
        cur = await db.execute("SELECT id FROM orders WHERE public_code IS NULL OR TRIM(public_code)='' ORDER BY id")
        for row in await cur.fetchall():
            code = await _new_public_order_code(db)
            await db.execute('UPDATE orders SET public_code=? WHERE id=?', (code, int(row[0])))

        await db.executescript('''
        CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_username ON orders(username);
        CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders(phone);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_public_code ON orders(public_code);
        CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
        CREATE INDEX IF NOT EXISTS idx_order_items_variant ON order_items(variant_id);
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
        CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_cart_items_user ON cart_items_v5(user_id);
        CREATE INDEX IF NOT EXISTS idx_product_variants_product ON product_variants(product_id);
        CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
        CREATE INDEX IF NOT EXISTS idx_order_history_order ON order_status_history(order_id,created_at DESC);
        ''')

        # owner
        if settings.admin_id:
            await db.execute(
                "INSERT OR IGNORE INTO admin_users(user_id,role,added_at) VALUES (?,'owner',?)",
                (settings.admin_id, NOW()),
            )
        await db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES ('size_chart',?)",
            (settings.size_chart_text,),
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES ('size_chart_photo','')"
        )
        await db.execute(
            "INSERT OR IGNORE INTO categories_config(name,created_at) SELECT DISTINCT category, ? FROM products WHERE TRIM(category)<>''",
            (NOW(),),
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES ('size_chart_media_type','')"
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES ('size_chart_media_file_id','')"
        )
        default_welcome = "Привет, {first_name}! 👋\n\nВыбирайте товары и оформляйте доставку прямо в боте."
        await db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES ('welcome_text',?)",
            (default_welcome,),
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES ('welcome_media_type',?)",
            ('photo' if settings.main_banner else '',),
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES ('welcome_media_file_id',?)",
            (settings.main_banner or '',),
        )

        # Migrate the old size-chart photo setting to the generic media settings once.
        old_chart_photo = await db.execute("SELECT value FROM settings WHERE key='size_chart_photo'")
        old_chart_photo = await old_chart_photo.fetchone()
        new_chart_media = await db.execute("SELECT value FROM settings WHERE key='size_chart_media_file_id'")
        new_chart_media = await new_chart_media.fetchone()
        if old_chart_photo and old_chart_photo[0] and not (new_chart_media and new_chart_media[0]):
            await db.execute("UPDATE settings SET value='photo' WHERE key='size_chart_media_type'")
            await db.execute("UPDATE settings SET value=? WHERE key='size_chart_media_file_id'", (old_chart_photo[0],))

        # Migrate the legacy single size-chart media setting into the new multi-media table once.
        chart_media_count = await db.execute('SELECT COUNT(*) FROM size_chart_media')
        chart_media_count = (await chart_media_count.fetchone())[0]
        if chart_media_count == 0:
            legacy_type = await db.execute("SELECT value FROM settings WHERE key='size_chart_media_type'")
            legacy_type = await legacy_type.fetchone()
            legacy_file = await db.execute("SELECT value FROM settings WHERE key='size_chart_media_file_id'")
            legacy_file = await legacy_file.fetchone()
            legacy_type_value = (legacy_type[0] if legacy_type else '').strip().lower()
            legacy_file_value = (legacy_file[0] if legacy_file else '').strip()
            if legacy_file_value and legacy_type_value in {'photo','video'}:
                await db.execute(
                    'INSERT INTO size_chart_media(file_id,media_type,sort_order) VALUES (?,?,0)',
                    (legacy_file_value, legacy_type_value),
                )

        db.row_factory = aiosqlite.Row
        # legacy product migration
        cur = await db.execute('SELECT id,sizes,photo_url FROM products')
        for product in await cur.fetchall():
            c = await db.execute('SELECT COUNT(*) FROM product_variants WHERE product_id=?',(product['id'],))
            if (await c.fetchone())[0] == 0:
                for size, stock in parse_sizes(product['sizes']).items():
                    await db.execute(
                        "INSERT OR IGNORE INTO product_variants(product_id,color,size,stock) VALUES (?,'Основной',?,?)",
                        (product['id'], size, stock),
                    )
            if product['photo_url']:
                await db.execute(
                    '''INSERT INTO product_photos(product_id,file_id,sort_order)
                       SELECT ?,?,0 WHERE NOT EXISTS(
                         SELECT 1 FROM product_photos WHERE product_id=? AND file_id=?
                       )''',
                    (product['id'],product['photo_url'],product['id'],product['photo_url']),
                )
        # legacy cart migration
        cur = await db.execute('SELECT user_id,product_id,size,qty FROM cart')
        for row in await cur.fetchall():
            v = await db.execute(
                '''SELECT id FROM product_variants WHERE product_id=? AND size=?
                   ORDER BY CASE WHEN color='Основной' THEN 0 ELSE 1 END,id LIMIT 1''',
                (row['product_id'],row['size']),
            )
            vr = await v.fetchone()
            if vr:
                await db.execute(
                    '''INSERT INTO cart_items_v5(user_id,variant_id,qty) VALUES (?,?,?)
                       ON CONFLICT(user_id,variant_id) DO UPDATE SET qty=MAX(qty,excluded.qty)''',
                    (row['user_id'],vr['id'],row['qty']),
                )
        # known users migration
        await db.execute('''INSERT OR IGNORE INTO users(user_id,username,full_name,first_started_at,last_seen_at,broadcasts_enabled,is_blocked)
                            SELECT user_id,'','',accepted_at,accepted_at,1,0 FROM legal_acceptances''')
        await db.execute('''INSERT OR IGNORE INTO users(user_id,username,full_name,first_started_at,last_seen_at,broadcasts_enabled,is_blocked)
                            SELECT user_id,COALESCE(MAX(username),''),COALESCE(MAX(customer_name),''),MIN(created_at),MAX(created_at),1,0
                            FROM orders WHERE user_id<>0 GROUP BY user_id''')
        # One-time migration: existing customers must not receive the new first-start welcome retroactively.
        c = await db.execute("SELECT 1 FROM settings WHERE key='welcome_sent_migration_v1'")
        if not await c.fetchone():
            await db.execute("UPDATE users SET welcome_sent_at=first_started_at WHERE welcome_sent_at IS NULL")
            await db.execute("INSERT INTO settings(key,value) VALUES ('welcome_sent_migration_v1','1')")
        await db.commit()


async def fetchone(sql: str, params=()):
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute('PRAGMA busy_timeout=30000')
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        return await cur.fetchone()


async def fetchall(sql: str, params=()):
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute('PRAGMA busy_timeout=30000')
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        return await cur.fetchall()


async def execute(sql: str, params=()):
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute('PRAGMA busy_timeout=30000')
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid


async def database_health() -> dict[str, object]:
    import os
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute('PRAGMA busy_timeout=30000')
        cur = await db.execute('PRAGMA quick_check')
        quick = (await cur.fetchone())[0]
        cur = await db.execute('PRAGMA journal_mode')
        journal = (await cur.fetchone())[0]
        cur = await db.execute('PRAGMA foreign_keys')
        foreign_keys = int((await cur.fetchone())[0] or 0)
    return {
        'quick_check': str(quick),
        'journal_mode': str(journal),
        'foreign_keys': foreign_keys,
        'size_bytes': os.path.getsize(settings.db_path) if os.path.exists(settings.db_path) else 0,
    }


# ---------- settings/admin ----------
async def get_setting(key: str, default='') -> str:
    row = await fetchone('SELECT value FROM settings WHERE key=?',(key,))
    return row['value'] if row else default

async def set_setting(key: str, value: str):
    await execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))

async def get_admins(): return await fetchall('SELECT * FROM admin_users ORDER BY role,user_id')
async def set_admin(user_id:int, role:str): await execute('INSERT INTO admin_users(user_id,role,added_at) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role',(user_id,role,NOW()))
async def delete_admin(user_id:int): await execute('DELETE FROM admin_users WHERE user_id=?',(user_id,))
async def audit(admin_id:int, action:str, details=''): await execute('INSERT INTO audit_logs(admin_id,action,details,created_at) VALUES (?,?,?,?)',(admin_id,action,details[:2000],NOW()))
async def audit_logs(limit=30): return await fetchall('SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?',(limit,))

# ---------- users/legal/referrals ----------
async def register_user(user):
    now=NOW()
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute('''INSERT INTO users(user_id,username,full_name,first_started_at,last_seen_at,broadcasts_enabled,is_blocked)
                            VALUES (?,?,?,?,?,1,0)
                            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,full_name=excluded.full_name,last_seen_at=excluded.last_seen_at,is_blocked=0''',
                         (user.id,user.username or '',user.full_name or '',now,now))
        await db.commit()

async def legal_accepted(user_id:int, version:str)->bool:
    return bool(await fetchone('SELECT 1 FROM legal_acceptances WHERE user_id=? AND legal_version=?',(user_id,version)))
async def accept_legal(user_id:int,version:str): await execute('INSERT INTO legal_acceptances(user_id,legal_version,accepted_at) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET legal_version=excluded.legal_version,accepted_at=excluded.accepted_at',(user_id,version,NOW()))
async def set_broadcasts(user_id:int,enabled:bool): await execute('UPDATE users SET broadcasts_enabled=? WHERE user_id=?',(1 if enabled else 0,user_id))
async def mark_blocked(user_id:int): await execute('UPDATE users SET is_blocked=1,broadcasts_enabled=0 WHERE user_id=?',(user_id,))
async def broadcast_user_ids():
    rows=await fetchall('SELECT user_id FROM users WHERE broadcasts_enabled=1 AND is_blocked=0 AND user_id<>? ORDER BY user_id',(settings.admin_id,))
    return [r['user_id'] for r in rows]

async def save_broadcast_log(source_message_id,total,sent,failed,blocked): await execute('INSERT INTO broadcast_logs(created_at,source_message_id,total,sent,failed,blocked) VALUES (?,?,?,?,?,?)',(NOW(),source_message_id,total,sent,failed,blocked))
async def broadcast_logs(limit=10): return await fetchall('SELECT * FROM broadcast_logs ORDER BY id DESC LIMIT ?',(limit,))
async def add_referral(invitee:int,inviter:int):
    if invitee!=inviter and inviter>0: await execute('INSERT OR IGNORE INTO referrals(invitee_id,inviter_id,created_at) VALUES (?,?,?)',(invitee,inviter,NOW()))
async def user_row(user_id:int): return await fetchone('SELECT * FROM users WHERE user_id=?',(user_id,))
async def mark_welcome_sent(user_id:int): await execute('UPDATE users SET welcome_sent_at=? WHERE user_id=?',(NOW(),user_id))
async def user_count_stats():
    total=(await fetchone('SELECT COUNT(*) c FROM users WHERE user_id<>?',(settings.admin_id,)))['c']
    active=(await fetchone('SELECT COUNT(*) c FROM users WHERE broadcasts_enabled=1 AND is_blocked=0 AND user_id<>?',(settings.admin_id,)))['c']
    off=(await fetchone('SELECT COUNT(*) c FROM users WHERE broadcasts_enabled=0 AND is_blocked=0 AND user_id<>?',(settings.admin_id,)))['c']
    blocked=(await fetchone('SELECT COUNT(*) c FROM users WHERE is_blocked=1 AND user_id<>?',(settings.admin_id,)))['c']
    return dict(total=total,active=active,off=off,blocked=blocked)
async def customers(limit=50): return await fetchall('SELECT * FROM users ORDER BY last_seen_at DESC LIMIT ?',(limit,))

# ---------- products ----------
async def _sync_categories_config() -> None:
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute(
            "INSERT OR IGNORE INTO categories_config(name,created_at) SELECT DISTINCT category, ? FROM products WHERE TRIM(category)<>''",
            (NOW(),),
        )
        await db.commit()

async def category_records():
    await _sync_categories_config()
    return await fetchall(
        """SELECT c.*,
                  (SELECT COUNT(*) FROM products p WHERE p.category=c.name) AS product_count,
                  (SELECT COUNT(*) FROM category_media m WHERE m.category_id=c.id) AS media_count
             FROM categories_config c
            WHERE EXISTS(SELECT 1 FROM products p WHERE p.category=c.name)
               OR EXISTS(SELECT 1 FROM category_media m WHERE m.category_id=c.id)
            ORDER BY c.name"""
    )

async def categories(): return [r['name'] for r in await category_records()]

async def category_record(category_id:int):
    await _sync_categories_config()
    return await fetchone(
        """SELECT c.*,
                  (SELECT COUNT(*) FROM products p WHERE p.category=c.name) AS product_count,
                  (SELECT COUNT(*) FROM category_media m WHERE m.category_id=c.id) AS media_count
             FROM categories_config c WHERE c.id=?""",
        (category_id,),
    )

async def category_by_name(name:str):
    await _sync_categories_config()
    return await fetchone(
        """SELECT c.*,
                  (SELECT COUNT(*) FROM products p WHERE p.category=c.name) AS product_count,
                  (SELECT COUNT(*) FROM category_media m WHERE m.category_id=c.id) AS media_count
             FROM categories_config c WHERE c.name=?""",
        (name,),
    )

async def ensure_category(name:str):
    name=(name or '').strip()
    if not name: raise ValueError('Пустое название категории')
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute('INSERT OR IGNORE INTO categories_config(name,created_at) VALUES (?,?)',(name,NOW()))
        await db.commit()
    return await category_by_name(name)

async def rename_category(category_id:int,new_name:str):
    new_name=(new_name or '').strip()
    if not new_name: raise ValueError('Пустое название категории')
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute('SELECT * FROM categories_config WHERE id=?',(category_id,)); row=await cur.fetchone()
        if not row: raise ValueError('Категория не найдена')
        cur=await db.execute('SELECT id FROM categories_config WHERE name=? AND id<>?',(new_name,category_id)); exists=await cur.fetchone()
        if exists: raise ValueError('Категория с таким названием уже существует')
        await db.execute('UPDATE products SET category=? WHERE category=?',(new_name,row['name']))
        await db.execute('UPDATE categories_config SET name=? WHERE id=?',(new_name,category_id))
        await db.commit()
    return await category_record(category_id)

async def category_media(category_id:int):
    return await fetchall('SELECT * FROM category_media WHERE category_id=? ORDER BY sort_order,id',(category_id,))

async def add_category_media(category_id:int,file_id:str,media_type:str='photo'):
    media_type=(media_type or '').strip().lower()
    if media_type not in {'photo','video'}: raise ValueError('Для категории поддерживаются только фото и видео')
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        cur=await db.execute('SELECT COALESCE(MAX(sort_order),-1)+1 FROM category_media WHERE category_id=?',(category_id,)); n=(await cur.fetchone())[0]
        await db.execute('INSERT INTO category_media(category_id,file_id,media_type,sort_order) VALUES (?,?,?,?)',(category_id,file_id,media_type,n))
        await db.commit()

async def delete_category_media(media_id:int):
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute('SELECT * FROM category_media WHERE id=?',(media_id,)); row=await cur.fetchone()
        if not row: return None
        await db.execute('DELETE FROM category_media WHERE id=?',(media_id,)); await db.commit(); return row

async def clear_category_media(category_id:int):
    await execute('DELETE FROM category_media WHERE category_id=?',(category_id,))

async def size_chart_media():
    return await fetchall('SELECT * FROM size_chart_media ORDER BY sort_order,id')

async def add_size_chart_media(file_id:str, media_type:str='photo'):
    media_type=(media_type or '').strip().lower()
    if media_type not in {'photo','video'}:
        raise ValueError('Для размерной сетки поддерживаются только фото и видео')
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        cur=await db.execute('SELECT COALESCE(MAX(sort_order),-1)+1 FROM size_chart_media')
        n=(await cur.fetchone())[0]
        await db.execute('INSERT INTO size_chart_media(file_id,media_type,sort_order) VALUES (?,?,?)',(file_id,media_type,n))
        await db.commit()

async def clear_size_chart_media():
    await execute('DELETE FROM size_chart_media')

async def product(product_id:int): return await fetchone('SELECT * FROM products WHERE id=?',(product_id,))
async def products(where='1=1',params=(),limit=50): return await fetchall(f'SELECT * FROM products WHERE {where} ORDER BY id DESC LIMIT ?',tuple(params)+(limit,))
async def variants(product_id:int): return await fetchall('SELECT * FROM product_variants WHERE product_id=? ORDER BY color,id',(product_id,))
async def variant(variant_id:int): return await fetchone('''SELECT v.*,p.name,p.name_html,p.price,p.old_price,p.category,p.description,p.description_html,p.photo_url,p.weight_grams,p.is_new,p.is_hit,p.availability_status FROM product_variants v JOIN products p ON p.id=v.product_id WHERE v.id=?''',(variant_id,))

async def customer_variants(product_id:int):
    """Variants with physical stock reduced by active unpaid-order reservations."""
    now=NOW()
    return await fetchall(
        '''SELECT v.*,MAX(0,v.stock-COALESCE((
               SELECT SUM(r.qty) FROM inventory_reservations r
               WHERE r.variant_id=v.id AND r.expires_at>?
           ),0)) AS available_stock
           FROM product_variants v WHERE v.product_id=? ORDER BY v.color,v.id''',
        (now,product_id),
    )

async def customer_variant(variant_id:int):
    now=NOW()
    return await fetchone(
        '''SELECT v.*,MAX(0,v.stock-COALESCE((
               SELECT SUM(r.qty) FROM inventory_reservations r
               WHERE r.variant_id=v.id AND r.expires_at>?
           ),0)) AS available_stock,
           p.name,p.name_html,p.price,p.old_price,p.category,p.description,p.description_html,p.photo_url,
           p.weight_grams,p.is_new,p.is_hit,p.availability_status
           FROM product_variants v JOIN products p ON p.id=v.product_id WHERE v.id=?''',
        (now,variant_id),
    )
async def photos(product_id:int,color:str|None=None):
    # Legacy function name kept for compatibility; rows can now be photo/video/GIF/document/audio.
    if color is None:
        return await fetchall('SELECT * FROM product_photos WHERE product_id=? ORDER BY id',(product_id,))
    return await fetchall('SELECT * FROM product_photos WHERE product_id=? AND color=? ORDER BY sort_order,id',(product_id,color))

async def product_media(product_id:int,color:str|None=None):
    return await photos(product_id,color)

async def product_media_item(media_id:int):
    return await fetchone('SELECT * FROM product_photos WHERE id=?',(media_id,))
async def total_stock(product_id:int): return int((await fetchone('SELECT COALESCE(SUM(stock),0) s FROM product_variants WHERE product_id=?',(product_id,)))['s'] or 0)

async def add_product(category,name,price,description,weight,variant_spec,photo_id='',description_html='',name_html='',availability_status='in_stock'):
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute('INSERT OR IGNORE INTO categories_config(name,created_at) VALUES (?,?)',((category or '').strip(),NOW()))
        status=availability_status if availability_status in {'in_stock','preorder'} else 'in_stock'
        cur=await db.execute('INSERT INTO products(category,name,name_html,price,description,description_html,sizes,photo_url,weight_grams,availability_status) VALUES (?,?,?,?,?,?,?,?,?,?)',(category,name,name_html,price,description,description_html,'',photo_id,weight,status))
        pid=cur.lastrowid
        for (color,size),stock in variant_spec.items(): await db.execute('INSERT INTO product_variants(product_id,color,size,stock) VALUES (?,?,?,?)',(pid,color,size,stock))
        if photo_id: await db.execute('INSERT INTO product_photos(product_id,file_id,sort_order) VALUES (?,?,0)',(pid,photo_id))
        await _sync_sizes(db,pid); await db.commit(); return pid

async def update_product(product_id:int,field:str,value):
    allowed={'category','name','name_html','price','description','description_html','photo_url','old_price','is_new','is_hit','weight_grams','availability_status'}
    if field not in allowed: raise ValueError('bad product field')
    if field=='category': await ensure_category(str(value))
    if field=='availability_status' and value not in {'in_stock','preorder'}: raise ValueError('bad availability status')
    await execute(f'UPDATE products SET {field}=? WHERE id=?',(value,product_id))

async def _sync_sizes(db,pid):
    cur=await db.execute('SELECT size,SUM(stock) FROM product_variants WHERE product_id=? GROUP BY size ORDER BY size',(pid,)); rows=await cur.fetchall()
    await db.execute('UPDATE products SET sizes=? WHERE id=?',(sizes_to_text({r[0]:int(r[1] or 0) for r in rows}),pid))

async def set_variants(product_id:int,spec:dict[tuple[str,str],int]):
    """Update variants without invalidating stock held by active orders."""
    restocked=[]
    now=NOW()
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory=aiosqlite.Row
        await db.execute('PRAGMA busy_timeout=30000')
        await db.execute('BEGIN IMMEDIATE')
        try:
            cur=await db.execute('SELECT * FROM product_variants WHERE product_id=?',(product_id,))
            old={(r['color'],r['size']):r for r in await cur.fetchall()}
            for key,row in old.items():
                cur=await db.execute(
                    'SELECT COALESCE(SUM(qty),0) FROM inventory_reservations WHERE variant_id=? AND expires_at>?',
                    (row['id'],now),
                )
                reserved=int((await cur.fetchone())[0] or 0)
                if key not in spec:
                    if reserved:
                        raise ValueError(
                            f"Нельзя удалить {row['color']} / {row['size']}: {reserved} шт. зарезервировано в активных заказах"
                        )
                    await db.execute('DELETE FROM cart_items_v5 WHERE variant_id=?',(row['id'],))
                    await db.execute('DELETE FROM restock_requests WHERE variant_id=?',(row['id'],))
                    await db.execute('DELETE FROM product_variants WHERE id=?',(row['id'],))
                    continue
                requested=max(0,int(spec[key]))
                if requested < reserved:
                    raise ValueError(
                        f"Нельзя поставить остаток {requested} для {row['color']} / {row['size']}: "
                        f"{reserved} шт. уже зарезервировано"
                    )
            for key,stock in spec.items():
                color,size=key; stock=max(0,int(stock)); row=old.get(key)
                if row:
                    if row['stock']<=0<stock: restocked.append(row['id'])
                    await db.execute('UPDATE product_variants SET stock=? WHERE id=?',(stock,row['id']))
                else:
                    cur=await db.execute('INSERT INTO product_variants(product_id,color,size,stock) VALUES (?,?,?,?)',(product_id,color,size,stock))
                    if stock>0: restocked.append(cur.lastrowid)
            await _sync_sizes(db,product_id)
            await db.commit()
            return restocked
        except Exception:
            await db.rollback()
            raise

async def add_media(product_id:int,file_id:str,media_type:str='photo',color:str=''):
    media_type=(media_type or 'document').strip().lower()
    allowed={'photo','video','animation','document','audio'}
    if media_type not in allowed: raise ValueError('Неподдерживаемый тип мультимедиа')
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        cur=await db.execute('SELECT COALESCE(MAX(sort_order),-1)+1 FROM product_photos WHERE product_id=? AND color=?',(product_id,color)); n=(await cur.fetchone())[0]
        await db.execute('INSERT INTO product_photos(product_id,file_id,color,sort_order,media_type) VALUES (?,?,?,?,?)',(product_id,file_id,color,n,media_type))
        if media_type=='photo':
            await db.execute("UPDATE products SET photo_url=CASE WHEN photo_url='' THEN ? ELSE photo_url END WHERE id=?",(file_id,product_id))
        await db.commit()

async def add_photo(product_id:int,file_id:str,color:str=''):
    return await add_media(product_id,file_id,'photo',color)

async def delete_media(media_id:int):
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute('SELECT * FROM product_photos WHERE id=?',(media_id,)); row=await cur.fetchone()
        if not row: return None
        await db.execute('DELETE FROM product_photos WHERE id=?',(media_id,))
        if row['media_type']=='photo':
            cur=await db.execute("SELECT file_id FROM product_photos WHERE product_id=? AND media_type='photo' ORDER BY sort_order,id LIMIT 1",(row['product_id'],)); replacement=await cur.fetchone()
            await db.execute('UPDATE products SET photo_url=? WHERE id=?',((replacement['file_id'] if replacement else ''),row['product_id']))
        await db.commit()
        return row

async def delete_product(product_id:int):
    """Delete a product only when it is not reserved by unpaid orders."""
    now=NOW()
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory=aiosqlite.Row
        await db.execute('PRAGMA busy_timeout=30000')
        await db.execute('BEGIN IMMEDIATE')
        try:
            cur=await db.execute(
                '''SELECT COALESCE(SUM(r.qty),0) qty FROM inventory_reservations r
                   JOIN product_variants v ON v.id=r.variant_id
                   WHERE v.product_id=? AND r.expires_at>?''',
                (product_id,now),
            )
            held=int((await cur.fetchone())['qty'] or 0)
            if held:
                raise ValueError(f'Нельзя удалить товар: {held} шт. зарезервировано в активных заказах')
            await db.execute('DELETE FROM cart_items_v5 WHERE variant_id IN (SELECT id FROM product_variants WHERE product_id=?)',(product_id,))
            await db.execute('DELETE FROM product_photos WHERE product_id=?',(product_id,))
            await db.execute('DELETE FROM restock_requests WHERE variant_id IN (SELECT id FROM product_variants WHERE product_id=?)',(product_id,))
            await db.execute('DELETE FROM product_variants WHERE product_id=?',(product_id,))
            await db.execute('DELETE FROM products WHERE id=?',(product_id,))
            await db.commit()
        except Exception:
            await db.rollback()
            raise

# ---------- cart/restock ----------
async def touch_cart(user_id:int): await execute('UPDATE users SET cart_updated_at=?,cart_reminder_sent_at=NULL WHERE user_id=?',(NOW(),user_id))
async def cart(user_id:int): return await fetchall('''SELECT c.id cart_id,c.qty,c.variant_id,v.size,v.color,v.stock,p.id product_id,p.name,p.price,p.old_price,p.weight_grams FROM cart_items_v5 c JOIN product_variants v ON v.id=c.variant_id JOIN products p ON p.id=v.product_id WHERE c.user_id=? ORDER BY c.id''',(user_id,))
async def add_cart(user_id:int,variant_id:int):
    v=await customer_variant(variant_id)
    available=int(v['available_stock'] or 0) if v else 0
    if not v or available<=0: return False,'Этот вариант закончился или временно зарезервирован'
    row=await fetchone('SELECT qty FROM cart_items_v5 WHERE user_id=? AND variant_id=?',(user_id,variant_id)); qty=row['qty'] if row else 0
    if qty>=available: return False,f"Сейчас доступно только {available} шт."
    await execute('INSERT INTO cart_items_v5(user_id,variant_id,qty) VALUES (?,?,1) ON CONFLICT(user_id,variant_id) DO UPDATE SET qty=qty+1',(user_id,variant_id)); await touch_cart(user_id); return True,'Добавлено'
async def cart_qty(user_id:int,cart_id:int,delta:int):
    row=await fetchone('''SELECT c.qty,c.variant_id,v.stock FROM cart_items_v5 c JOIN product_variants v ON v.id=c.variant_id WHERE c.id=? AND c.user_id=?''',(cart_id,user_id))
    if not row:return False,'Позиция не найдена'
    q=row['qty']+delta
    if q<=0: await execute('DELETE FROM cart_items_v5 WHERE id=? AND user_id=?',(cart_id,user_id)); await touch_cart(user_id); return True,'Удалено'
    available=max(0,int(row['stock'])-await reserved_qty(int(row['variant_id'])))
    if q>available: return False,f"Сейчас доступно только {available} шт."
    await execute('UPDATE cart_items_v5 SET qty=? WHERE id=? AND user_id=?',(q,cart_id,user_id)); await touch_cart(user_id); return True,'Изменено'
async def cart_delete(user_id:int,cart_id:int): await execute('DELETE FROM cart_items_v5 WHERE id=? AND user_id=?',(cart_id,user_id)); await touch_cart(user_id)
async def validate_cart(user_id:int):
    for i in await cart(user_id):
        available=max(0,int(i['stock'])-await reserved_qty(int(i['variant_id'])))
        if i['qty']>available: return False,f"{i['name']} / {i['color']} / {i['size']}: сейчас доступно {available} шт."
    return True,''

async def watch_restock(user_id:int,variant_id:int): await execute('INSERT OR IGNORE INTO restock_requests(user_id,variant_id,created_at) VALUES (?,?,?)',(user_id,variant_id,NOW()))
async def restock_watchers(variant_id:int): return [r['user_id'] for r in await fetchall('SELECT user_id FROM restock_requests WHERE variant_id=?',(variant_id,))]
async def clear_restock_watchers(variant_id:int): await execute('DELETE FROM restock_requests WHERE variant_id=?',(variant_id,))

# ---------- promos / loyalty ----------
async def promo(code:str): return await fetchone('SELECT * FROM promo_codes WHERE UPPER(code)=UPPER(?)',(code,))
async def promo_by_id(pid:int): return await fetchone('SELECT * FROM promo_codes WHERE id=?',(pid,))
async def promos(): return await fetchall('SELECT * FROM promo_codes ORDER BY id DESC')
async def add_promo(code,percent,min_order,max_uses): return await execute('INSERT INTO promo_codes(code,percent,min_order,max_uses,active,created_at) VALUES (?,?,?,?,1,?)',(code.upper(),percent,min_order,max_uses,NOW()))
async def toggle_promo(pid:int): await execute('UPDATE promo_codes SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?',(pid,))
async def promo_usage_count(pid:int): return (await fetchone('SELECT COUNT(*) c FROM promo_usages WHERE promo_id=?',(pid,)))['c']
async def promo_used_by(pid:int,user_id:int): return bool(await fetchone('SELECT 1 FROM promo_usages WHERE promo_id=? AND user_id=?',(pid,user_id)))

async def promo_stats(pid:int):
    p=await promo_by_id(pid)
    if not p: return None
    return await fetchone("""SELECT COUNT(*) purchases,COUNT(DISTINCT user_id) customers,
        COALESCE(SUM(discount_amount),0) discount_sum,COALESCE(SUM(total),0) orders_sum
        FROM orders WHERE UPPER(COALESCE(promo_code,''))=UPPER(?)
        AND status IN ('Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен','Завершён')""",(p['code'],))

async def promo_orders(pid:int,limit=20):
    p=await promo_by_id(pid)
    if not p: return []
    return await fetchall("""SELECT id,user_id,customer_name,username,total,discount_amount,confirmed_at,created_at
        FROM orders WHERE UPPER(COALESCE(promo_code,''))=UPPER(?)
        AND status IN ('Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен','Завершён')
        ORDER BY COALESCE(confirmed_at,created_at) DESC,id DESC LIMIT ?""",(p['code'],limit))

async def lifetime_spend(user_id:int): return int((await fetchone("SELECT COALESCE(SUM(total),0) s FROM orders WHERE user_id=? AND status IN ('Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен','Завершён')",(user_id,)))['s'] or 0)
async def bonus_balance(user_id:int):
    r=await user_row(user_id); return int(r['bonus_balance'] or 0) if r else 0

async def bonus_customers(limit=50):
    return await fetchall('SELECT * FROM users WHERE user_id<>? ORDER BY bonus_balance DESC,last_seen_at DESC LIMIT ?',(settings.admin_id,limit))

async def adjust_bonus(user_id:int,delta:int):
    delta=int(delta)
    if delta==0: raise ValueError('Изменение бонусов не может быть равно нулю')
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute('SELECT bonus_balance FROM users WHERE user_id=?',(user_id,)); row=await cur.fetchone()
        if not row: raise ValueError('Покупатель не найден')
        current=int(row['bonus_balance'] or 0)
        new_balance=current+delta
        if new_balance<0: raise ValueError(f'Недостаточно бонусов. Текущий баланс: {current}')
        await db.execute('UPDATE users SET bonus_balance=? WHERE user_id=?',(new_balance,user_id))
        await db.commit()
    return new_balance

async def add_bonus(user_id:int,amount:int):
    amount=int(amount)
    if amount<=0: raise ValueError('Количество бонусов должно быть больше нуля')
    return await adjust_bonus(user_id,amount)

async def subtract_bonus(user_id:int,amount:int):
    amount=int(amount)
    if amount<=0: raise ValueError('Количество бонусов должно быть больше нуля')
    return await adjust_bonus(user_id,-amount)

# ---------- saved delivery profiles ----------
DELIVERY_PROFILE_FIELDS = (
    'recipient_full_name','phone','delivery_method','postal_code','region','city',
    'street','house','building','apartment','cdek_type','cdek_point','delivery_comment'
)

async def delivery_profiles(user_id:int, limit=25):
    return await fetchall(
        'SELECT * FROM delivery_profiles WHERE user_id=? ORDER BY last_used_at DESC,id DESC LIMIT ?',
        (user_id,limit),
    )

async def delivery_profile(user_id:int, profile_id:int):
    return await fetchone(
        'SELECT * FROM delivery_profiles WHERE id=? AND user_id=?',
        (profile_id,user_id),
    )

async def touch_delivery_profile(user_id:int, profile_id:int):
    await execute(
        'UPDATE delivery_profiles SET last_used_at=? WHERE id=? AND user_id=?',
        (NOW(),profile_id,user_id),
    )

async def save_delivery_profile(user_id:int, data:dict):
    values={field:str(data.get(field,'') or '').strip() for field in DELIVERY_PROFILE_FIELDS}
    existing=await delivery_profiles(user_id,100)
    for row in existing:
        if all(str(row[field] or '').strip()==values[field] for field in DELIVERY_PROFILE_FIELDS):
            await touch_delivery_profile(user_id,row['id'])
            return int(row['id'])

    now=NOW()
    cols=','.join(DELIVERY_PROFILE_FIELDS)
    placeholders=','.join('?' for _ in DELIVERY_PROFILE_FIELDS)
    params=[user_id]+[values[field] for field in DELIVERY_PROFILE_FIELDS]+[now,now]
    return await execute(
        f'INSERT INTO delivery_profiles(user_id,{cols},created_at,last_used_at) '
        f'VALUES (? ,{placeholders},?,?)',
        tuple(params),
    )

# ---------- orders ----------
async def release_expired_reservations() -> int:
    """Drop expired reservations without touching physical stock."""
    now = NOW()
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute('PRAGMA busy_timeout=30000')
        cur = await db.execute('SELECT COUNT(*) FROM inventory_reservations WHERE expires_at<=?', (now,))
        count = int((await cur.fetchone())[0] or 0)
        await db.execute('DELETE FROM inventory_reservations WHERE expires_at<=?', (now,))
        await db.execute(
            "UPDATE orders SET reservation_expires_at=NULL WHERE reservation_expires_at IS NOT NULL AND reservation_expires_at<=?",
            (now,),
        )
        await db.commit()
        return count


async def reserved_qty(variant_id: int, *, exclude_order_id: int | None = None) -> int:
    now = NOW()
    if exclude_order_id:
        row = await fetchone(
            'SELECT COALESCE(SUM(qty),0) q FROM inventory_reservations WHERE variant_id=? AND expires_at>? AND order_id<>?',
            (variant_id, now, exclude_order_id),
        )
    else:
        row = await fetchone(
            'SELECT COALESCE(SUM(qty),0) q FROM inventory_reservations WHERE variant_id=? AND expires_at>?',
            (variant_id, now),
        )
    return int(row['q'] or 0)


async def create_order(user_id, username, customer_name, delivery: dict, pricing: dict):
    """Atomically create an order and reserve its stock for payment."""
    now = NOW()
    expires = (datetime.now() + timedelta(minutes=max(1, settings.reservation_minutes))).strftime('%Y-%m-%d %H:%M:%S')
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('PRAGMA busy_timeout=30000')
        await db.execute('BEGIN IMMEDIATE')
        try:
            await db.execute('DELETE FROM inventory_reservations WHERE expires_at<=?', (now,))
            cur = await db.execute(
                """SELECT c.id cart_id,c.qty,c.variant_id,v.size,v.color,v.stock,
                          p.id product_id,p.name,p.price,p.old_price,p.weight_grams
                     FROM cart_items_v5 c
                     JOIN product_variants v ON v.id=c.variant_id
                     JOIN products p ON p.id=v.product_id
                    WHERE c.user_id=? ORDER BY c.id""",
                (user_id,),
            )
            items = await cur.fetchall()
            if not items:
                await db.rollback()
                return None

            current_subtotal = sum(int(i['price']) * int(i['qty']) for i in items)
            if int(pricing.get('subtotal', current_subtotal)) != current_subtotal:
                await db.rollback()
                raise ValueError('Корзина или цены изменились. Откройте корзину и подтвердите заказ заново.')

            for item in items:
                cur = await db.execute(
                    'SELECT COALESCE(SUM(qty),0) FROM inventory_reservations WHERE variant_id=? AND expires_at>?',
                    (item['variant_id'], now),
                )
                reserved = int((await cur.fetchone())[0] or 0)
                available = max(0, int(item['stock']) - reserved)
                if int(item['qty']) > available:
                    await db.rollback()
                    raise ValueError(
                        f"{item['name']} / {item['color']} / {item['size']}: доступно только {available} шт. "
                        "(часть товара уже зарезервирована)"
                    )

            cur = await db.execute(
                """INSERT INTO orders(
                    user_id,username,customer_name,recipient_full_name,phone,delivery_method,postal_code,region,city,
                    street,house,building,apartment,cdek_type,cdek_point,delivery_comment,address,subtotal,
                    discount_amount,loyalty_discount,bonus_used,shipping_cost,promo_code,total,status,created_at,
                    reservation_expires_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'Ожидает оплаты',?,?)""",
                (
                    user_id, username, customer_name, delivery.get('recipient_full_name',''), delivery.get('phone',''),
                    delivery.get('delivery_method',''), delivery.get('postal_code',''), delivery.get('region',''),
                    delivery.get('city',''), delivery.get('street',''), delivery.get('house',''), delivery.get('building',''),
                    delivery.get('apartment',''), delivery.get('cdek_type',''), delivery.get('cdek_point',''),
                    delivery.get('delivery_comment',''), delivery.get('address',''), pricing['subtotal'],
                    pricing['promo_discount'], pricing['loyalty_discount'], pricing['points_used'], pricing['shipping_cost'],
                    pricing.get('promo_code',''), pricing['total'], now, expires,
                ),
            )
            oid = int(cur.lastrowid)
            public_code = await _new_public_order_code(db)
            await db.execute('UPDATE orders SET public_code=? WHERE id=?', (public_code, oid))
            for i in items:
                await db.execute(
                    'INSERT INTO order_items(order_id,product_id,product_name,size,color,variant_id,qty,price) VALUES (?,?,?,?,?,?,?,?)',
                    (oid,i['product_id'],i['name'],i['size'],i['color'],i['variant_id'],i['qty'],i['price']),
                )
                await db.execute(
                    'INSERT INTO inventory_reservations(order_id,variant_id,qty,expires_at,created_at) VALUES (?,?,?,?,?)',
                    (oid,i['variant_id'],i['qty'],expires,now),
                )
            await db.execute('DELETE FROM cart_items_v5 WHERE user_id=?', (user_id,))
            await db.execute('UPDATE users SET cart_updated_at=NULL,cart_reminder_sent_at=NULL WHERE user_id=?', (user_id,))
            await db.execute(
                'INSERT INTO order_status_history(order_id,status,actor_id,created_at) VALUES (?,?,?,?)',
                (oid,'Ожидает оплаты',user_id,now),
            )
            await db.commit()
            return oid
        except Exception:
            try:
                await db.rollback()
            except Exception:
                log.debug("SQLite rollback failed", exc_info=True)
            raise


async def ensure_order_reservation(order_id: int, *, hours: int | None = None, minutes: int | None = None) -> str:
    """Create or renew an unpaid order reservation atomically.

    If the old hold expired, this re-checks real stock against reservations from
    other orders before recreating the hold. It prevents accepting a manual
    receipt or starting checkout for stock that another customer already took.
    Returns the new expiry timestamp.
    """
    if hours is not None:
        expires_dt = datetime.now() + timedelta(hours=max(1, hours))
    else:
        expires_dt = datetime.now() + timedelta(minutes=max(1, minutes or settings.reservation_minutes))
    expires = expires_dt.strftime('%Y-%m-%d %H:%M:%S')
    now = NOW()
    paid_statuses = ('Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен','Завершён')
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('PRAGMA busy_timeout=30000')
        await db.execute('BEGIN IMMEDIATE')
        try:
            cur = await db.execute('SELECT * FROM orders WHERE id=?', (order_id,))
            order_row = await cur.fetchone()
            if not order_row:
                await db.rollback()
                raise ValueError('Заказ не найден')
            if order_row['status'] in paid_statuses:
                await db.rollback()
                return str(order_row['reservation_expires_at'] or '')

            await db.execute('DELETE FROM inventory_reservations WHERE expires_at<=?', (now,))
            cur = await db.execute('SELECT * FROM order_items WHERE order_id=? ORDER BY id', (order_id,))
            items = await cur.fetchall()
            if not items:
                await db.rollback()
                raise ValueError('В заказе нет товаров')

            resolved: list[tuple[int, int, str]] = []
            for item in items:
                variant_id = int(item['variant_id'] or 0)
                if not variant_id:
                    cur = await db.execute(
                        'SELECT id FROM product_variants WHERE product_id=? AND size=? '
                        'ORDER BY CASE WHEN color=? THEN 0 ELSE 1 END,id LIMIT 1',
                        (item['product_id'], item['size'], item['color'] or 'Основной'),
                    )
                    row = await cur.fetchone()
                    if not row:
                        await db.rollback()
                        raise ValueError(f"Вариант {item['product_name']} больше недоступен")
                    variant_id = int(row['id'])
                cur = await db.execute('SELECT * FROM product_variants WHERE id=?', (variant_id,))
                variant = await cur.fetchone()
                if not variant:
                    await db.rollback()
                    raise ValueError(f"Вариант {item['product_name']} больше недоступен")
                cur = await db.execute(
                    'SELECT COALESCE(SUM(qty),0) FROM inventory_reservations '
                    'WHERE variant_id=? AND expires_at>? AND order_id<>?',
                    (variant_id, now, order_id),
                )
                other_reserved = int((await cur.fetchone())[0] or 0)
                available = max(0, int(variant['stock']) - other_reserved)
                qty = int(item['qty'])
                if qty > available:
                    await db.rollback()
                    raise ValueError(
                        f"{item['product_name']} / {variant['color']} / {variant['size']}: "
                        f"доступно только {available} шт."
                    )
                resolved.append((variant_id, qty, str(item['product_name'])))

            # Rebuild this order's hold as a single atomic operation.
            await db.execute('DELETE FROM inventory_reservations WHERE order_id=?', (order_id,))
            for variant_id, qty, _ in resolved:
                await db.execute(
                    'INSERT INTO inventory_reservations(order_id,variant_id,qty,expires_at,created_at) VALUES (?,?,?,?,?)',
                    (order_id, variant_id, qty, expires, now),
                )
            await db.execute('UPDATE orders SET reservation_expires_at=? WHERE id=?', (expires, order_id))
            await db.commit()
            return expires
        except Exception:
            try:
                await db.rollback()
            except Exception:
                log.debug("SQLite rollback failed while ensuring reservation", exc_info=True)
            raise


async def extend_order_reservation(order_id: int, *, hours: int | None = None, minutes: int | None = None) -> None:
    # Compatibility wrapper: unlike the old implementation it safely reacquires
    # an expired hold instead of merely updating a timestamp that may no longer exist.
    await ensure_order_reservation(order_id, hours=hours, minutes=minutes)


async def consume_reservation_and_confirm(order_id: int, method: str, actor_id: int | None) -> tuple[bool, str, bool]:
    """Atomically consume stock once and confirm payment.

    Returns (ok, error, already_confirmed). Repeated payment callbacks are safe.
    """
    paid_statuses = ('Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен','Завершён')
    now = NOW()
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('PRAGMA busy_timeout=30000')
        await db.execute('BEGIN IMMEDIATE')
        try:
            cur = await db.execute('SELECT * FROM orders WHERE id=?', (order_id,))
            order_row = await cur.fetchone()
            if not order_row:
                await db.rollback()
                return False, 'Заказ не найден', False
            if order_row['status'] in paid_statuses:
                await db.rollback()
                return True, '', True

            await db.execute('DELETE FROM inventory_reservations WHERE expires_at<=?', (now,))
            cur = await db.execute('SELECT * FROM order_items WHERE order_id=? ORDER BY id', (order_id,))
            items = await cur.fetchall()
            changed_products: set[int] = set()
            for item in items:
                variant_id = int(item['variant_id'] or 0)
                if not variant_id:
                    cur = await db.execute(
                        'SELECT id FROM product_variants WHERE product_id=? AND size=? '
                        'ORDER BY CASE WHEN color=? THEN 0 ELSE 1 END,id LIMIT 1',
                        (item['product_id'], item['size'], item['color'] or 'Основной'),
                    )
                    vr = await cur.fetchone()
                    if not vr:
                        await db.rollback()
                        return False, f"Вариант {item['product_name']} удалён", False
                    variant_id = int(vr['id'])
                cur = await db.execute('SELECT * FROM product_variants WHERE id=?', (variant_id,))
                v = await cur.fetchone()
                if not v:
                    await db.rollback()
                    return False, f"Вариант {item['product_name']} удалён", False
                cur = await db.execute(
                    'SELECT COALESCE(SUM(qty),0) FROM inventory_reservations '
                    'WHERE variant_id=? AND expires_at>? AND order_id<>?',
                    (variant_id, now, order_id),
                )
                other_reserved = int((await cur.fetchone())[0] or 0)
                available = int(v['stock']) - other_reserved
                if int(item['qty']) > available:
                    await db.rollback()
                    return False, (
                        f"Недостаточно {item['product_name']} / {v['color']} / {v['size']}: "
                        f"доступно {max(0, available)} шт."
                    ), False
                await db.execute('UPDATE product_variants SET stock=stock-? WHERE id=?', (int(item['qty']), variant_id))
                changed_products.add(int(v['product_id']))

            await db.execute('DELETE FROM inventory_reservations WHERE order_id=?', (order_id,))
            for pid in changed_products:
                await _sync_sizes(db, pid)
            await db.execute(
                "UPDATE orders SET status='Подтверждён',confirmed_at=COALESCE(confirmed_at,?),"
                "payment_method=CASE WHEN ?<>'' THEN ? ELSE payment_method END,reservation_expires_at=NULL WHERE id=?",
                (now, method or '', method or '', order_id),
            )
            await db.execute(
                'INSERT INTO order_status_history(order_id,status,actor_id,created_at) VALUES (?,?,?,?)',
                (order_id,'Подтверждён',actor_id,now),
            )
            await db.commit()
            return True, '', False
        except Exception:
            try:
                await db.rollback()
            except Exception:
                log.debug("SQLite rollback failed", exc_info=True)
            raise


async def order(order_id:int): return await fetchone('SELECT * FROM orders WHERE id=?',(order_id,))
async def order_by_public_code(public_code:str):
    code=(public_code or '').strip().lstrip('#').upper()
    return await fetchone('SELECT * FROM orders WHERE UPPER(public_code)=?',(code,)) if code else None
async def order_items(order_id:int): return await fetchall('SELECT * FROM order_items WHERE order_id=? ORDER BY id',(order_id,))
async def user_orders(user_id:int,limit=10): return await fetchall('SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?',(user_id,limit))
async def claim_order_notification(order_id:int, kind:str) -> bool:
    column={'payment':'payment_notice_sent_at','low_stock':'low_stock_alert_sent_at'}.get(kind)
    if not column: raise ValueError('unknown notification kind')
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute('PRAGMA busy_timeout=30000')
        cur=await db.execute(f"UPDATE orders SET {column}=? WHERE id=? AND {column} IS NULL",(NOW(),order_id))
        await db.commit()
        return int(cur.rowcount or 0)>0

async def reset_order_notification(order_id:int, kind:str) -> None:
    column={'payment':'payment_notice_sent_at','low_stock':'low_stock_alert_sent_at'}.get(kind)
    if not column: return
    await execute(f"UPDATE orders SET {column}=NULL WHERE id=?",(order_id,))

async def admin_orders(status:Optional[str]=None,limit=50):
    if status:return await fetchall('SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT ?',(status,limit))
    return await fetchall('SELECT * FROM orders ORDER BY id DESC LIMIT ?',(limit,))
async def set_order_status(order_id:int,status:str,actor_id:Optional[int]=None):
    extra=''; params=[status]
    if status=='Подтверждён': extra=',confirmed_at=?'; params.append(NOW())
    if status in ('Получен','Завершён'): extra+=',delivered_at=COALESCE(delivered_at,?)'; params.append(NOW())
    params.append(order_id)
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute(f'UPDATE orders SET status=?{extra} WHERE id=?',tuple(params)); await db.execute('INSERT INTO order_status_history(order_id,status,actor_id,created_at) VALUES (?,?,?,?)',(order_id,status,actor_id,NOW())); await db.commit()
async def set_tracking(order_id:int,track:str,actor_id:int):
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        await db.execute("UPDATE orders SET tracking_number=?,tracking_sent_at=?,status='Отправлен' WHERE id=?",(track,NOW(),order_id)); await db.execute("INSERT INTO order_status_history(order_id,status,actor_id,created_at) VALUES (?,'Отправлен',?,?)",(order_id,actor_id,NOW())); await db.commit()
async def set_order_note(order_id:int,note:str): await execute('UPDATE orders SET admin_note=? WHERE id=?',(note,order_id))
async def save_receipt(order_id:int,file_id:str,kind:str):
    # Reacquire/extend stock first. If stock was taken after an expired hold,
    # do not accept a receipt that the store can no longer fulfil.
    await ensure_order_reservation(order_id, hours=settings.reservation_receipt_hours)
    await execute("UPDATE orders SET receipt_file_id=?,receipt_type=?,status='На проверке оплаты',paid_at=? WHERE id=?",(file_id,kind,NOW(),order_id))
async def save_online_payment(order_id,tg_id,provider_id): await execute("UPDATE orders SET payment_method='telegram',telegram_payment_charge_id=?,provider_payment_charge_id=?,paid_at=? WHERE id=?",(tg_id,provider_id,NOW(),order_id))

async def decrement_stock(order_id:int):
    items=await order_items(order_id)
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory=aiosqlite.Row; await db.execute('BEGIN IMMEDIATE'); products_changed=set()
        for item in items:
            v=None
            if item['variant_id']:
                c=await db.execute('SELECT * FROM product_variants WHERE id=?',(item['variant_id'],)); v=await c.fetchone()
            if not v:
                c=await db.execute('SELECT * FROM product_variants WHERE product_id=? AND size=? ORDER BY CASE WHEN color=? THEN 0 ELSE 1 END,id LIMIT 1',(item['product_id'],item['size'],item['color'] or 'Основной')); v=await c.fetchone()
            if not v: await db.rollback(); return False,f"Вариант {item['product_name']} удалён"
            if v['stock']<item['qty']: await db.rollback(); return False,f"Недостаточно {item['product_name']} / {v['color']} / {v['size']}: {v['stock']} шт."
            await db.execute('UPDATE product_variants SET stock=? WHERE id=?',(v['stock']-item['qty'],v['id'])); products_changed.add(v['product_id'])
        for pid in products_changed: await _sync_sizes(db,pid)
        await db.commit()
    return True,''

async def apply_paid_benefits(order_row):
    """Apply loyalty/promo side effects at most once per order."""
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('PRAGMA busy_timeout=30000')
        await db.execute('BEGIN IMMEDIATE')
        cur = await db.execute('SELECT * FROM orders WHERE id=?', (order_row['id'],))
        current = await cur.fetchone()
        if not current:
            await db.rollback()
            return {'cashback':0,'balance':0,'already':True}
        if current['benefits_applied_at']:
            cur = await db.execute('SELECT bonus_balance FROM users WHERE user_id=?',(current['user_id'],))
            row = await cur.fetchone()
            balance = int(row[0] or 0) if row else 0
            await db.rollback()
            return {'cashback':0,'balance':balance,'already':True}
        if current['bonus_used']:
            await db.execute(
                'UPDATE users SET bonus_balance=MAX(0,bonus_balance-?) WHERE user_id=?',
                (current['bonus_used'],current['user_id']),
            )
        cashback=max(0, int(current['total'] or 0) * int(settings.loyalty_cashback_percent) // 100)
        if cashback:
            await db.execute('UPDATE users SET bonus_balance=bonus_balance+? WHERE user_id=?',(cashback,current['user_id']))
        if current['promo_code']:
            c=await db.execute('SELECT id FROM promo_codes WHERE UPPER(code)=UPPER(?)',(current['promo_code'],))
            p=await c.fetchone()
            if p:
                await db.execute(
                    'INSERT OR IGNORE INTO promo_usages(promo_id,user_id,order_id,used_at) VALUES (?,?,?,?)',
                    (p[0],current['user_id'],current['id'],NOW()),
                )
        await db.execute(
            'UPDATE orders SET benefits_applied_at=? WHERE id=? AND benefits_applied_at IS NULL',
            (NOW(),current['id']),
        )
        c=await db.execute('SELECT bonus_balance FROM users WHERE user_id=?',(current['user_id'],))
        row=await c.fetchone()
        balance=int(row[0] or 0) if row else 0
        await db.commit()
    return {'cashback':cashback,'balance':balance,'already':False}


async def reward_referral(invitee:int,order_id:int):
    """Reward the inviter at most once, even under concurrent payment callbacks."""
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory=aiosqlite.Row
        await db.execute('PRAGMA busy_timeout=30000')
        await db.execute('BEGIN IMMEDIATE')
        try:
            c=await db.execute('SELECT * FROM referrals WHERE invitee_id=? AND rewarded=0',(invitee,))
            r=await c.fetchone()
            if not r:
                await db.rollback()
                return None
            c=await db.execute(
                "SELECT COUNT(*) FROM orders WHERE user_id=? AND id<>? AND status IN "
                "('Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен','Завершён')",
                (invitee,order_id),
            )
            if int((await c.fetchone())[0] or 0)>0:
                await db.execute('UPDATE referrals SET rewarded=1 WHERE invitee_id=? AND rewarded=0',(invitee,))
                await db.commit()
                return None
            # BEGIN IMMEDIATE serializes competing confirmations; the guarded update
            # documents the exactly-once transition before the bonus is committed.
            cur=await db.execute('UPDATE referrals SET rewarded=1 WHERE invitee_id=? AND rewarded=0',(invitee,))
            if int(cur.rowcount or 0)!=1:
                await db.rollback()
                return None
            await db.execute('UPDATE users SET bonus_balance=bonus_balance+? WHERE user_id=?',(settings.referral_bonus_points,r['inviter_id']))
            await db.commit()
            return r['inviter_id']
        except Exception:
            try:
                await db.rollback()
            except Exception:
                log.debug("SQLite rollback failed while rewarding referral", exc_info=True)
            raise

# ---------- reviews ----------
async def add_review(order_id,user_id,rating,text): return await execute("INSERT INTO reviews(order_id,user_id,rating,text,status,created_at) VALUES (?,?,?,?, 'pending',?)",(order_id,user_id,rating,text,NOW()))
async def reviews(status=None,limit=30):
    if status:return await fetchall('SELECT * FROM reviews WHERE status=? ORDER BY id DESC LIMIT ?',(status,limit))
    return await fetchall('SELECT * FROM reviews ORDER BY id DESC LIMIT ?',(limit,))
async def set_review_status(rid,status): await execute('UPDATE reviews SET status=? WHERE id=?',(status,rid))
async def review_for_order(order_id): return await fetchone('SELECT * FROM reviews WHERE order_id=?',(order_id,))

# ---------- privacy ----------
async def request_privacy(user_id):
    if not await fetchone("SELECT 1 FROM privacy_requests WHERE user_id=? AND status='pending'",(user_id,)): await execute("INSERT INTO privacy_requests(user_id,status,created_at) VALUES (?,'pending',?)",(user_id,NOW()))
async def privacy_requests(): return await fetchall("SELECT * FROM privacy_requests WHERE status='pending' ORDER BY id")
async def privacy_request(rid): return await fetchone('SELECT * FROM privacy_requests WHERE id=?',(rid,))
async def privacy_status(rid,status): await execute('UPDATE privacy_requests SET status=?,processed_at=? WHERE id=?',(status,NOW(),rid))
async def anonymize_user(user_id):
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        for sql in ['DELETE FROM restock_requests WHERE user_id=?','DELETE FROM cart_items_v5 WHERE user_id=?','DELETE FROM legal_acceptances WHERE user_id=?','DELETE FROM reviews WHERE user_id=?','DELETE FROM delivery_profiles WHERE user_id=?']:
            await db.execute(sql,(user_id,))
        await db.execute('DELETE FROM referrals WHERE invitee_id=? OR inviter_id=?',(user_id,user_id))
        await db.execute("UPDATE orders SET user_id=0,username='',customer_name='Удалено',recipient_full_name='Удалено',phone='',address='',postal_code='',region='',city='',street='',house='',building='',apartment='',cdek_point='',delivery_comment='' WHERE user_id=?",(user_id,)); await db.execute('DELETE FROM users WHERE user_id=?',(user_id,)); await db.commit()

# ---------- owner cleanup tools ----------
_CLEANUP_PAID_STATUSES = ('Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен','Завершён')
_CLEANUP_COMPLETED_STATUSES = ('Получен','Завершён')
_CLEANUP_UNPAID_STATUSES = ('Ожидает оплаты','Чек отклонён')

async def cleanup_summary() -> dict[str, int]:
    """Counts shown in the owner-only cleanup panel."""
    paid = await fetchone(
        "SELECT COUNT(*) c FROM orders WHERE status IN (?,?,?,?,?,?,?)",
        _CLEANUP_PAID_STATUSES,
    )
    completed = await fetchone(
        "SELECT COUNT(*) c FROM orders WHERE status IN (?,?)",
        _CLEANUP_COMPLETED_STATUSES,
    )
    unpaid = await fetchone(
        "SELECT COUNT(*) c FROM orders WHERE status IN (?,?)",
        _CLEANUP_UNPAID_STATUSES,
    )
    profiles = await fetchone("SELECT COUNT(*) c FROM delivery_profiles")
    customers = await fetchone(
        "SELECT COUNT(*) c FROM users WHERE user_id NOT IN (SELECT user_id FROM admin_users)"
    )
    activity = await fetchone(
        "SELECT "
        "(SELECT COUNT(*) FROM cart)+(SELECT COUNT(*) FROM cart_items_v5)+"
        "(SELECT COUNT(*) FROM restock_requests) c"
    )
    broadcasts = await fetchone("SELECT COUNT(*) c FROM broadcast_logs")
    audits = await fetchone("SELECT COUNT(*) c FROM audit_logs")
    return {
        'paid_orders': int(paid['c'] or 0),
        'completed_orders': int(completed['c'] or 0),
        'unpaid_orders': int(unpaid['c'] or 0),
        'delivery_profiles': int(profiles['c'] or 0),
        'customer_profiles': int(customers['c'] or 0),
        'customer_activity': int(activity['c'] or 0),
        'broadcast_logs': int(broadcasts['c'] or 0),
        'audit_logs': int(audits['c'] or 0),
    }


async def _delete_orders_by_statuses(statuses: tuple[str, ...]) -> int:
    if not statuses:
        return 0
    placeholders = ','.join('?' for _ in statuses)
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN IMMEDIATE')
        cur = await db.execute(
            f"SELECT id FROM orders WHERE status IN ({placeholders})",
            statuses,
        )
        ids = [int(r['id']) for r in await cur.fetchall()]
        if not ids:
            await db.commit()
            return 0
        id_marks = ','.join('?' for _ in ids)
        # Delete all records that are meaningful only while the order exists.
        await db.execute(f"DELETE FROM inventory_reservations WHERE order_id IN ({id_marks})", ids)
        await db.execute(f"DELETE FROM reviews WHERE order_id IN ({id_marks})", ids)
        await db.execute(f"DELETE FROM promo_usages WHERE order_id IN ({id_marks})", ids)
        await db.execute(f"DELETE FROM order_status_history WHERE order_id IN ({id_marks})", ids)
        await db.execute(f"DELETE FROM order_items WHERE order_id IN ({id_marks})", ids)
        await db.execute(f"DELETE FROM orders WHERE id IN ({id_marks})", ids)
        await db.commit()
        return len(ids)


async def cleanup_completed_orders() -> int:
    return await _delete_orders_by_statuses(_CLEANUP_COMPLETED_STATUSES)


async def cleanup_paid_orders() -> int:
    return await _delete_orders_by_statuses(_CLEANUP_PAID_STATUSES)


async def cleanup_unpaid_orders() -> int:
    # Deliberately excludes "На проверке оплаты" so an uploaded receipt is never
    # destroyed by routine cleanup before an administrator checks it.
    return await _delete_orders_by_statuses(_CLEANUP_UNPAID_STATUSES)


async def cleanup_delivery_profiles() -> int:
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        cur = await db.execute("SELECT COUNT(*) FROM delivery_profiles")
        count = int((await cur.fetchone())[0] or 0)
        await db.execute("DELETE FROM delivery_profiles")
        await db.commit()
        return count


async def cleanup_customer_activity() -> int:
    """Clear transient shopping data without deleting customer accounts/orders."""
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        count = 0
        for table in ('cart', 'cart_items_v5', 'restock_requests'):
            cur = await db.execute(f"SELECT COUNT(*) FROM {table}")
            count += int((await cur.fetchone())[0] or 0)
            await db.execute(f"DELETE FROM {table}")
        await db.execute("UPDATE users SET cart_updated_at=NULL,cart_reminder_sent_at=NULL")
        await db.commit()
        return count


async def cleanup_customer_profiles() -> int:
    """Remove non-admin customer profiles and anonymize retained order/review history."""
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN IMMEDIATE')
        cur = await db.execute(
            "SELECT user_id FROM users WHERE user_id NOT IN (SELECT user_id FROM admin_users)"
        )
        ids = [int(r['user_id']) for r in await cur.fetchall()]
        if not ids:
            await db.commit()
            return 0
        marks = ','.join('?' for _ in ids)

        # Remove personal/user-specific state.
        for table in ('cart', 'cart_items_v5', 'restock_requests',
                      'legal_acceptances', 'delivery_profiles', 'privacy_requests'):
            await db.execute(f"DELETE FROM {table} WHERE user_id IN ({marks})", ids)
        await db.execute(
            f"DELETE FROM referrals WHERE invitee_id IN ({marks}) OR inviter_id IN ({marks})",
            tuple(ids) + tuple(ids),
        )

        # Keep business history but detach/anonymize personal data.
        await db.execute(
            f"UPDATE reviews SET user_id=0 WHERE user_id IN ({marks})",
            ids,
        )
        await db.execute(
            f"UPDATE promo_usages SET user_id=0 WHERE user_id IN ({marks})",
            ids,
        )
        await db.execute(
            f"""UPDATE orders SET
                user_id=0,username='',customer_name='Удалено',recipient_full_name='Удалено',
                phone='',address='',postal_code='',region='',city='',street='',house='',
                building='',apartment='',cdek_point='',delivery_comment=''
                WHERE user_id IN ({marks})""",
            ids,
        )
        await db.execute(f"DELETE FROM users WHERE user_id IN ({marks})", ids)
        await db.commit()
        return len(ids)


async def cleanup_broadcast_logs() -> int:
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        cur = await db.execute("SELECT COUNT(*) FROM broadcast_logs")
        count = int((await cur.fetchone())[0] or 0)
        await db.execute("DELETE FROM broadcast_logs")
        await db.commit()
        return count


async def cleanup_audit_logs() -> int:
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        cur = await db.execute("SELECT COUNT(*) FROM audit_logs")
        count = int((await cur.fetchone())[0] or 0)
        await db.execute("DELETE FROM audit_logs")
        await db.commit()
        return count


# --- single item cleanup ---
async def admin_cleanup_list(kind: str, limit: int = 10):
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory=aiosqlite.Row
        if kind=="orders":
            q="SELECT id, user_id, username, status, total FROM orders ORDER BY id DESC LIMIT ?"
        elif kind=="users":
            q="SELECT user_id, username, full_name FROM users ORDER BY user_id DESC LIMIT ?"
        elif kind=="delivery":
            q="SELECT id, user_id, city, delivery_method FROM delivery_profiles ORDER BY id DESC LIMIT ?"
        elif kind=="broadcast":
            q="SELECT id, created_at, total FROM broadcast_logs ORDER BY id DESC LIMIT ?"
        elif kind=="audit":
            q="SELECT id, action, details FROM audit_logs ORDER BY id DESC LIMIT ?"
        else:
            return []
        cur=await db.execute(q,(limit,))
        return await cur.fetchall()

async def admin_delete_one(kind:str, item_id:int):
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        if kind=="orders":
            await db.execute("DELETE FROM inventory_reservations WHERE order_id=?",(item_id,))
            await db.execute("DELETE FROM order_items WHERE order_id=?",(item_id,))
            await db.execute("DELETE FROM order_status_history WHERE order_id=?",(item_id,))
            await db.execute("DELETE FROM orders WHERE id=?",(item_id,))
        elif kind=="users":
            for t,c in [("cart","user_id"),("cart_items_v5","user_id"),("delivery_profiles","user_id"),("legal_acceptances","user_id"),("users","user_id")]:
                await db.execute(f"DELETE FROM {t} WHERE {c}=?",(item_id,))
        elif kind=="delivery":
            await db.execute("DELETE FROM delivery_profiles WHERE id=?",(item_id,))
        elif kind=="broadcast":
            await db.execute("DELETE FROM broadcast_logs WHERE id=?",(item_id,))
        elif kind=="audit":
            await db.execute("DELETE FROM audit_logs WHERE id=?",(item_id,))
        await db.commit()


# ---------- customizable UI buttons ----------
async def ui_button_customizations():
    return await fetchall(
        "SELECT button_key,custom_text,custom_emoji_id,custom_style FROM ui_button_labels "
        "WHERE TRIM(custom_text)<>'' OR TRIM(custom_emoji_id)<>'' OR TRIM(custom_style)<>''"
    )

async def sync_ui_button_definitions(definitions) -> None:
    if not definitions:
        return
    async with aiosqlite.connect(settings.db_path, timeout=30) as conn:
        await conn.executemany(
            """INSERT INTO ui_button_labels(button_key,default_text,custom_text,custom_emoji_id,kind,group_name,updated_at)
               VALUES (?,?, '', '', ?, ?, ?)
               ON CONFLICT(button_key) DO UPDATE SET
                 default_text=excluded.default_text,
                 kind=excluded.kind,
                 group_name=excluded.group_name""",
            [(d.button_key,d.default_text,d.kind,d.group_name,NOW()) for d in definitions],
        )
        await conn.commit()

async def ui_button_groups():
    return await fetchall(
        "SELECT group_name,COUNT(*) c FROM ui_button_labels GROUP BY group_name ORDER BY group_name"
    )

async def ui_buttons(group_name:str='',limit:int=12,offset:int=0):
    if group_name:
        return await fetchall(
            "SELECT * FROM ui_button_labels WHERE group_name=? ORDER BY id LIMIT ? OFFSET ?",
            (group_name,limit,offset),
        )
    return await fetchall("SELECT * FROM ui_button_labels ORDER BY id LIMIT ? OFFSET ?",(limit,offset))

async def ui_button_count(group_name:str='') -> int:
    if group_name:
        row=await fetchone("SELECT COUNT(*) c FROM ui_button_labels WHERE group_name=?",(group_name,))
    else:
        row=await fetchone("SELECT COUNT(*) c FROM ui_button_labels")
    return int(row['c'] or 0) if row else 0

async def ui_button(button_id:int):
    return await fetchone("SELECT * FROM ui_button_labels WHERE id=?",(button_id,))

async def set_ui_button_custom_text(button_id:int,text:str):
    await execute("UPDATE ui_button_labels SET custom_text=?,updated_at=? WHERE id=?",(text.strip(),NOW(),button_id))
    return await ui_button(button_id)

async def set_ui_button_custom_emoji(button_id:int,custom_emoji_id:str):
    await execute(
        "UPDATE ui_button_labels SET custom_emoji_id=?,updated_at=? WHERE id=?",
        ((custom_emoji_id or '').strip(),NOW(),button_id),
    )
    return await ui_button(button_id)


async def apply_ui_button_custom_emoji_map(mapping:dict[str,str]) -> int:
    """Apply a prepared per-button Premium emoji map without sending any Telegram messages."""
    pairs=[
        ((emoji_id or '').strip(),NOW(),(button_key or '').strip())
        for button_key,emoji_id in (mapping or {}).items()
        if (button_key or '').strip() and (emoji_id or '').strip()
    ]
    if not pairs:
        return 0
    async with aiosqlite.connect(settings.db_path, timeout=30) as conn:
        cur=await conn.executemany(
            "UPDATE ui_button_labels SET custom_emoji_id=?,updated_at=? WHERE button_key=?",
            pairs,
        )
        await conn.commit()
    return len(pairs)


async def clear_all_ui_button_custom_styles():
    await execute("UPDATE ui_button_labels SET custom_style='' WHERE TRIM(custom_style)<>''")

async def set_ui_button_custom_style(button_id:int,style:str):
    value=(style or '').strip().lower()
    if value not in {'default','primary','success','danger'}:
        value=''
    await execute(
        "UPDATE ui_button_labels SET custom_style=?,updated_at=? WHERE id=?",
        (value,NOW(),button_id),
    )
    return await ui_button(button_id)

async def reset_ui_button_style(button_id:int):
    return await set_ui_button_custom_style(button_id,'')

async def reset_ui_button(button_id:int):
    await execute("UPDATE ui_button_labels SET custom_text='',updated_at=? WHERE id=?",(NOW(),button_id))
    return await ui_button(button_id)

async def reset_ui_button_emoji(button_id:int):
    await execute("UPDATE ui_button_labels SET custom_emoji_id='',updated_at=? WHERE id=?",(NOW(),button_id))
    return await ui_button(button_id)

async def ui_buttons_by_custom_emoji(custom_emoji_id:str,limit:int=100):
    return await fetchall(
        "SELECT * FROM ui_button_labels WHERE custom_emoji_id=? ORDER BY id LIMIT ?",
        ((custom_emoji_id or '').strip(),limit),
    )

async def reset_ui_buttons_by_custom_emoji(custom_emoji_id:str):
    emoji_id=(custom_emoji_id or '').strip()
    rows=await ui_buttons_by_custom_emoji(emoji_id,10000)
    if emoji_id:
        await execute(
            "UPDATE ui_button_labels SET custom_emoji_id='',updated_at=? WHERE custom_emoji_id=?",
            (NOW(),emoji_id),
        )
    return rows

async def reset_all_ui_buttons():
    await execute("UPDATE ui_button_labels SET custom_text='',updated_at=?",(NOW(),))


# ---------- premium/custom emoji ----------
async def premium_emoji_rules():
    return await fetchall("SELECT * FROM premium_emoji_rules ORDER BY id")

async def premium_emoji_rule(rule_id:int):
    return await fetchone("SELECT * FROM premium_emoji_rules WHERE id=?",(rule_id,))

async def upsert_premium_emoji_rule(fallback_text:str,custom_emoji_id:str):
    await execute(
        """INSERT INTO premium_emoji_rules(fallback_text,custom_emoji_id,updated_at) VALUES (?,?,?)
           ON CONFLICT(fallback_text) DO UPDATE SET
             custom_emoji_id=excluded.custom_emoji_id, updated_at=excluded.updated_at""",
        ((fallback_text or '').strip(),(custom_emoji_id or '').strip(),NOW()),
    )
    return await fetchone("SELECT * FROM premium_emoji_rules WHERE fallback_text=?",((fallback_text or '').strip(),))

async def delete_premium_emoji_rule(rule_id:int):
    row=await premium_emoji_rule(rule_id)
    if row:
        await execute("DELETE FROM premium_emoji_rules WHERE id=?",(rule_id,))
    return row

async def reset_premium_emoji_rules():
    await execute("DELETE FROM premium_emoji_rules")

async def premium_emoji_rules_by_custom_emoji(custom_emoji_id:str):
    return await fetchall(
        "SELECT * FROM premium_emoji_rules WHERE custom_emoji_id=? ORDER BY id",
        ((custom_emoji_id or '').strip(),),
    )

async def delete_premium_emoji_rules_by_custom_emoji(custom_emoji_id:str):
    rows=await premium_emoji_rules_by_custom_emoji(custom_emoji_id)
    await execute("DELETE FROM premium_emoji_rules WHERE custom_emoji_id=?",((custom_emoji_id or '').strip(),))
    return rows


# ---------- imported Premium emoji packs ----------
async def premium_emoji_packs():
    return await fetchall("SELECT * FROM premium_emoji_packs ORDER BY id")

async def premium_emoji_pack(pack_id:int):
    return await fetchone("SELECT * FROM premium_emoji_packs WHERE id=?",(pack_id,))

async def premium_emoji_pack_by_name(set_name:str):
    return await fetchone("SELECT * FROM premium_emoji_packs WHERE set_name=?",((set_name or '').strip(),))

async def premium_emoji_pack_items(pack_id:int,limit:int=20,offset:int=0):
    return await fetchall(
        "SELECT * FROM premium_emoji_pack_items WHERE pack_id=? ORDER BY position,id LIMIT ? OFFSET ?",
        (pack_id,limit,offset),
    )

async def premium_emoji_pack_item_count(pack_id:int):
    row=await fetchone("SELECT COUNT(*) c FROM premium_emoji_pack_items WHERE pack_id=?",(pack_id,))
    return int(row['c'] or 0) if row else 0

async def premium_emoji_pack_item(item_id:int):
    return await fetchone(
        """SELECT i.*,p.title pack_title,p.set_name pack_set_name
           FROM premium_emoji_pack_items i
           LEFT JOIN premium_emoji_packs p ON p.id=i.pack_id
           WHERE i.id=?""",
        (item_id,),
    )

async def upsert_premium_emoji_pack(set_name:str,title:str,source_url:str,items:list[dict]):
    set_name=(set_name or '').strip()
    title=(title or set_name).strip() or set_name
    source_url=(source_url or '').strip()
    async with aiosqlite.connect(settings.db_path, timeout=30) as conn:
        conn.row_factory=aiosqlite.Row
        await conn.execute(
            """INSERT INTO premium_emoji_packs(set_name,title,source_url,sticker_count,updated_at) VALUES (?,?,?,?,?)
               ON CONFLICT(set_name) DO UPDATE SET
                 title=excluded.title,source_url=excluded.source_url,sticker_count=excluded.sticker_count,updated_at=excluded.updated_at""",
            (set_name,title,source_url,len(items),NOW()),
        )
        cur=await conn.execute("SELECT id FROM premium_emoji_packs WHERE set_name=?",(set_name,))
        row=await cur.fetchone();pack_id=int(row['id'])
        incoming_ids=[]
        for position,item in enumerate(items):
            custom_id=str(item.get('custom_emoji_id') or '').strip()
            if not custom_id:
                continue
            incoming_ids.append(custom_id)
            fallback=str(item.get('fallback_text') or '💎').strip() or '💎'
            file_id=str(item.get('file_id') or '').strip()
            await conn.execute(
                """INSERT INTO premium_emoji_pack_items(pack_id,position,custom_emoji_id,fallback_text,file_id,updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(custom_emoji_id) DO UPDATE SET
                     pack_id=excluded.pack_id,position=excluded.position,fallback_text=excluded.fallback_text,
                     file_id=excluded.file_id,updated_at=excluded.updated_at""",
                (pack_id,position,custom_id,fallback,file_id,NOW()),
            )
        if incoming_ids:
            placeholders=','.join('?' for _ in incoming_ids)
            await conn.execute(
                f"DELETE FROM premium_emoji_pack_items WHERE pack_id=? AND custom_emoji_id NOT IN ({placeholders})",
                (pack_id,*incoming_ids),
            )
        else:
            await conn.execute("DELETE FROM premium_emoji_pack_items WHERE pack_id=?",(pack_id,))
        await conn.execute(
            "UPDATE premium_emoji_packs SET sticker_count=?,updated_at=? WHERE id=?",
            (len(incoming_ids),NOW(),pack_id),
        )
        await conn.commit()
        cur=await conn.execute("SELECT * FROM premium_emoji_packs WHERE id=?",(pack_id,))
        return await cur.fetchone()

async def delete_premium_emoji_pack(pack_id:int):
    row=await premium_emoji_pack(pack_id)
    if not row:
        return None
    async with aiosqlite.connect(settings.db_path, timeout=30) as conn:
        cur=await conn.execute("SELECT id FROM premium_emoji_pack_items WHERE pack_id=?",(pack_id,))
        item_ids=[int(x[0]) for x in await cur.fetchall()]
        if item_ids:
            placeholders=','.join('?' for _ in item_ids)
            await conn.execute(
                f"UPDATE premium_emoji_placements SET pack_item_id=NULL WHERE pack_item_id IN ({placeholders})",
                tuple(item_ids),
            )
            await conn.execute(
                f"DELETE FROM premium_emoji_favorites WHERE pack_item_id IN ({placeholders})",
                tuple(item_ids),
            )
            await conn.execute(
                f"DELETE FROM premium_emoji_recent WHERE pack_item_id IN ({placeholders})",
                tuple(item_ids),
            )
        await conn.execute("DELETE FROM premium_emoji_pack_items WHERE pack_id=?",(pack_id,))
        await conn.execute("DELETE FROM premium_emoji_packs WHERE id=?",(pack_id,))
        await conn.commit()
    return row



async def premium_emoji_all_items():
    return await fetchall(
        """SELECT i.*,p.title pack_title,p.set_name pack_set_name,p.source_url pack_source_url
           FROM premium_emoji_pack_items i
           JOIN premium_emoji_packs p ON p.id=i.pack_id
           ORDER BY p.title COLLATE NOCASE,i.position,i.id"""
    )

async def premium_emoji_item_by_custom_id(custom_emoji_id:str):
    return await fetchone(
        """SELECT i.*,p.title pack_title,p.set_name pack_set_name
           FROM premium_emoji_pack_items i
           JOIN premium_emoji_packs p ON p.id=i.pack_id
           WHERE i.custom_emoji_id=? LIMIT 1""",
        ((custom_emoji_id or '').strip(),),
    )

async def premium_emoji_is_favorite(admin_id:int,item_id:int)->bool:
    return bool(await fetchone(
        "SELECT 1 FROM premium_emoji_favorites WHERE admin_id=? AND pack_item_id=?",
        (admin_id,item_id),
    ))

async def toggle_premium_emoji_favorite(admin_id:int,item_id:int)->bool:
    current=await premium_emoji_is_favorite(admin_id,item_id)
    if current:
        await execute("DELETE FROM premium_emoji_favorites WHERE admin_id=? AND pack_item_id=?",(admin_id,item_id))
        return False
    await execute(
        "INSERT OR IGNORE INTO premium_emoji_favorites(admin_id,pack_item_id,created_at) VALUES (?,?,?)",
        (admin_id,item_id,NOW()),
    )
    return True

async def premium_emoji_favorite_items(admin_id:int):
    return await fetchall(
        """SELECT i.*,p.title pack_title,p.set_name pack_set_name,f.created_at favorite_at
           FROM premium_emoji_favorites f
           JOIN premium_emoji_pack_items i ON i.id=f.pack_item_id
           JOIN premium_emoji_packs p ON p.id=i.pack_id
           WHERE f.admin_id=?
           ORDER BY f.created_at DESC,i.id DESC""",
        (admin_id,),
    )

async def mark_premium_emoji_recent(admin_id:int,item_id:int):
    await execute(
        """INSERT INTO premium_emoji_recent(admin_id,pack_item_id,last_used_at,use_count)
           VALUES (?,?,?,1)
           ON CONFLICT(admin_id,pack_item_id) DO UPDATE SET
             last_used_at=excluded.last_used_at,use_count=premium_emoji_recent.use_count+1""",
        (admin_id,item_id,NOW()),
    )

async def premium_emoji_recent_items(admin_id:int,limit:int=100):
    return await fetchall(
        """SELECT i.*,p.title pack_title,p.set_name pack_set_name,r.last_used_at,r.use_count
           FROM premium_emoji_recent r
           JOIN premium_emoji_pack_items i ON i.id=r.pack_item_id
           JOIN premium_emoji_packs p ON p.id=i.pack_id
           WHERE r.admin_id=?
           ORDER BY r.last_used_at DESC,r.use_count DESC
           LIMIT ?""",
        (admin_id,limit),
    )

async def premium_emoji_used_items():
    # One row per imported emoji currently used by a global rule, text placement,
    # or a UI button. Usage count is computed in SQL so the catalog can sort the
    # most important interface icons first.
    return await fetchall(
        """WITH usage AS (
             SELECT custom_emoji_id,COUNT(*) n FROM premium_emoji_rules GROUP BY custom_emoji_id
             UNION ALL
             SELECT custom_emoji_id,COUNT(*) n FROM premium_emoji_placements GROUP BY custom_emoji_id
             UNION ALL
             SELECT custom_emoji_id,COUNT(*) n FROM ui_button_labels
              WHERE TRIM(custom_emoji_id)<>'' GROUP BY custom_emoji_id
           ), summed AS (
             SELECT custom_emoji_id,SUM(n) usage_count FROM usage GROUP BY custom_emoji_id
           )
           SELECT i.*,p.title pack_title,p.set_name pack_set_name,s.usage_count
           FROM summed s
           JOIN premium_emoji_pack_items i ON i.custom_emoji_id=s.custom_emoji_id
           JOIN premium_emoji_packs p ON p.id=i.pack_id
           ORDER BY s.usage_count DESC,p.title COLLATE NOCASE,i.position"""
    )

# ---------- arbitrary text/caption Premium emoji placements ----------
async def premium_emoji_placements(limit:int=500,offset:int=0):
    return await fetchall(
        "SELECT * FROM premium_emoji_placements ORDER BY LENGTH(match_text) DESC,id LIMIT ? OFFSET ?",
        (limit,offset),
    )

async def premium_emoji_placement(rule_id:int):
    return await fetchone("SELECT * FROM premium_emoji_placements WHERE id=?",(rule_id,))

async def premium_emoji_placement_count():
    row=await fetchone("SELECT COUNT(*) c FROM premium_emoji_placements")
    return int(row['c'] or 0) if row else 0

async def premium_emoji_pack_item_placements(item_id:int):
    return await fetchall(
        "SELECT * FROM premium_emoji_placements WHERE pack_item_id=? ORDER BY id",
        (item_id,),
    )

async def upsert_premium_emoji_placement(pack_item_id:int|None,custom_emoji_id:str,fallback_text:str,match_text:str,position:str):
    custom_emoji_id=(custom_emoji_id or '').strip()
    fallback_text=(fallback_text or '💎').strip() or '💎'
    match_text=(match_text or '').strip()
    position=(position or 'before').strip().lower()
    if not custom_emoji_id or not match_text or position not in {'before','after','replace','replace_emoji'}:
        return None
    await execute(
        """INSERT INTO premium_emoji_placements(pack_item_id,custom_emoji_id,fallback_text,match_text,position,updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(custom_emoji_id,match_text,position) DO UPDATE SET
             pack_item_id=excluded.pack_item_id,fallback_text=excluded.fallback_text,updated_at=excluded.updated_at""",
        (pack_item_id,custom_emoji_id,fallback_text,match_text,position,NOW()),
    )
    return await fetchone(
        "SELECT * FROM premium_emoji_placements WHERE custom_emoji_id=? AND match_text=? AND position=?",
        (custom_emoji_id,match_text,position),
    )

async def delete_premium_emoji_placement(rule_id:int):
    row=await premium_emoji_placement(rule_id)
    if row:
        await execute("DELETE FROM premium_emoji_placements WHERE id=?",(rule_id,))
    return row

async def reset_premium_emoji_placements():
    await execute("DELETE FROM premium_emoji_placements")

async def delete_premium_emoji_placements_by_custom_emoji(custom_emoji_id:str):
    emoji_id=(custom_emoji_id or '').strip()
    rows=await fetchall(
        "SELECT * FROM premium_emoji_placements WHERE custom_emoji_id=? ORDER BY id",
        (emoji_id,),
    )
    if emoji_id:
        await execute("DELETE FROM premium_emoji_placements WHERE custom_emoji_id=?",(emoji_id,))
    return rows

