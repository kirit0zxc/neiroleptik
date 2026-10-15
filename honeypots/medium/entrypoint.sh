#!/bin/sh
# Точка входа образа Medium HoneyForge: проверяет смонтированные каталоги и запускает
# Cowrie основным процессом контейнера.
#
# Контракт с драйвером (agent/runtime/drivers/medium.py):
#   --config-dir=<DIR>  только для чтения: cowrie.cfg и userdb.txt, созданные драйвером
#   --state-dir=<DIR>   записываемый: log, lib/tty, lib/downloads, run, keys
# Других аргументов нет: профиль центра и учётные данные передаются файлами, а не
# аргументами, потому что аргументы видны в docker inspect и в списке процессов хоста.
#
# Скрипт ничего не создаёт и не меняет: ссылки из каталога Cowrie на эти файлы созданы
# при сборке образа (см. Dockerfile), поэтому процесс ловушки не может их подменить, а
# относительные пути апстрима, включая cowrie.cfg.dist, остаются внутри образа.
#
# Флаг запуска без демонизации взят из документации Cowrie и НЕ подтверждён на
# закреплённом релизе: живой прогон с Docker и Cowrie не выполнялся
# (honeypots/medium/README.md, infra/runtime/README.md).
set -eu

CONFIG_DIR=""
STATE_DIR=""
for argument in "$@"; do
    case "${argument}" in
        --config-dir=*) CONFIG_DIR="${argument#*=}" ;;
        --state-dir=*) STATE_DIR="${argument#*=}" ;;
        *)
            echo "неизвестный аргумент точки входа" >&2
            exit 2
            ;;
    esac
done
if [ -z "${CONFIG_DIR}" ] || [ -z "${STATE_DIR}" ]; then
    echo "нужны --config-dir и --state-dir" >&2
    exit 2
fi
for required in "${CONFIG_DIR}/cowrie.cfg" "${CONFIG_DIR}/userdb.txt"; do
    if [ ! -f "${required}" ]; then
        echo "каталог конфигурации неполный" >&2
        exit 2
    fi
done
for required in log lib/tty lib/downloads run keys; do
    if [ ! -d "${STATE_DIR}/${required}" ]; then
        echo "каталог состояния неполный" >&2
        exit 2
    fi
done
if [ ! -d "${HONEYFORGE_COWRIE_HOME}" ]; then
    echo "домашний каталог Cowrie недоступен" >&2
    exit 2
fi

# Рабочим каталогом остаётся установка Cowrie: записываемые пути задаёт cowrie.cfg,
# а ключи хоста создаются при первом запуске экземпляра в state/keys, поэтому у
# каждого экземпляра свой отпечаток, а корень контейнера остаётся read-only.
cd "${HONEYFORGE_COWRIE_HOME}"
exec cowrie start -n
