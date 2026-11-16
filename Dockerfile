FROM python:3.12-slim

ENV TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# 直接运行 python main.py 即可，因为脚本内部已经配置好了 host 和 port
CMD ["python", "main.py"]