# Low: доверенная граница процесса в прежнем instance

Статус 14.10.2026: **предложение Данила к согласованию с Димой и backend**.
Формат и совместная фикстура подготовлены; producer runtime и потребление proof
collector **не реализованы**. Основание: PR #39, head
`20f19d2ce3c1aae6ec79205a971a6d358260dfb1`, main
`01f8272ba37ef16af7b905042b00a7a1d36808ca`.
Mapping/DTO/UUID из PR #39 сохраняются: process seq — private data.low_seq;
эта спецификация не вводит альтернативный Event DTO.

## 1. Что уже есть и чего не хватает

PR #39 добавляет LowProductionMapper и collect_low. Поздний
previous_journal_id=null после первого поколения даёт waiting/MISSING_PREDECESSOR
до сохранения новой цепочки, с footer у старого файла или без него.
OPEN_PREDECESSOR относится к обычной связанной ротации с открытым predecessor.
collect_low запускает conversion уже проверенного raw независимо от этого waiting.

Runtime сохраняет ID своей контейнерной попытки и применяет полный Docker ID для
stop/remove. Но durable writer epoch, fencing писателя, startup binding и экспорт
boundary proof отсутствуют. Рестарт агента, PID, StartedAt, смена filename, O_EXCL,
seq reset и поздний null сами не доказывают границу процесса.

Прежнее предложение writer_finalized закрывало **весь instance** и запрещало
следующий запуск этого UUID. Для обсуждаемого случая нужны два writer_id внутри
одного instance и закрытие только старого writer. Журнал остаётся
honeyforge-low-journal/1; никакого фиктивного footer не добавляется.

## 2. Поддерживаемый вариант и граница доверия

Предлагаемый первый вариант: runtime контролируемо удаляет старый контейнер и
создаёт **другой контейнер** с прежними agent/deployment/instance/revision и новым
writer_id. Один writer_id относится к одной разрешённой жизни контейнера:
повторный запуск удалённого writer запрещён durable fencing записью.
Существующая command start/recovery не объявляется разрешением такого перезапуска;
нужна отдельно согласованная внутренняя runtime операция и её тесты.

docker restart того же container ID и самопроизвольный restart процесса внутри
живого контейнера пока не поддерживают этот proof. Для них нужны supervisor,
устойчивая идентичность дочернего процесса и ограждение всех его потомков.

Proof хранится в защищённом runtime control-каталоге **вне RW mount Low**,
collector получает только чтение. Проверяются доверенная привязка агента/узла,
родители пути, отсутствие ссылок/reparse подмены и фактические права.
Локальная форма публикации: immutable файл под настроенным runtime control-root
`low-boundaries/<agent>/<deployment>/<instance>/<boundary_seq>-<boundary_id>.json`;
copies находятся в защищённом snapshot-root этой binding/boundary. Корни задаёт
оператор, не профиль или JSON proof. Filename не заменяет проверку содержимого.
Файл, помещённый атакующим в /journal, доказательством не является.
Удалённая доставка возможна только по согласованному аутентифицированному
runtime каналу с проверкой agent identity. Хеш удостоверяет bytes, не автора.

Администратор runtime-хоста и Docker Engine входят в доверенную базу.
Ошибка/timeout Engine, другой context/daemon, неизвестный исход операции,
утрата durable launch истории или неполный охват писателей означают unknown.
Управление только одним именем слота не доказывает отсутствие других контейнеров
с RW доступом к тому же журналу.

## 3. Порядок producer и durable барьер

1. Runtime берёт межпроцессный lock на binding и каталог. До запуска сохраняет
   launch intent с новым writer_id, выбранным Engine/context, параметрами и
   заранее выделенной идентичностью первого журнала. Один разрешённый writer;
   незавершённые create/run/stop/remove учтены долговечно.
2. Подтверждает прекращение старого writer и удаление **по полному сохранённому ID**.
   Успешно сверяет все разрешённые операции и все ресурсы с RW доступом к каталогу.
   Старый writer_id закрывает для следующего run/recovery. stopped=false/true,
   exit code или исчезновение имени этого барьера не заменяют.
