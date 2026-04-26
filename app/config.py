"""
Configuración de la app cargada desde .env mediante pydantic-settings.
Se importa como un singleton: `from app.config import settings`.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./novel_analyzer.db"
    app_name: str = "Novel Analyzer API"
    app_version: str = "0.1.0"
    debug: bool = True

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8")


# Instancia única compartida por toda la app
settings = Settings()
