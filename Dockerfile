FROM python:3.12-slim

WORKDIR /app

# Abhängigkeiten zuerst – bleibt bei Codeänderungen im Build-Cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Exec-Form: die --args aus "gcloud run jobs execute" werden angehängt
ENTRYPOINT ["python", "main.py"]
