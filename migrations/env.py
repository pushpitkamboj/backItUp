from alembic import context
from sqlalchemy import create_engine

from backitup.config import get_settings
from backitup.models import Base


def run():
    if context.is_offline_mode():
        context.configure(url=get_settings().database_url, target_metadata=Base.metadata, literal_binds=True, include_schemas=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine(get_settings().database_url)
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata, include_schemas=True)
            with context.begin_transaction():
                context.run_migrations()


run()
