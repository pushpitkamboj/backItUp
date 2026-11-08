from alembic import context
from sqlalchemy import create_engine

from backitup.config import get_settings
from backitup.models import Base


def run():
    settings = get_settings()
    database_url = settings.sqlalchemy_database_url()
    if context.is_offline_mode():
        context.configure(url=database_url, target_metadata=Base.metadata, literal_binds=True, include_schemas=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine(database_url)
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata, include_schemas=True)
            with context.begin_transaction():
                context.run_migrations()


run()
