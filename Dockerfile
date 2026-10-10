FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY hookrelay ./hookrelay
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --system --uid 10001 hookrelay
USER hookrelay
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-", "hookrelay:create_app()"]
