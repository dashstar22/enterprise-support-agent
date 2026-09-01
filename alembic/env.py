"""Alembic environment for the PostgreSQL audit schema. / PostgreSQL 审计结构的 Alembic 环境。"""

from logging.config import fileConfig
from os import environ

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
# Compose injects the reachable service hostname (`db`), while alembic.ini remains a safe local example.
# Compose 注入容器网络可达的 `db` 主机名, alembic.ini 保留安全的本地示例地址。
database_url = environ.get("ESA_DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without connecting to a database. / 不连接数据库地生成 SQL。"""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations through one connection. / 通过一个连接执行迁移。"""

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