3. **До выдачи нового RW доступа** копирует весь набор относящихся к прежнему
   writer журналов в собственный защищённый snapshot-каталог. Копии обычные,
   не hardlink на mutable оригинал; сохраняются все bytes, включая незавершённый
   LF-хвост. После durable записи/fsync и проверки hash/size snapshot публикуется
   атомарно. Новый Low не может изменять эти copies. Исходники остаются на месте.
4. Сохраняет и исполняет launch нового writer. Привязывает полный новый container ID
   к intent; проверяет Engine/context, параметры и право единственного писателя.
   Startup binding включает заранее выделенный journal_id/ожидаемое первое
   поколение, переданные launcher, и фактическую полную header-строку нового файла.
   Новая header проверяется против выделения/привязки и хешируется **вместе с LF**.
   Нужен согласованный startup-механизм передачи выделения writer: текущий Low CLI
   таких параметров/receipt не предоставляет. Простого поиска позднего null нет.
5. Одной durable фиксацией сохраняет завершённый boundary со ссылками на закрытый
   writer, snapshots и новую привязку. Только после commit выдаёт immutable proof.
   При crash между шагами нет готового proof; после reconciliation шаг продолжается
   или остаётся unknown. Прежний instance при этом не финализируется.

Snapshot закрепляет конкретные старые bytes, даже если новый writer способен
изменить общий исходный каталог. Сохранённые collector префиксы побайтно сверяются
со snapshot до разрешения перехода. Несовпадение — SOURCE_CHANGED, не замена raw.
Формат не доказывает полноту сетевого захвата, успешный ответ, raw receipt или право
удаления. Разрешение перехода не является разрешением cleanup.

## 4. Формат immutable proof

`schema="honeyforge-low-process-boundary/1"`; JSON object без неизвестных ключей,
дублированных ключей, NaN/Infinity. Все UUID — canonical lowercase.
Хеши/container ID — 64 lowercase hex. Все счётчики и размеры ниже — canonical
decimal **strings**, чтобы не зависеть от точности JavaScript; поля, допускающие
ноль, описаны отдельно. Timestamp — RFC3339 UTC Z, только диагностика, не причинность.
Для хранения/повторов используется канонический UTF-8 JSON без BOM/LF:
sort_keys=True, ensure_ascii=False, separators=(",",":"), allow_nan=False.
После строгой проверки полей consumer сравнивает эти canonical bytes.
Пример в samples форматирован для чтения; отступы не создают новый переход.

| Поле | Контракт |
| --- | --- |
| schema | const honeyforge-low-process-boundary/1 |
| boundary_id | UUID перехода, выделенный durable до публикации |
| boundary_seq | положительный долговечный счётчик на agent/deployment/instance; после рестарта не сбрасывается |
| previous_boundary_id | UUID предыдущего принятого перехода либо null для первого |
| publisher_epoch | UUID запуска exporter; его смена не доказывает смерть Low и не сбрасывает boundary_seq |
| agent_id, deployment_id, instance_id | доверенная привязка; для обоих writer одна |
| profile_revision | строгий integer 1..9007199254740991; исторически применённая revision, не requested |
| previous_writer | writer_id UUID, container_id, fence=container_removed, fenced_at timestamp |
| previous_journals | непустой массив всех журналов закрываемого writer в порядке journal_seq |
| previous_journals[].journal_id | UUID; уникален в массиве и согласован с header |
| previous_journals[].journal_seq | положительная decimal string; unique в binding, не новая нумерация внутри writer |
| previous_journals[].snapshot_ref | basename заранее разрешённого обычного файла защищённого snapshot-root; без /, backslash, .. и абсолютного пути |
| previous_journals[].size_bytes | decimal string >=0, точный размер копии всех bytes |
| previous_journals[].sha256 | SHA-256 копии всех bytes, включая неполный хвост |
| previous_journals[].complete_lf_end | decimal string >=0, offset сразу после последнего LF, <=size_bytes; сам по себе не доказывает валидность JSON |
| previous_journals[].footer_present | strict boolean, результат структурной проверки; false не значит пустой журнал |
| next_writer | другой writer_id и другой полный container_id, привязанные runtime к launch intent |
| next_writer.first_journal | journal_id, journal_seq, previous_journal_id=null, first_seq="1", header_sha256 |
| committed_at | время durable фиксации, не время повторной публикации |

