# Use an official, lightweight Python image as a starting point
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

# --- Install System Dependencies ---
# This is the crucial step to get ffmpeg.
# We run `apt-get` which is the package manager for the Debian Linux OS in the container.
RUN apt-get update && apt-get install -y ffmpeg git

# --- Install Python Packages ---
# Copy only the requirements file first to leverage Docker's build cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Copy Your Application Code ---
# Copy all the files from your local project folder into the container's /app directory
COPY . .

# --- Command to Run the Application ---
# Tell Render how to start your app using the Gunicorn production server.
# This command will be run every time your container starts.
CMD ["gunicorn", "--workers", "4", "--bind", "0.0.0.0:8080", "main:app"]
