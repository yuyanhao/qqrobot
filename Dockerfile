FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="qqrobot" \
      org.opencontainers.image.description="QQ official bot with pluggable OpenAI-compatible model providers" \
      org.opencontainers.image.source="https://github.com/yuyanhao/qqrobot" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 DATA_DIR=/app/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-xetex texlive-lang-chinese texlive-latex-extra texlive-science \
    fonts-noto-cjk ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 qqbot \
    && mkdir /app/data && chown qqbot:qqbot /app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY qqrobot/ ./qqrobot/
COPY skills/ ./skills/
COPY VERSION ./VERSION
USER qqbot
CMD ["python", "-m", "qqrobot"]
