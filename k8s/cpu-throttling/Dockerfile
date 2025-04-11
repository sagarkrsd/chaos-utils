# Start from a base image with Python
FROM --platform=linux/amd64 python:3.9-slim

# Install dependencies for Kubernetes API and kubectl
RUN pip install kubernetes && \
    apt-get update && \
    apt-get install -y curl && \
    curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/arm64/kubectl" && \
    install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Set environment variables for Kubernetes
# ENV KUBECONFIG=/root/.kube/config

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
