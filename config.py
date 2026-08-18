import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)

def env_int(name: str, default: int) -> int:
    try: return int(os.getenv(name, '').strip() or default)
    except ValueError: return default

def env_float(name: str, default: float) -> float:
    try: return float((os.getenv(name, '').strip() or str(default)).replace(',','.'))
    except ValueError: return default

def env_path(name: str, default: str) -> str:
    raw = os.getenv(name, default).strip() or default
    p = Path(raw)
    return str(p if p.is_absolute() else BASE_DIR / p)

@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv('BOT_TOKEN','').strip()
    admin_id: int = env_int('ADMIN_ID',0)
    shop_username: str = os.getenv('SHOP_USERNAME','').strip().lstrip('@')
    db_path: str = env_path('DB_PATH','shop.db')
    payment_card: str = os.getenv('PAYMENT_CARD','').strip()
    payment_recipient: str = os.getenv('PAYMENT_RECIPIENT','').strip()
    payment_provider_token: str = (os.getenv('TELEGRAM_PROVIDER_TOKEN','') or os.getenv('PAYMENT_PROVIDER_TOKEN','')).strip()

    legal_version: str = os.getenv('LEGAL_VERSION','2026-08-12-v5').strip() or '2026-08-12-v5'
    seller_name: str = os.getenv('SELLER_NAME','УКАЖИТЕ ПРОДАВЦА').strip()
    seller_status: str = os.getenv('SELLER_STATUS','Самозанятый / ИП / ООО').strip()
    seller_inn: str = os.getenv('SELLER_INN','УКАЖИТЕ ИНН').strip()
    seller_email: str = os.getenv('SELLER_EMAIL','УКАЖИТЕ EMAIL').strip()
    seller_address: str = os.getenv('SELLER_ADDRESS','УКАЖИТЕ АДРЕС').strip()
    return_address: str = os.getenv('RETURN_ADDRESS','').strip()
    support_contact: str = os.getenv('SUPPORT_CONTACT','').strip()

    main_banner: str = (os.getenv('WELCOME_BANNER','') or os.getenv('MAIN_BANNER','')).strip()
    bot_username: str = os.getenv('BOT_USERNAME','').strip().lstrip('@')
    size_chart_text: str = os.getenv('SIZE_CHART_TEXT','S — грудь 88–94 см\nM — 95–101 см\nL — 102–108 см\nXL — 109–116 см').strip()

    low_stock_threshold: int = env_int('LOW_STOCK_THRESHOLD',2)
    abandoned_cart_hours: float = env_float('ABANDONED_CART_HOURS',6)
    loyalty_threshold: int = env_int('LOYALTY_THRESHOLD',20000)
    loyalty_discount_percent: int = env_int('LOYALTY_DISCOUNT_PERCENT',5)
    loyalty_cashback_percent: int = env_int('LOYALTY_CASHBACK_PERCENT',3)
    referral_bonus_points: int = env_int('REFERRAL_BONUS', env_int('REFERRAL_BONUS_POINTS',300))
    max_points_percent: int = env_int('MAX_BONUS_PERCENT',20)
    default_product_weight: int = env_int('DEFAULT_PRODUCT_WEIGHT',500)

    # Production/runtime hardening
    reservation_minutes: int = env_int('RESERVATION_MINUTES',20)
    reservation_receipt_hours: int = env_int('RESERVATION_RECEIPT_HOURS',24)
    backup_dir: str = env_path('BACKUP_DIR','backups')
    backup_keep: int = env_int('BACKUP_KEEP',14)
    backup_interval_hours: int = env_int('BACKUP_INTERVAL_HOURS',24)
    telegram_request_timeout: int = env_int('TELEGRAM_REQUEST_TIMEOUT',60)
    polling_concurrency_limit: int = env_int('POLLING_CONCURRENCY_LIMIT',100)
    log_dir: str = env_path('LOG_DIR','logs')

    cdek_client_id: str = os.getenv('CDEK_CLIENT_ID','').strip()
    cdek_client_secret: str = os.getenv('CDEK_CLIENT_SECRET','').strip()
    cdek_origin_city: str = os.getenv('CDEK_ORIGIN_CITY','').strip()
    cdek_origin_region: str = os.getenv('CDEK_ORIGIN_REGION','').strip()
    cdek_origin_postal: str = os.getenv('CDEK_ORIGIN_POSTAL','').strip()
    cdek_fixed_cost: int = env_int('CDEK_FIXED_COST',0)

    russian_post_token: str = os.getenv('RUSSIAN_POST_TOKEN','').strip()
    russian_post_user_auth: str = (os.getenv('RUSSIAN_POST_USER_AUTH','') or os.getenv('RUSSIAN_POST_LOGIN','')).strip()
    post_origin_postal: str = os.getenv('POST_ORIGIN_POSTAL','').strip()
    post_fixed_cost: int = env_int('POST_FIXED_COST',0)

settings=Settings()
