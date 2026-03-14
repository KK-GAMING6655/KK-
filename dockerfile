FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsndfile1 libasound2 libpulse0 build-essential libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["python","bot.py"]
