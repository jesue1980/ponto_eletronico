import os


BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class BaseConfig:
    APP_ENV = os.environ.get("APP_ENV", "development")
    SECRET_KEY = os.environ.get("SECRET_KEY", "ponto-eletronico-repp-dev")
    SQLITE_DB_PATH = os.environ.get("SQLITE_DB_PATH", os.path.join(BASE_DIR, "ponto_eletronico.db"))
    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 128 * 1024 * 1024))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0").lower() in ("1", "true", "yes")
    PREFERRED_URL_SCHEME = os.environ.get("PREFERRED_URL_SCHEME", "http")
    SERVER_NAME = os.environ.get("SERVER_NAME") or None
    LOG_DIR = os.environ.get("LOG_DIR", os.path.join(BASE_DIR, "logs"))
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    BEHIND_PROXY = os.environ.get("BEHIND_PROXY", "0").lower() in ("1", "true", "yes")


class DevelopmentConfig(BaseConfig):
    DEBUG = False
    APP_ENV = "development"


class ProductionConfig(BaseConfig):
    DEBUG = False
    APP_ENV = "production"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "1").lower() in ("1", "true", "yes")
    PREFERRED_URL_SCHEME = os.environ.get("PREFERRED_URL_SCHEME", "https")


CONFIGS = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
}


def get_config():
    return CONFIGS.get(os.environ.get("APP_ENV", "development").lower(), DevelopmentConfig)