Engine/context и launch intent идентичность хранятся в durable runtime registry,
из которого producer строит proof; недоверенные поля документа их не заменяют.
Детали такого registry — внутренняя реализация runtime после согласования,
не разрешение использовать старый неподтверждённый instances.json proposal.

Номер первого нового поколения продолжает общую нумерацию instance.
Для первого MVP требуется last previous journal_seq + 1; если есть пропуск,
неизвестный файл, параллельная цепь или повтор номера, переход ждёт отдельного
разрешения. Первый process seq новой цепочки =1. Boundary не переименовывает journal.

[Полный пример](samples/low-process-boundary/boundary-example.json) содержит
настоящие journal bytes/hash из наблюдения 11.10 и **синтетические** runtime
writer/container/boundary identities и timestamps. Это пример формы для теста,
не emitted proof и не разрешение collector использовать репозиторную фикстуру
как доверенный runtime control-файл. [manifest](samples/low-process-boundary/manifest.json)
отдельно отмечает происхождение каждого поля.

## 5. Правила consumer и replay

До согласования и реализации действуют прежние waiting/MISSING_PREDECESSOR и
OPEN_PREDECESSOR; файлы/checkpoints/ledger сохраняются. Уже проверенные записи
доступны conversion/delivery, включая поздние события известных завершённых
instances по прежней исторической авторизации.

Владелец collector добавляет принятие proof со следующими условиями:

- Проверены источник доверия, binding/revision, оба writer, предыдущий boundary,
  возрастающий счётчик и отсутствие конфликтующих intents/resources.
- Для второго и следующих переходов previous_writer.writer_id/container_id
  в точности совпадают с next_writer последнего принятого boundary. Первый
  переход проверяется против уже доверенного исходного launch/writer anchor.
  Все previous_journals принадлежат этому anchor; новые writer/container/journal
  IDs ранее не использовались в binding. Неизвестный anchor, повтор закрытого
  writer или разрыв истории блокируют переход; null/header историю не создают.
- Snapshot headers/полный hash/size/LF-граница совпадают; прежний сохранённый
  префикс не меняется. Tail сохраняется как opaque bytes, без Event для неполной
  строки. Каждая завершённая строка всё равно проходит действующий validator.
- Новый первый journal/header hash/allocated identity совпадают, нет чужой
  цепочки. Producer подтверждает именно этот переход, не любой будущий null.
- Потребление boundary и новый chain anchor сохраняются атомарно с согласованным
  состоянием raw. При crash повтор безопасен; boundary не расходуется до commit.
- На подтверждённой границе сбрасывается только **межфайловая проверка process seq**:
  next first_seq=1 допустим после прежнего seq=2/9. Checkpoints старых журналов,
  source_seq, raw, ledger/mapper version/Event IDs и уникальность journal_seq
  остаются прежними. Простой обход проверки previous недостаточен.
- Повтор того же boundary с теми же canonical bytes идемпотентен; другой payload под
  тем же boundary_id, rollback/перестановка/развилка, конфликт writer ID или
  утрата durable consumer anchor блокируют новые решения. Epoch/время не заменяют
  историю. После рестарта сначала reconciliation, затем новые подтверждения.

Boundary уже принятого и сохранённого перехода не истекает по TTL.
Это доказательство прежнего durable действия, а не свежий writer_alive snapshot.
Изменение текущей жизни Low не отменяет ранее принятый raw. При неизвестном новом
состоянии запрещены новые переходы, существующий прогресс не перематывается.

Для диагностики сохраняются существующие collector коды. Дополнительные причины
предлагаются как внутренние: PREVIOUS_WRITER_NOT_FENCED, NEXT_WRITER_UNBOUND,
RUNTIME_OBSERVATION_UNKNOWN, BOUNDARY_BINDING_MISMATCH, BOUNDARY_SNAPSHOT_UNAVAILABLE,
BOUNDARY_SEQUENCE_CONFLICT. Их wire/API размещение согласует владелец collector;
новый endpoint здесь не определяется. Операторский текст для позднего null:
«Ожидание подтверждения границы процесса Low». В диагноз не включаются raw/секреты.

