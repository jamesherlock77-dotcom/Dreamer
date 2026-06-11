FROM python:3.11
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
ARG CACHEBUST=1
CMD ["python", "main.py"]
