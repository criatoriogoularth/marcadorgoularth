FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd -r -u 1001 app && chown -R app /app
USER app

EXPOSE 8000
# --workers 1 é obrigatório: o tempo ao vivo fica na memória do processo.
# Para aguentar mais celulares simultâneos, aumente só --threads.
CMD ["sh", "-c", "exec gunicorn app:app --worker-class gthread --workers 1 --threads ${THREADS:-16} --bind 0.0.0.0:${PORT:-8000} --timeout 60 --access-logfile -"]
