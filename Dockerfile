FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create needed directories
RUN mkdir -p input output processed failed tmp logs

# Set Linux font and defaults
ENV SUBTITLE_FONT_PATH=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
ENV USE_IONICE=0

CMD ["python", "bot.py"]
