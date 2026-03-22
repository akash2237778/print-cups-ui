# Use a slim Python 3.11 image
FROM python:3.11-slim

# Install cups-client to get access to `lp` and `lpstat` commands
RUN apt-get update && \
    apt-get install -y cups-client && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY index.html .

# Expose the web port
EXPOSE 8000

# Start the FastAPI app via Uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]