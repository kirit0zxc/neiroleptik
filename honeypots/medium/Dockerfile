# Образ Medium: ловушка Cowrie, которая получает конфигурацию от драйвера агента.
#
# Апстрим закрепляется тегом и digest при публикации: драйвер запускает контейнер с
# --pull never, образы не собирает и не скачивает (см. README.md рядом). Кандидат на
# закрепление согласован в infra/runtime/README.md; digest подставляется после spike.
ARG COWRIE_IMAGE=cowrie/cowrie:3.1.1
FROM ${COWRIE_IMAGE}
# Точка входа принадлежит root: процесс ловушки может её только читать и выполнять,
# даже если корневая файловая система когда-нибудь окажется записываемой.
USER root
COPY honeypots/medium/entrypoint.sh /honeyforge/entrypoint.sh
# Каталоги целей монтирования создаются в образе, чтобы раскладка контейнера не
# зависела от порядка создания точек монтирования при --read-only.
RUN chmod 0555 /honeyforge/entrypoint.sh \
    && mkdir -p /honeyforge/conf /honeyforge/state \
    && chown 1000:1000 /honeyforge/conf /honeyforge/state
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# uid:gid должен совпадать с --medium-user исполнителя и с владельцем каталогов на
# хосте, иначе ловушка не сможет писать в смонтированное состояние.
USER 1000:1000
EXPOSE 2222
ENTRYPOINT ["/honeyforge/entrypoint.sh"]
CMD ["--config-dir=/honeyforge/conf", "--state-dir=/honeyforge/state"]
