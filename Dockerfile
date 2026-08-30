FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the script
COPY dredger.py .
COPY vegan_filter.py .

# Copy maintenance tools
COPY maintenance/ ./maintenance/

# Run the script
CMD ["python", "dredger.py"]
