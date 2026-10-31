from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ASK_YMCA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    database_url: str = "postgresql+psycopg://ask_ymca:ask_ymca@localhost:5432/ask_ymca"
    cors_origins: list[str] = []
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:1.5b"
    ollama_timeout_seconds: float = 45
    ollama_embedding_model: str = "embeddinggemma"
    ollama_embedding_timeout_seconds: float = 60
    admin_token: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
