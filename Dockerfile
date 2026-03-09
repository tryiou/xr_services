FROM python:3.10-alpine

# Create non-root user
RUN addgroup -g 1000 -S cgproxy && \
    adduser -u 1000 -S cgproxy -G cgproxy -s /bin/sh

WORKDIR /app

COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY cg_proxy_xrs.py .
RUN chown cgproxy:cgproxy cg_proxy_xrs.py

# Create log directory for optional file logging
RUN mkdir -p /var/log/cgproxy && \
    chown cgproxy:cgproxy /var/log/cgproxy

# Switch to non-root user
USER cgproxy

EXPOSE 8080

# Health check: service is healthy if /health returns 200
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q --spider http://127.0.0.1:8080/health || exit 1

CMD ["python", "cg_proxy_xrs.py"]