## 6. Воспроизводимая фикстура и совместный тест

В samples/low-process-boundary/ сохранены точные небольшие журналы реальных двух
Python Low процессов на loopback из 11.10. Первый завершён аварийно без footer,
второй запущен с прежним instance: journal_seq=2, previous=null, first_seq=1.
Это ранее выполненное наблюдение writer, не Docker или trusted boundary test.

| Исходник | Размер / LF-конец | SHA-256 |
| --- | --- | --- |
| old-crash.jsonl | 885 / 885 | 48491825af8ebe71078cee42d9fde01de2f95d044f71618623c82b1852416b8e |
| new-process.jsonl | 889 / 889 | 461bc062bfd51fc95bdcdcba0b9d08fabde4c6143807709fe3ed2b2caabb2137 |
| old-with-tail.jsonl | 910 / 885 | ee98c6080d62239d899514cfee4316306cfeb116b89d3db00d194d271612c422 |

Последний файл — контролируемая **синтетическая** добавка 25 bytes к старому
журналу. SIGKILL не обязан оставлять воспроизводимый неполный хвост. Для этого
варианта пример proof меняет snapshot_ref/size/hash, сохраняет complete_lf_end=885.
Тест не дописывает footer и не меняет старые строки.

1. На согласованном Docker-стенде writer A/instance I пишет несколько событий.
   Сборщик сохраняет raw/checkpoints, mapper — ledger/Event IDs. Необработанная
   проверенная строка остаётся доступной conversion после остановки A.
2. Завершить **собственную** жизнь A аварийно; проверить отсутствие footer.
   Для отдельной tail-фикстуры сохранить все 910 bytes, LF-checkpoint=885.
3. Runtime выполняет producer шаги 1–4: fenced A, durable snapshots до нового RW,
   затем один запуск и startup binding B с прежним I, более высоким journal_seq,
   previous=null, seq=1. Окончательный commit/export proof пока удерживается.
   Collector уже видит B, но proof отсутствует: ожидать MISSING_PREDECESSOR и причину,
   ноль новых записей B, старые checkpoints/IDs сохранены; verified A конвертируется.
4. Runtime выполняет только producer шаг 5 — commit/export готового proof;
   повторного запуска B нет. На тестовом стенде удержание export — управляемая
   точка fault injection, не разрешение выдавать RW до snapshot.
   Consumer проверяет protected provenance и hashes, читает валидные полные
   строки A из snapshot, сохраняет tail, принимает B с отдельными offsets.
5. Повторить proof/чтение/ACK и рестарт runtime/collector: ни повторного pending,
   ни новых Event ID на прежних позициях, ни сброса source_seq/raw.
6. Сравнить публичное отображение: capture_completeness=unknown, для HTTP
   «ответ не записан»; proof/footer/snapshot не создают HTTP 200.

Обязательные отрицательные варианты: A жив; второй RW writer; spoof proof в Low
mount; неверный binding/revision/Engine/header/journal/container ID; тот же PID или
имя при другой жизни; повреждённая copy; rollback/перестановка boundary; потеря
durable registry; daemon timeout; crash до/после каждого producer/consumer commit;
рестарт только агента при живом Low; docker restart того же ID без supervisor.
При отсутствии доказательства новый переход запрещён, старые данные доступны.

## 7. Ответственность и готовность

Данил: согласовать и реализовать durable writer identity/fencing/snapshots/startup
binding/exporter и runtime негативные тесты. Дима: consumer/proof validation,
атомарность anchors/raw, seq boundary, replay и collector integration.
Backend: historical binding/auth channel; Диана: отображение ожидания и unknown
по уже согласованному DTO. Ничего не удаляется автоматически.

Готовность требует согласования точного формата/канала/прав и операции restart,
producer+consumer реализации в согласованных PR, полного совместного Docker
прогона с отрицательными случаями, актуального main/CI/трёх рецензентов и человека.
Документ и sample consistency checks не подтверждают эти невыполненные пункты.
