"""Settings read from the environment."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    session_secret: str
    ip_hash_salt: str
    db_path: str = 'data/tfm_adp_pricing.sqlite3'
    admin_password: str = ''
    public_base_url: str = ''
    cookie_secure: bool = True
    trust_fly_client_ip: bool = False
    gmail_address: str = ''
    gmail_app_password: str = ''
    export_spreadsheet_id: str = ''
    service_account_json: str = ''
    service_account_file: str = ''


def _flag(name, default):
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {'1', 'true', 'yes', 'on'}


def load_settings():
    load_dotenv()
    missing = [name for name in ('SESSION_SECRET', 'IP_HASH_SALT') if not os.getenv(name)]
    if missing:
        raise SystemExit(f"Missing required settings: {', '.join(missing)}")
    return Settings(
        session_secret=os.environ['SESSION_SECRET'],
        ip_hash_salt=os.environ['IP_HASH_SALT'],
        db_path=os.getenv('DB_PATH', Settings.db_path),
        admin_password=os.getenv('ADMIN_PASSWORD', ''),
        public_base_url=os.getenv('PUBLIC_BASE_URL', '').rstrip('/'),
        cookie_secure=_flag('COOKIE_SECURE', True),
        trust_fly_client_ip=_flag('TRUST_FLY_CLIENT_IP', False),
        gmail_address=os.getenv('GMAIL_ADDRESS', ''),
        gmail_app_password=os.getenv('GMAIL_APP_PASSWORD', ''),
        export_spreadsheet_id=os.getenv('EXPORT_SPREADSHEET_ID', ''),
        service_account_json=os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON', ''),
        service_account_file=os.getenv('SERVICE_ACCOUNT_FILE', ''),
    )
