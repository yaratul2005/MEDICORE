import os
from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


def detect_default_env() -> Literal["colab", "local", "prod"]:
    env_var = os.getenv("MEDICORE_ENV", "").lower()
    if env_var in ("colab", "local", "prod"):
        return env_var  # type: ignore
    # Check if running inside Google Colab
    if os.path.exists("/content") or "COLAB_RELEASE_TAG" in os.environ or "COLAB_GPU" in os.environ:
        return "colab"
    return "local"


def detect_default_db_path(env_profile: str) -> str:
    # 1. Check explicit environment override
    custom_path = os.getenv("DATABASE_PATH")
    if custom_path:
        return custom_path

    # 2. Check if Google Drive is mounted
    drive_mount_path = Path("/content/drive/MyDrive/medicore")
    if drive_mount_path.parent.exists():
        drive_mount_path.mkdir(parents=True, exist_ok=True)
        return str(drive_mount_path / "medicore.db")

    # 3. If in Colab
    if env_profile == "colab" or os.path.exists("/content"):
        return "/content/medicore.db"

    # 4. Local fallback
    local_dir = Path(__file__).resolve().parent.parent.parent
    return str(local_dir / "medicore.db")


class Settings(BaseSettings):
    app_name: str = "MediCore"
    app_version: str = "0.1.0"
    env: Literal["colab", "local", "prod"] = detect_default_env()
    debug: bool = True
    secret_key: str = "medicore-super-secure-production-ready-secret-key-2026"
    access_token_expire_minutes: int = 60 * 24

    # Host & Port
    host: str = "0.0.0.0"
    port: int = 8000

    # Database
    db_path: str = ""
    database_url: str = ""

    # UI & Hospital Defaults
    hospital_name: str = "MediCore Central Hospital"
    hospital_tagline: str = "Clinical Excellence & Integrated Care"
    mrn_prefix: str = "MC"

    model_config = SettingsConfigDict(
        env_prefix="MEDICORE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def __init__(self, **values):
        super().__init__(**values)
        if not self.db_path:
            self.db_path = detect_default_db_path(self.env)
        if not self.database_url:
            self.database_url = os.getenv("DATABASE_URL", "")
        if not self.database_url:
            # Normalize SQLite URL for cross-platform compatibility
            normalized_path = self.db_path.replace("\\", "/")
            if not normalized_path.startswith("/"):
                # Windows absolute path or relative
                if len(normalized_path) > 1 and normalized_path[1] == ":":
                    self.database_url = f"sqlite:///{normalized_path}"
                else:
                    self.database_url = f"sqlite:///{normalized_path}"
            else:
                self.database_url = f"sqlite:///{normalized_path}"

        if self.env == "prod":
            self.debug = False


settings = Settings()
