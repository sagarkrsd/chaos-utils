# Use Alpine-based Python image for a smaller footprint
FROM --platform=linux/amd64 python:3.9-alpine

# Install required system packages
RUN apk add --no-cache \
    curl \
    bash \
    && rm -rf /var/cache/apk/*

# Install Python dependencies
RUN pip install --no-cache-dir kubernetes

# Install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && \
    chmod +x kubectl && \
    mv kubectl /usr/local/bin/

# Set Python to unbuffered mode for better logging
ENV PYTHONUNBUFFERED=1

# Copy the Python script into the container
COPY script.py /app/script.py

# Make script executable
RUN chmod +x /app/script.py

# Set the working directory
WORKDIR /app

# Run the Python script by default
ENTRYPOINT ["python", "script.py"]
