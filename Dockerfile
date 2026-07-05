FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install pyrogram tgcrypto python-dotenv sqlalchemy
CMD ['python', '-m', 'app.main']
