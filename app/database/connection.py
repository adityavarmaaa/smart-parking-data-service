from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)