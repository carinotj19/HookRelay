import os
from datetime import timedelta


class Config:
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///hookrelay.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    MAX_CONTENT_LENGTH = 256 * 1024
    SIGNATURE_TOLERANCE_SECONDS = 300
    MAX_DELIVERY_ATTEMPTS = 5
    RETRY_BASE_SECONDS = 10
    RETRY_CAP_SECONDS = 3600
    LEASE_SECONDS = 60
    DISPATCH_BATCH_SIZE = 100
    DELIVERY_TIMEOUT_SECONDS = 10
    ALLOW_PRIVATE_DESTINATIONS = os.getenv("ALLOW_PRIVATE_DESTINATIONS", "0") == "1"

    LOCAL_PLAYGROUND_ENABLED = os.getenv("LOCAL_PLAYGROUND_ENABLED", "0") == "1"

    # Web dashboard stays disabled until a stable 32+ character secret is set.
    SECRET_KEY = os.getenv("DASHBOARD_SESSION_SECRET")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Strict"
    SESSION_COOKIE_SECURE = os.getenv("DASHBOARD_COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=45)
