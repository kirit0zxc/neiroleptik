# Образ Medium: ловушка Cowrie, которая получает конфигурацию от драйвера агента.
#
# Апстрим закрепляется тегом и digest при публикации: драйвер запускает контейнер с
# --pull never, образы не собирает и не скачивает (см. README.md рядом). Кандидат на
# закрепление согласован в infra/runtime/README.md; digest подставляется после spike.
ARG COWRIE_IMAGE=cowrie/cowrie:3.1.1
FROM ${COWRIE_IMAGE}
# Домашний каталог установки Cowrie: рядом с ним лежат cowrie.cfg.dist, share/, honeyfs/
# и txtcmds/, пути к которым Cowrie резолвит относительно себя. Значение взято из
# документации апстрима и ПРОВЕРЯЕТСЯ НА ЭТАПЕ СБОРКИ: неверное предположение ломает
# сборку образа, а не живой запуск ловушки.
ARG COWRIE_HOME_DIR=/cowrie/cowrie-git
# Точка входа и ссылки принадлежат root: процесс ловушки может их только читать и
# выполнять, даже если корневая файловая система когда-нибудь окажется записываемой.
USER root
COPY honeypots/medium/entrypoint.sh /honeyforge/entrypoint.sh
# Конфигурация подставляется ссылками в собственный каталог Cowrie, а не переносом его
# домашнего каталога: иначе относительные пути апстрима (в том числе cowrie.cfg.dist с
# значениями по умолчанию) указывали бы в пустой смонтированный каталог состояния.
# Ссылки созданы в образе и неизменяемы: процесс ловушки не может их подменить.
RUN set -eu \
    && test -d "${COWRIE_HOME_DIR}" \
    && test -f "${COWRIE_HOME_DIR}/etc/cowrie.cfg.dist" \
    && chmod 0555 /honeyforge/entrypoint.sh \
    && mkdir -p /honeyforge/conf /honeyforge/state "${COWRIE_HOME_DIR}/var" \
    && ln -sfn /honeyforge/conf/cowrie.cfg "${COWRIE_HOME_DIR}/etc/cowrie.cfg" \
    && ln -sfn /honeyforge/conf/userdb.txt "${COWRIE_HOME_DIR}/etc/userdb.txt" \
    && ln -sfn /honeyforge/state/run "${COWRIE_HOME_DIR}/var/run" \
    && chown 1000:1000 /honeyforge/conf /honeyforge/state
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HONEYFORGE_COWRIE_HOME=${COWRIE_HOME_DIR}
# uid:gid должен совпадать с --medium-user исполнителя и с владельцем каталогов на
# хосте, иначе ловушка не сможет писать в смонтированное состояние.
USER 1000:1000
EXPOSE 2222
ENTRYPOINT ["/honeyforge/entrypoint.sh"]
CMD ["--config-dir=/honeyforge/conf", "--state-dir=/honeyforge/state"]
