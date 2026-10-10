FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Код принадлежит root: процесс ловушки (65532) может его только читать, даже без
# --read-only. Базовый образ закрепляется по digest при публикации в GHCR.
COPY honeypots/ /app/honeypots/
USER 65532:65532
EXPOSE 2222 8080
ENTRYPOINT ["python", "-m", "honeypots.low"]
CMD ["--host", "0.0.0.0"]
