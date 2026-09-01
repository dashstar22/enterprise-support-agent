"""Database engine and session-factory helpers. / 数据库引擎与会话工厂辅助函数。"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base

DatabaseSessionFactory = sessionmaker[Session]


def create_database_engine(database_url: str) -> Engine:
    """Build a synchronous engine for PostgreSQL or an explicit test database. / 为 PostgreSQL 或显式测试库创建同步引擎。"""

    return create_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> DatabaseSessionFactory:
    """Create independent transaction sessions. / 创建彼此独立的事务会话。"""

    return sessionmaker(bind=engine, expire_on_commit=False)


def create_schema_for_test(engine: Engine) -> None:
    """Create all tables only for isolated tests. / 仅为隔离测试创建所有表。"""

    Base.metadata.create_all(engine)
