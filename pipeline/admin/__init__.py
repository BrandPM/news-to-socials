"""Admin UI backend package (IT_PROJ_NTS_014).

Houses the FastAPI app, SQLAlchemy models, Alembic migrations, and the
``AdminConfigClient`` that lets the pipeline read sources/prompts/config
from ``admin.db`` instead of hardcoded ``icon_brand_config()``.
"""
