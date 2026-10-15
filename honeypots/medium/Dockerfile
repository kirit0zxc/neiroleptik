FROM cowrie/cowrie:3.1.1@sha256:710f1e4601a663ac8591f58a42b44e4cd3b7ea170c7e6cdb9e54c430a30cd459
# Конфигурация не принимается из профиля: оператор задаёт проверенный образ.
COPY honeypots/medium/launch.py /honeyforge-launch.py
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["/cowrie/cowrie-env/bin/python3", "/honeyforge-launch.py"]
CMD []
