# The Cloud Run image for the webhook receiver (co_lectr.web:app).
#
# The repo *is* the co_lectr package and every import is `co_lectr.*`, so the
# code has to sit in a directory of that name with its parent on the path — the
# same reason the README clones into `co_lectr/`. gunicorn is the production
# server; it is imported by nothing in the code, so it lives here, not in
# requirements.txt.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1

# Install deps first, off requirements alone, so a code change does not rebuild
# the dependency layer.
COPY requirements.txt /app/co_lectr/requirements.txt
RUN pip install --no-cache-dir -r /app/co_lectr/requirements.txt gunicorn==23.0.0

COPY . /app/co_lectr

# Cloud Run sends traffic to $PORT (8080 by default). One worker is enough for a
# webhook that returns fast; threads absorb GitHub's concurrent deliveries.
CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 8 co_lectr.web:app
