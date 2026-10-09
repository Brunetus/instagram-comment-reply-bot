FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app/bot
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
CMD ["python", "-m", "bot.bot"]
