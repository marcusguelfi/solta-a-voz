# Solta a Voz — imagem CPU para servidor doméstico (Coolify/Portainer/compose).
# Os modelos de IA baixam no primeiro preparo e ficam no volume /app/data.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch CPU primeiro (do índice cpu — MUITO menor que o padrão com CUDA)
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY server/ server/
COPY static/ static/

# cache do whisper/modelos dentro do volume, pra sobreviver a rebuilds
ENV XDG_CACHE_HOME=/app/data/.cache \
    KARAOKE_HOST=0.0.0.0

VOLUME /app/data
EXPOSE 8777

CMD ["python", "server/main.py"]
