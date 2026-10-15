#!/bin/sh
# Точка входа образа Medium HoneyForge: собирает ожидаемую Cowrie раскладку из
# смонтированных каталогов и запускает ловушку основным процессом контейнера.
#
# Контракт с драйвером (agent/runtime/drivers/medium.py):
#   --config-dir=<DIR>  только для чтения: cowrie.cfg и userdb.txt, созданные драйвером
#   --state-dir=<DIR>   записываемый: log, lib/tty, lib/downloads, run, keys
# Других аргументов нет: профиль центра и учётные данные передаются файлами, а не
# аргументами, потому что аргументы видны в docker inspect и в списке процессов хоста.
#
# Имена переменных и флагов Cowrie взяты из документации проекта и НЕ подтверждены на
# закреплённом релизе: живой прогон с Docker и Cowrie не выполнялся. Перед первым
# запуском сверьте способ указания домашнего каталога, путь поиска cowrie.cfg и флаг
# запуска без демонизации (honeypots/medium/README.md, infra/runtime/README.md).
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

# Cowrie ожидает конфигурацию в etc/ своего домашнего каталога. Ссылки оставляют её
# содержимое в каталоге только для чтения: процесс ловушки не может переписать свои
# учётные данные через записываемое состояние.
mkdir -p "${STATE_DIR}/etc"
ln -sfn "${CONFIG_DIR}/cowrie.cfg" "${STATE_DIR}/etc/cowrie.cfg"
ln -sfn "${CONFIG_DIR}/userdb.txt" "${STATE_DIR}/etc/userdb.txt"

COWRIE_HOME="${STATE_DIR}"
export COWRIE_HOME
cd "${STATE_DIR}"
# Ключи хоста создаются при первом запуске экземпляра в keys/ и остаются на хосте,
# поэтому у каждого экземпляра свой отпечаток, а корень контейнера остаётся read-only.
exec cowrie start -n
