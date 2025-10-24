FROM python:3.10-slim
ENV COQUI_TOS_AGREED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    espeak-ng libespeak-ng1 \
    git \
    && rm -rf /var/lib/apt/lists/*
COPY docker/constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir -r /tmp/constraints.txt
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -e .
ENTRYPOINT ["anime-v1"]
