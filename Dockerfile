FROM python:3.12-slim

LABEL org.opencontainers.image.title="DGX Spark Manager" \
      org.opencontainers.image.description="Web dashboard to run, schedule and monitor LLM serving recipes (vLLM, SGLang, MLX, llama.cpp, ...)" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/olekslev69/dgxspark-manager"

# Docker CLI only; the manager talks to the host daemon through the mounted socket.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dgxspark_manager ./dgxspark_manager
COPY LICENSE NOTICE ./

ENV DGXM_DB=/data/manager.db PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8090
CMD ["python", "-m", "dgxspark_manager.main"]
