# Use the official Python 3.10 image as base
FROM python:3.10-alpine

# Set working directory in the container
WORKDIR /app

# Copy the Python files and requirements.txt into the container
COPY cg_proxy_xrs.py .
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port 8080
EXPOSE 8080

# Command to run the Python script
CMD ["python", "cg_proxy_xrs.py"]

