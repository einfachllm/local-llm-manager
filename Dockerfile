FROM python:3.12-slim

# Docker CLI only; the manager talks to the host daemon through the mounted socket.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dgxspark_manager ./dgxspark_manager

ENV DGXM_DB=/data/manager.db PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "-m", "dgxspark_manager.main"]
