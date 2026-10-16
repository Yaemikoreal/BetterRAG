FROM python:3.11-slim

WORKDIR /app

# 安装基础依赖与健康检查工具 curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝工程核心源码与数据目录结构
COPY src/ /app/src/
COPY data/ /app/data/

ENV PYTHONUNBUFFERED=1
ENV PORT=8000
ENV PYTHONPATH=/app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl -f http://localhost:8000/api/v1/health || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
