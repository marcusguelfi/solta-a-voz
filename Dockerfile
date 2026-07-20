# Solta a Voz — imagem CPU para servidor doméstico (Coolify/Portainer/compose).
# Os modelos de IA baixam no primeiro preparo e ficam no volume /app/data.
FROM python:3.12-slim

# gcc + libc6-dev: o diffq (dependência do audio-separator) não tem wheel pro
# linux/py3.12 e compila uma extensão C na instalação (precisa dos headers)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch CPU primeiro (do índice cpu — MUITO menor que o padrão com CUDA)
COPY requirements.txt .
# ‼️ torchaudio JUNTO, do MESMO índice: instalado depois (como dependência
# transitiva do índice padrão) ele vem numa versão incompatível com o torch cpu
# e o .so não carrega — `OSError: Could not load _torchaudio.abi3.so`. Isso
# derruba o motor MMS (opcional, tem fallback, mas some sem avisar).
RUN pip install --no-cache-dir torch torchaudio \
        --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY server/ server/
COPY static/ static/

# cache do whisper/modelos dentro do volume, pra sobreviver a rebuilds
ENV XDG_CACHE_HOME=/app/data/.cache \
    KARAOKE_HOST=0.0.0.0

VOLUME /app/data
EXPOSE 8777

CMD ["python", "server/main.py"]
