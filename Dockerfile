FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# GitPython (used by app/extractors/code.py) shells out to the git binary
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --default-timeout=120 --retries 10 -r requirements.txt

# Chromium + its OS-level deps for the Playwright prototype validator
RUN playwright install --with-deps chromium

# Pre-bake the embedding model so the first user request doesn't stall
# on a cold Hugging Face Hub download
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

ENV PYTHONPATH=/app

COPY . .
RUN mkdir -p data/uploads data/chroma_db data/screenshots

# Hugging Face Spaces (Docker SDK) expects the app on port 7860
EXPOSE 7860
CMD ["streamlit", "run", "app/ui/streamlit_chat.py", \
     "--server.port=7860", "--server.address=0.0.0.0"]
