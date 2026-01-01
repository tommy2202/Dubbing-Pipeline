FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# Required for Coqui TTS model downloads (no interactive prompt)
ENV COQUI_TOS_AGREED=1

# Cache dirs (match .env.example defaults)
ENV HF_HOME=/root/.cache/huggingface
ENV TORCH_HOME=/root/.cache/torch
ENV TTS_HOME=/root/.local/share/tts

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    ffmpeg \
    espeak-ng \
    libsndfile1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Ensure `python` points to python3 and pip is up to date
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3 1 && \
    python -m pip install --upgrade pip

COPY docker/constraints.txt /tmp/constraints.txt

# Torch CUDA 12.4 wheels
RUN python -m pip install --no-cache-dir \
    torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 \
    --index-url https://download.pytorch.org/whl/cu124

# Install pinned Python dependencies
RUN python -m pip install --no-cache-dir -r /tmp/constraints.txt

# Pre-download models into image layers
RUN python -c "from TTS.api import TTS; TTS('tts_models/multilingual/multi-dataset/xtts_v2')" && \
    python -c "import whisper; whisper.load_model('large-v3')" && \
    python -c "import whisper; whisper.load_model('medium')" && \
    python -c "import whisper; whisper.load_model('small')"

WORKDIR /app
COPY . /app

# Install the project
RUN python -m pip install --no-cache-dir .

ENTRYPOINT ["anime-v2"]
