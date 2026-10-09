FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --chown=65532:65532 honeypots/ /app/honeypots/
USER 65532:65532
EXPOSE 2222 8080
ENTRYPOINT ["python", "-m", "honeypots.low"]
CMD ["--host", "0.0.0.0"]
