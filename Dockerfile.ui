# syntax=docker/dockerfile:1.7
#
# convbot-ui: Streamlit chat UI. Talks to convbot-api over the docker
# network at http://api:8000. Genuinely slim image — only streamlit + httpx,
# no torch / chromadb / anthropic. Exposes port 8501.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# UI-only deps. The UI doesn't talk to the LLM or the database directly —
# it just POSTs questions to the api service over HTTP.
RUN pip install --no-cache-dir \
        "streamlit>=1.40.0" \
        "httpx>=0.28.0"

COPY ui/ ui/

EXPOSE 8501
CMD ["streamlit", "run", "ui/app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
