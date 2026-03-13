FROM python:3.11-slim

# 安装Chromium依赖
RUN apt-get update && apt-get install -y \
    libglib2.0-0 \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libpango-1.0-0 \
    libcairo2 \
    fonts-liberation \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装Playwright Chromium
RUN playwright install chromium

# 复制源代码
COPY . .

# 创建数据持久化目录（Zeabur挂载 /app/data）
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 8080

# 运行应用
CMD ["python", "main.py"]