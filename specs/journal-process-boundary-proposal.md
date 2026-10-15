# Low: доверенная граница процесса в прежнем instance

Статус 15.10.2026: **сторона runtime реализована, сторона collector — нет**.
Формат, совместная фикстура и ответы на вопросы consumer согласуются прежним
порядком; producer, startup-выделение Low CLI, durable registry, снимки и
локальная публикация write-once реализованы в этом PR (§9). Со стороны сборщика
в main уже есть изолированный слой хранения подготовленного перехода
([low-boundary-storage.md](low-boundary-storage.md)), но доверие, файловая
валидация, начальная авторизация и правила чтения перехода там прямо объявлены
отдельным изменением и не реализованы. Поэтому до них действуют прежние
waiting/MISSING_PREDECESSOR и обработка проверенного raw.
Основание: head `01599aede963b0903c9dc9b805bdae5068268ba7`, main
`01f8272ba37ef16af7b905042b00a7a1d36808ca`; предыдущее основание — PR #39,
`20f19d2ce3c1aae6ec79205a971a6d358260dfb1`.
Mapping/DTO/UUID из PR #39 сохраняются: process seq — private data.low_seq;
эта спецификация не вводит альтернативный Event DTO.
Значения §4 и §8 становятся контрактом только после согласования с Димой
и Кирито: реализация runtime их не утверждает за обе стороны. Полный совместный
прогон на согласованном Linux-стенде Docker не выполнялся (§9.4).

## 1. Что уже есть и чего не хватает

PR #39 добавляет LowProductionMapper и collect_low. Поздний
previous_journal_id=null после первого поколения даёт waiting/MISSING_PREDECESSOR
до сохранения новой цепочки, с footer у старого файла или без него.
OPEN_PREDECESSOR относится к обычной связанной ротации с открытым predecessor.
collect_low запускает conversion уже проверенного raw независимо от этого waiting.

Runtime сохраняет ID своей контейнерной попытки и применяет полный Docker ID для
stop/remove. Рестарт агента, PID, StartedAt, смена filename, O_EXCL, seq reset и
поздний null сами не доказывают границу процесса: поэтому durable writer identity,
ограждение писателя, startup binding и экспорт документов выполняет отдельная
операция runtime (§9), а не обычный путь start исполнителя команд.

Прежнее предложение writer_finalized закрывало **весь instance** и запрещало
следующий запуск этого UUID. Для обсуждаемого случая нужны два writer_id внутри
одного instance и закрытие только старого writer. Журнал остаётся
honeyforge-low-journal/1; никакого фиктивного footer не добавляется.

## 2. Поддерживаемый вариант и граница доверия

Предлагаемый первый вариант: runtime контролируемо удаляет старый контейнер и
создаёт **другой контейнер** с прежними agent/deployment/instance/revision и новым
writer_id. Один writer_id относится к одной разрешённой жизни контейнера:
повторный запуск удалённого writer запрещён durable fencing записью.
Существующая command start/recovery не объявляется разрешением такого перезапуска:
такой перезапуск выполняет отдельная внутренняя операция runtime (§9.1), которую
исполнитель команд центра не вызывает.

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
Раскладка, владельцы, права и протокол публикации — §8.2, snapshots — §8.3.
Файл, помещённый атакующим в /journal, доказательством не является.
Удалённая доставка возможна только по согласованному аутентифицированному
runtime каналу с проверкой agent identity; в MVP её нет. Хеш удостоверяет
bytes, не автора.

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
decimal **strings** без ведущих нулей и не больше 9007199254740991, чтобы
не зависеть от точности JavaScript; поля, допускающие ноль, описаны отдельно. Timestamp — RFC3339 UTC Z, только диагностика, не причинность.
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
| publisher_epoch | UUID запуска exporter, выполнившего durable commit; повторная публикация его не меняет (§8.4); смена epoch не доказывает смерть Low и не сбрасывает boundary_seq |
| agent_id, deployment_id, instance_id | доверенная привязка; для обоих writer одна |
| profile_revision | строгий integer 1..9007199254740991; исторически применённая revision, не requested |
| previous_writer | writer_id UUID, container_id, fence=container_removed, fenced_at timestamp |
| previous_journals | непустой массив всех журналов закрываемого writer в порядке journal_seq |
| previous_journals[].journal_id | UUID; уникален в массиве и согласован с header |
| previous_journals[].journal_seq | положительная decimal string; unique в binding, не новая нумерация внутри writer |
| previous_journals[].snapshot_ref | basename заранее разрешённого обычного файла защищённого snapshot-root (§8.3); без /, backslash, .. и абсолютного пути; producer использует имя исходного файла Low |
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
  переход проверяется против начальной привязки writer (anchor, §8.1).
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
BOUNDARY_SEQUENCE_CONFLICT, BOUNDARY_PUBLICATION_CONFLICT (§8.2) и общий с consumer
LIMIT_EXCEEDED (§8.5). Их wire/API размещение согласует владелец collector;
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

Шаги выполняются стендом `python -m infra.runtime.boundary_stand` (§9.3): он
вызывает настоящий producer на настоящем Docker Engine и печатает пути
опубликованных документов. Этапы стенда названы ниже в скобках.

1. На согласованном Docker-стенде writer A/instance I пишет несколько событий
   (`--stage=anchor` выдаёт anchor и печатает полный container ID A).
   Сборщик сохраняет raw/checkpoints, mapper — ledger/Event IDs. Необработанная
   проверенная строка остаётся доступной conversion после остановки A.
2. Завершить **собственную** жизнь A аварийно; проверить отсутствие footer.
   Стенд этого не делает сам: участник завершает процесс выбранным способом,
   например `docker kill --signal KILL <container>`, поэтому хвост остаётся
   честно аварийным. Для отдельной tail-фикстуры сохранить все 910 bytes,
   LF-checkpoint=885.
3. Runtime выполняет producer шаги 1–4 (`--stage=hold`): fenced A, durable
   snapshots до нового RW, затем один запуск и startup binding B с прежним I,
   более высоким journal_seq, previous=null, seq=1. Окончательный commit/export
   proof пока удерживается.
   Collector уже видит B, но proof отсутствует: ожидать MISSING_PREDECESSOR и причину,
   ноль новых записей B, старые checkpoints/IDs сохранены; verified A конвертируется.
4. Runtime выполняет только producer шаг 5 (`--stage=finish`) — commit/export
   готового proof; повторного запуска B нет. На тестовом стенде удержание export —
   управляемая точка fault injection, не разрешение выдавать RW до snapshot.
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
рестарт только агента при живом Low; docker restart того же ID без supervisor;
отсутствующий, подложенный или второй anchor; повторная публикация с другими bytes;
больше 64 журналов, tail больше 65535 bytes, нехватка snapshot или Low квоты (§8.5).
При отсутствии доказательства новый переход запрещён, старые данные доступны.
Какие из этих вариантов уже закрыты автотестами runtime и какие остаются за
стендом и consumer — в §9.4. Прохождение автотестов не заменяет совместный прогон.

## 7. Ответственность и готовность

Данил: durable writer identity/fencing/snapshots/startup binding/exporter и runtime
негативные тесты — **выполнено** в этом PR (§9). Дима: consumer/proof validation,
атомарность anchors/raw, seq boundary, replay и collector integration — **не
выполнено**. Backend: historical binding/auth channel; Диана: отображение ожидания
и unknown по уже согласованному DTO. Ничего не удаляется автоматически.

Готовность изменения к слиянию остаётся частичной. Выполнено: изменение Low CLI
для startup-выделения, реализация producer с автотестами, проверка прав и
публикации на Linux, проверка примитивов Engine на настоящем Docker (§9.4).
Не выполнено: согласование точного формата, канала и прав с Димой и Кирито,
реализация consumer в его PR, полный совместный Docker прогон §6 с отрицательными
случаями и сборщиком, а также финальное одобрение человеком актуальной версии.
Документ, автотесты и sample consistency checks эти пункты не подтверждают.

## 8. Ответы runtime на вопросы consumer

Ответ Данила на §8 consumer proposal Димы от 14.10.2026 (рассмотрена версия
SHA-256 `e827834ed5f47ff43b655dd0e6aaf6e2fb4bddb40b14f28630de8e7ae6dc51fa`).
Producer, изменение Low CLI, registry и проверки прав **реализованы** (§9).
Значения всё равно становятся контрактом только после согласования с Димой
и Кирито: реализация одной стороны соглашения не заменяет. До согласования
и реализации consumer действуют прежние waiting и обработка verified raw.

### 8.1 Начальная доверенная привязка writer

**Источник.** Anchor выдаёт только runtime producer при **первом** запуске нового
instance тем же durable порядком, что шаги 1, 4 и 5 §3, без fencing и snapshots.
Основание anchor — сохранённый до запуска launch intent (anchor_id, writer_id,
launch_intent_id, выделенный journal_id первого поколения, Engine/daemon),
полный container ID из ответа Engine на create, сохранённый до start, и фактическая
header нового файла. Command ACK, attempt ExecutionStore, PID, имя контейнера или
файла, header без intent, первый boundary и накопленный spool источником не являются.

**Startup-выделение** реализовано в CLI Low (§9.2) и согласуется этим документом:

- `--journal-first-id=<uuid>` — journal_id первого поколения этого процесса;
  следующие поколения Low выбирает сам, как сейчас.
- `--journal-first-seq=<n>` — ожидаемый journal_seq первого поколения. Если номер,
  вычисленный Low (максимум своего instance в каталоге + 1), другой или файл уже
  существует, Low завершается с ошибкой до открытия слушателей.

Формат honeyforge-low-journal/1 не меняется: writer_id в header не пишется,
связь writer ↔ journal хранит runtime registry. Для нового instance ожидаются
journal_seq=1, previous_journal_id=null и first_seq=1.

**Привязка header.** Runtime открывает на хосте
`<journal_root>/<deployment>/low-<instance>-000001.jsonl` без перехода по ссылкам,
требует обычный файл и в пределах start_timeout читает не более 4096 bytes до
первого LF. Все поля header совпадают с выделением и binding; SHA-256 считается
по header **вместе с LF**. Тайм-аут или несовпадение: anchor не выдаётся.

**Формат.** `schema="honeyforge-low-writer-anchor/1"`; правила типов,
канонизации и сравнения bytes те же, что у proof (§4).

| Поле | Контракт |
| --- | --- |
| schema | const honeyforge-low-writer-anchor/1 |
| anchor_id | UUID, выделен в launch intent до запуска; повторно не используется |
| publisher_epoch | UUID exporter, выполнившего commit; повторная публикация его не меняет |
| agent_id, deployment_id, instance_id | доверенная привязка; совпадает у всех boundary этого instance |
| profile_revision | строгий integer 1..9007199254740991, как в proof |
| writer_id | UUID первого writer instance |
| container_id | 64 lowercase hex, полный ID из ответа Engine на create |
| launch_intent_id | UUID durable intent для аудита runtime; consumer проверяет только формат |
| first_journal | journal_id, journal_seq="1", previous_journal_id=null, first_seq="1", header_sha256 |
| committed_at | время durable commit, только диагностика |

**Использование consumer.** Первый boundary (`boundary_seq="1"`,
`previous_boundary_id=null`) допустим, только если previous_writer.writer_id и
container_id равны anchor, а первый элемент previous_journals совпадает с
anchor.first_journal по journal_id/journal_seq и header hash snapshot. До первого
перехода счётчик равен 0. Instance имеет ровно один anchor; другой anchor того же
instance — конфликт, а не замена.

**Ограничения.** Anchor выдаётся только instance, запущенному после включения
producer. Bootstrap уже работающего или накопленного instance запрещён: restart
operation для него отказывает, runtime использует обычный новый instance.
Ошибка выдачи anchor не меняет результат обычного start: instance работает,
но boundary для него не поддерживается. Рестарт агента или exporter при живом
Low anchor не меняет. [Пример anchor](samples/low-process-boundary/anchor-example.json)
использует header old-crash.jsonl и синтетические runtime identities (как proof):
canonical 780 bytes, SHA-256
`21f9d105fd1461e8b7cc96ff2cfd381d1bff9f9dcc95f4aec158bc38453bd5a3`.

### 8.2 Канал и права публикации

**Канал MVP** — только локальная файловая публикация на runtime-хосте; collector
работает на том же хосте. Anchor и proof не передаются через Event, command ACK,
telemetry batch или API центра. Удалённая доставка — отдельное согласование
с backend. Гарантии ниже определены для Linux; на другой ОС producer выключен
и не выдаёт anchor/proof, поэтому collector остаётся в waiting.

**Корни.** `control_root` и `snapshot_root` — абсолютные пути из настроек
оператора runtime; collector получает те же значения из своей конфигурации
оператора. Профиль, центр, proof и journal пути не задают. Корни не вложены
в journal_root и не содержат его. Runtime отказывает в BindMount любого
контейнера, источник которого пересекается с корнями.

```text
<control_root>/low-boundaries/<agent>/<deployment>/<instance>/anchor.json
<control_root>/low-boundaries/<agent>/<deployment>/<instance>/<boundary_seq>-<boundary_id>.json
<snapshot_root>/low-snapshots/<agent>/<deployment>/<instance>/<boundary_id>/<snapshot_ref>
```

Номер в имени — canonical decimal без ведущих нулей. Порядок задают содержимое
и история, не сортировка имён. Файл содержит точные canonical bytes без LF.

**Пользователи.** R — runtime producer, единственный писатель корней. C — collector,
член группы G с правом только чтения. L — пользователь контейнеров ловушек без
доступа к корням. R, C и L различны; root и Docker Engine входят в доверенную базу.

| Объект | Владелец | Режим |
| --- | --- | --- |
| Корни и каталоги binding | R:G | 0750 |
| Опубликованные anchor, proof, snapshots | R:G | 0440, ровно одна жёсткая ссылка |
| Опубликованный каталог snapshots boundary | R:G | 0550 |
| Временные `.tmp-*`, `.staging-*` до готовности | R:R | 0600 / 0700 |
| Родители корней до `/` | root или R | без записи группы и остальных |

**Публикация (write-once).** В целевом каталоге создаётся `.tmp-<uuid>` с
O_CREAT|O_EXCL|O_NOFOLLOW и режимом 0600; записываются все bytes, затем fsync,
fchown на группу G и fchmod 0440. Группа и окончательный режим назначаются
временному имени непосредственно перед переименованием, когда содержимое уже
полное: имена с точкой collector не читает, а отдельного объекта под окончательным
именем в этот момент ещё не существует. То же относится к `.staging-*` каталогу,
который получает 0550 перед своим переименованием; уборка после отказа возвращает
ему приватный режим, иначе она сама падала бы по правам. Окончательное имя появляется атомарно и
**без замены** через renameat2 RENAME_NOREPLACE, затем синхронизируется каталог.
Если ФС не поддерживает RENAME_NOREPLACE, producer выключен: замена через
rename и обходные link-схемы не используются. Если имя уже есть: те же bytes —
успешный повтор, другие — BOUNDARY_PUBLICATION_CONFLICT без перезаписи; новые
переходы binding запрещены до разбора. Имена с точкой в начале collector не читает.

**Проверки collector.** Путь открывается цепочкой openat от настроенного корня
с O_NOFOLLOW/O_DIRECTORY. Каждый каталог — не ссылка, владелец root или R, без записи
группы и остальных. Файл обычный, владелец R, без битов записи, одна жёсткая
ссылка, размер в пределах §8.5; читается целиком из одного дескриптора.
Collector не может удалить или переименовать файл: записи в каталоги у него нет.
Если collector работает в контейнере, корни монтируются read-only **каталогами**,
не отдельными файлами, с теми же uid/gid. Права, renameat2 и fsync проверяются
на целевом Linux-стенде; сейчас это не подтверждено.

### 8.3 Неизменяемость и хранение snapshots

**Перечень.** Snapshots создаются на шаге 3 §3, после fencing и до нового RW.
Журналы закрываемого writer — файлы `low-<instance>-<n>.jsonl` каталога слота
от первого журнала writer (anchor.first_journal или next_writer.first_journal
последнего boundary) до максимального номера instance. Цепочка previous и
journal_seq проверяется без пропусков, повторов и развилок. Промежуточные
поколения без footer (ошибка записи или footer) — обычный путь Low; они входят
как есть с footer_present=false. Пропуск номера, файл без полной header,
чужая header или неизвестное имя с префиксом instance блокируют переход
(BOUNDARY_SNAPSHOT_UNAVAILABLE).

**Копирование.** Исходник открывается O_RDONLY|O_NOFOLLOW; размер S берётся fstat,
копируются ровно S bytes с подсчётом SHA-256. Повторный fstat должен показать
тот же размер и mtime, иначе SOURCE_CHANGED и переход unknown: значит, ограждение
неполно. Копия — новый обычный файл: не hardlink, не symlink, не bind mount и не
reflink. После fsync, fchown на группу G и fchmod 0440 копия перечитывается
новым дескриптором и хеш сверяется. Все копии создаются в `.staging-<boundary_id>`;
каталог получает R:G 0550, затем атомарно и без замены получает имя
`<boundary_id>` (§8.2), после чего родитель синхронизируется. Перечень
snapshot_ref, size, sha256, complete_lf_end, footer_present и длина хвоста
фиксируется в registry вместе с состоянием. Исходники остаются на месте.
Число журналов и размеры по §8.5 проверяются после fencing до копирования,
хвосты — по копиям; всё это — до запуска нового writer.

**Неизменяемость.** Имена write-once. R не открывает опубликованные snapshots на
запись и не переименовывает их; L не имеет пути к корням; C только читает.
chattr +i допустим как дополнительная мера, но не требуется. От компрометации
R или root гарантии нет: это доверенная база. Поэтому consumer перед первым
использованием проверяет полный размер и SHA-256. Несовпадение — ошибка
целостности без возврата к изменяемому оригиналу.

**Сверка копии.** Размер, SHA-256, граница полных строк и наличие footer копии
сверяются с измерением, сделанным при перечислении цепочки. После ограждения
исходник меняться не может, поэтому расхождение означает неполное ограждение:
переход становится неизвестным (SOURCE_CHANGED), и новые переходы instance
запрещаются до решения оператора. Повторный fstat внутри копирования покрывает
только изменение во время чтения и этой сверки не заменяет.

**Хранение.** В MVP нет TTL и автоматического удаления anchors, proofs, snapshots
и registry: они переживают ACK, принятие consumer и рестарты. Исходные журналы
Low в каталоге слота тоже не удаляются и не перемещаются. Удаление возможно
только отдельно согласованной операцией cleanup, которая здесь не определена.
Принятие boundary, ACK и Event такого разрешения не дают. Повторная публикация
использует существующий набор без новой копии и двойного учёта места.

**Место.** Snapshot_root имеет отдельную квоту оператора с учётом staging.
Producer проверяет её до fencing по текущим размерам и повторно после fencing
по фактическим. При нехватке snapshots не создаются, новый writer не
запускается, proof нет (BOUNDARY_SNAPSHOT_UNAVAILABLE); данные остаются на месте.
Проверяются и фактическое свободное место файловой системы, и квота оператора,
если она задана (`--snapshot-quota-bytes` стенда). Предварительная проверка идёт
до durable intent: нехватка места не расходует идентичности и не оставляет
незавершённого перехода. Пока писатель жив, набор может быть неполным или
меняться — тогда предварительная проверка пропускается, и решение принимает
повторная после ограждения. Нехватка места во время записи копии тоже даёт
BOUNDARY_SNAPSHOT_UNAVAILABLE, а не необработанную ошибку ввода-вывода.

### 8.4 Восстановление producer и неизменность повторного proof

**Registry** — SQLite в приватном каталоге состояния runtime (R, 0700), вне корней
публикации и недоступная collector. Он хранит intents, их состояния, полные
container ID, идентичность Engine/daemon, выделения, перечень snapshots,
сожжённые идентичности и **точные canonical bytes** выданных anchor/proof с SHA-256.
Каждое состояние фиксируется транзакцией до соответствующего внешнего действия.

| Состояние | Восстановление после сбоя |
| --- | --- |
| intent | Inspect старого writer по полному ID. Существует в любом состоянии — продолжить stop/remove. Отсутствует на том же daemon — проверить RW mounts всех контейнеров к каталогу слота и перейти в fenced. Ошибка Engine или другой daemon — ожидание unknown без продвижения. |
| fenced | Ограждение подтверждается наблюдением заново: durable запись сделана в прошлом, а появившийся с тех пор писатель с RW доступом сделал бы снимок доказательством чужих байт. Незавершённый `.staging-<boundary_id>` удаляется, копирование повторяется: исходники неизменны, нового RW не было. Если каталог снимков уже опубликован, а перечня в registry нет (сбой между двумя транзакциями), опубликованный набор перенимается со сверкой байтов цепочки: копии write-once и повторное копирование невозможно. Контейнер с меткой нового writer_id здесь — противоречие и unknown. |
| snapshots_published | Сверить опубликованный набор с registry. Create без сохранённого ID ищется по метке writer_id: ровно один — привязать, несколько — unknown. |
| next_created | Engine сообщает, что контейнер ни разу не запускался, и файла первого журнала нет — удалить его по полному ID, сжечь writer_id и journal_id, повторить launch с новыми. Иначе продолжить start. |
| next_started | Ждать header (§8.1). Тайм-аут, несовпадение или выход процесса — остановить и удалить новый контейнер по полному ID; состояние failed_after_launch. |
| next_bound | Commit: boundary_seq = последний committed + 1, previous_boundary_id, committed_at и текущий publisher_epoch; canonical bytes и их SHA-256 сохраняются в той же транзакции. |
| committed | Публикация по §8.2; существующее имя с теми же bytes — успех. |
| exported | Сверить опубликованный файл с registry. Пропал — опубликовать те же bytes; отличается — BOUNDARY_PUBLICATION_CONFLICT без перезаписи. Сверка выполняется без незавершённого intent: reconciliation проверяет anchor и последний boundary привязки по сохранённым байтам. Утрата самих сохранённых байт — REGISTRY_UNAVAILABLE, собирать документ заново запрещено. |

Anchor проходит те же состояния, кроме fenced и snapshots_published: в intent
старый writer не проверяется, create ищется по метке writer_id. Если в
next_started header не получена или не совпала, anchor не выдаётся и instance
помечается как не поддерживающий boundary; судьбу контейнера решают обычные
правила start, producer его ради anchor не удаляет.

**Граф состояний.** Разрешены только соседние рёбра своего вида:

```text
anchor:   intent → next_created → next_started → next_bound
boundary: intent → fenced → snapshots_published → next_created
                 → next_started → next_bound
```

Состояния `committed`, `exported` и `failed_after_launch` этим графом не
достигаются: их устанавливают фиксация, публикация и закрытие после запуска.
Иначе скачок выдавал бы документ в обход create, start или ограждения, а anchor
получал бы стадии перехода. Возврат после снятой попытки — отдельное повторное
выделение: anchor возвращается в intent, переход — в snapshots_published, прежние
UUID остаются сожжёнными.

Каждое ребро закрепляет ровно свои поля и только пока они не заполнены:
`fenced` — время ограждения, `snapshots_published` — номер первого поколения
нового writer, `next_created` — полный container ID, `next_bound` — SHA-256
фактической header; `next_started` своих полей не имеет. Поле чужой стадии,
отсутствующее обязательное поле и попытка переписать уже закреплённое значение
отклоняются до SQL. Закрытие после запуска допустимо только со стадий, где RW
новому writer уже выдавался (`next_created`, `next_started`), и только пока
документ не зафиксирован: иначе поздний вызов блокировал бы привязку с уже
выданным доказательством.

- На binding допускается один незавершённый intent. Межпроцессный lock берётся
  до reconciliation; новые операции слота принимаются только после неё.
- boundary_seq выделяется только при commit: сбойные intents номер не расходуют.
  boundary_id, writer_id, journal_id и anchor_id из intent повторно не используются
  даже после отказа.
- failed_after_launch и unknown после выдачи RW новому writer автоматически
  не продолжаются и не порождают новый переход: его цепочка не привязана,
  collector ждёт. Дальше — новый instance или отдельное решение оператора.
  Данные не удаляются.
- **Неизменность повтора.** Canonical bytes создаются один раз в транзакции commit
  и никогда не собираются заново из полей. Любая повторная публикация (рестарт,
  пропавший файл, запрос оператора) пишет сохранённые bytes; publisher_epoch и
  committed_at в них — исходного commit. Новый epoch попадает только в документы,
  зафиксированные после рестарта.
- Потеря registry или сохранённых bytes: новые anchor/proof для binding не
  выдаются (RUNTIME_OBSERVATION_UNKNOWN). Уже опубликованные файлы остаются
  действительными. Восстанавливать историю по каталогу запрещено.
- Reconciliation при живом Low не останавливает и не перезапускает его и не
  меняет anchor или последний boundary.

### 8.5 Лимиты

| Лимит consumer | Позиция runtime | Основание |
| --- | --- | --- |
| Proof до 1 MiB | Подтверждаю; тот же предел для anchor. Producer не фиксирует документ больше лимита (LIMIT_EXCEEDED) и не обрезает его. Опубликованный файл равен canonical bytes, поэтому предел одинаков до и после разбора. | Пример с одним журналом — 1268 bytes, anchor — 780. Худший случай: 64 журнала и 16-значные числа — 22540 bytes. |
| До 64 журналов на boundary | Подтверждаю как предел MVP на весь boundary: один закрываемый writer не делится. Producer считает журналы после fencing и до копирования; больше 64 — переход останавливается до нового RW, новый writer не запускается, данные на месте. | Low не ограничивает число поколений writer: ротации и ошибки записи создают новые. По умолчанию 256 MiB / 8 MiB — около 32 полных поколений; при 256 KiB / 256 MiB возможно около 1024, и переход упрётся в предел. |
| Суммарный хвост до 4 MiB | Подтверждаю. Producer до запуска нового writer проверяет хвост каждого snapshot не больше 65535 bytes; больше — переход блокируется. Тогда 64 × 65535 < 4 MiB. | Хвост бывает только в конце файла: после ошибки записи Low закрывает файл. При запуске из профиля max_payload_bytes ≤ 8192 и строка события около 12 KiB (low-journal-v1 §6); footer меньше 512 bytes. |

Low обращён к атакующему, поэтому его собственные пределы не считаются
доказательством. Дополнительно producer **проверяет** до запуска нового writer:

- Один snapshot не больше journal_max_bytes, с которым запускался закрываемый
  writer (хранится в его intent), а их сумма не больше его journal_quota_bytes
  (по умолчанию 8 MiB и 256 MiB). Превышение — LIMIT_EXCEEDED, переход стоит.
  Сумма может быть больше max_scan_bytes=64 MiB collector: consumer нужна
  возобновляемая подготовка с cursor, привязанным к boundary_id, snapshot_ref
  и sha256. Runtime гарантирует, что эти файлы не меняются.
- Числовые строки anchor/proof canonical и не больше 9007199254740991;
  иначе producer документ не выдаёт.
- Исходники старого writer остаются в каталоге слота и учитываются Low квотой.
  До запуска нового writer producer требует свободного места в ней не меньше
  journal_max_bytes, иначе новый writer не запускается (LIMIT_EXCEEDED);
  snapshots и данные сохраняются, оператор увеличивает квоту или согласует cleanup.
- Collector шаг с max_files=64 считает пути каталога. После перехода там лежат
  до 64 журналов старого writer и новые журналы. Закреплённые snapshots журналы
  нужно исключать из выборки шага, иначе FILE_LIMIT. Это задача consumer;
  runtime исходники не удаляет.

## 9. Реализация стороны runtime

Реализована сторона producer. Со стороны collector в main есть изолированный слой
хранения подготовленного перехода (`agent/telemetry/low_journal/boundary.py`,
[low-boundary-storage.md](low-boundary-storage.md)): он сохраняет неизменяемые
canonical proof bytes, идентичности обоих writer, закрепление журналов и хвосты,
но сам прямо указывает, что доверие, файловая валидация, начальная авторизация и
правила чтения перехода будут сделаны отдельно. Принятие proof, атомарность
anchors/raw, сброс межфайловой проверки process seq и replay остаются за
владельцем collector (§5), и до его следующего PR поведение сборщика не меняется.

Состав полей совпадает: `PreparedTransition` того слоя требует boundary_id, seq,
previous_id, canonical proof bytes, идентичности закрываемого и нового writer с их
контейнерами, набор журналов с journal_id, journal_seq, snapshot_ref, размером,
digest, LF-границей и footer_present, заголовок нового поколения и его journal_seq
— всё это несёт документ §4. Пределы тоже совпадают: proof до 1 MiB, до 64
журналов, суммарный хвост до 4 MiB (`BoundaryLimits`). Связи по коду между
пакетами нет: producer публикует файлы, а подготовку и валидацию выполняет
сторона сборщика.

### 9.1 Модули и операция

`agent/runtime/boundary/` содержит только producer и его наблюдения:

| Модуль | Ответственность |
| --- | --- |
| `documents.py` | Канонические anchor и proof: строгая проверка полей §4 и §8.1, десятичные строки, байты sort_keys/UTF-8 без BOM и LF, пределы §8.5. Разбор обратен сборке. |
| `journals.py` | Чтение каталога слота: полная строка заголовка с LF, SHA-256 всех байт, граница полных строк, структурная проверка footer, цепочка поколений без пропусков, повторов и развилок, занятое место квоты Low. |
| `registry.py` | SQLite в приватном каталоге (0700, межпроцессный замок, synchronous=FULL): intents и их состояния, полные container ID, идентичность Engine, выделения, перечень снимков, сожжённые идентичности и **точные канонические байты** выданных документов. |
| `snapshots.py` | Копирование журналов закрываемого писателя в `.staging-<boundary_id>` и атомарная публикация каталога перехода; обычные копии, сверка размера и mtime исходника до и после, перечитывание копии новым дескриптором. |
| `publication.py` | Локальная публикация write-once: цепочка `openat` от настроенного корня без переходов по ссылкам, `.tmp-<uuid>` 0600, затем fsync, группа G, режим 0440, `renameat2(RENAME_NOREPLACE)` и fsync каталога. |
| `launcher.py` | Одна жизнь контейнера-писателя: `create` с сохранением полного ID до `start`, метка `honeyforge.writer_id`, startup-выделение в аргументах Low, ожидание и сверка фактического заголовка, уборка непривязанной попытки. |
| `producer.py` | Порядок шагов §3, фиксация, публикация и восстановление §8.4. |

Операция вызывается отдельно и **не входит** в путь исполнителя команд центра:
`BoundaryProducer.launch_first_writer` — первый запуск instance с выдачей anchor,
`restart_writer` — переход писателя (с `hold_export` для fault injection §6),
`finish` — только шаг 5, `reconcile` — продолжение незавершённого intent,
`republish` — сверка опубликованных документов привязки с сохранёнными байтами.
Назначение центра и известность истории проверяются **до** ограждения и снимков;
`restart_writer` при готовом удержанном переходе доводит его и возвращает результат,
а не начинает второй переход подряд.
Команда start или recovery центра разрешением перезапуска по-прежнему не является,
capabilities узла остаются `["low"]`, и `runtime-agent-executor.md` не меняется.

Контейнерному backend добавлены примитивы `DockerCli`, которых требуют §3 и §8.4:
`create` (полный ID до запуска), `start_created`, `inspect_by_id`,
`containers_by_label` (поиск потерянного create по метке writer_id),
`engine_identity` и `writable_mount_holders` (контейнеры с записью в каталог
слота). Сверка RW-доступа работает только с путями POSIX и на другой ОС **явно
отказывает**: пустой список выглядел бы как подтверждённое ограждение. Подготовка
каталога слота перенесена в `LowProfileTranslator.prepare_journal_directory`,
чтобы обычный start и producer готовили его одинаково.

Идентичность Engine текущей цепочки хранится у привязки: отсутствие контейнера
прежнего писателя на **другом** daemon ограждения не доказывает и даёт
RUNTIME_OBSERVATION_UNKNOWN без продвижения состояния.

### 9.2 Startup-выделение в CLI Low

Добавлены согласованные в §8.1 параметры `--journal-first-id=<uuid>` и
`--journal-first-seq=<n>`; они задаются вместе и только вместе с `--journal-dir`.
Выделение относится к первому поколению процесса: следующие Low выбирает сам.
Если номер, вычисленный Low (максимум своего instance в каталоге плюс один),
отличается от выделенного, ловушка завершается кодом 2 **до открытия слушателей**
и не создаёт файла; выделение не расходуется неудачной попыткой. Существующий
файл по-прежнему защищён O_EXCL. Формат `honeyforge-low-journal/1` не изменился:
writer_id в заголовок не пишется, связь писателя и журнала хранит registry.

### 9.3 Стенд совместного теста

`python -m infra.runtime.boundary_stand --stage {anchor|hold|finish|restart|reconcile|status}`
вызывает настоящий producer с настоящим `DockerCli`. Корни публикации, каталог
журналов, каталог состояния, привязку и группу чтения сборщика задаёт оператор
аргументами; профиль центра стенд не читает и секретов не принимает. Этап `status`
ничего не меняет. Аварийное завершение писателя стенд не выполняет: он печатает
полный container ID, и участник завершает процесс сам. Публикация поддерживается
только на Linux; на другой ОС стенд сообщает PRODUCER_DISABLED вместо имитации.

### 9.4 Что проверено и что не проверено

Проверено автотестами (`tests/runtime/test_boundary_*.py`,
`tests/runtime/test_containers_boundary.py`, `tests/low/test_journal_allocation.py`,
`tests/infra/test_boundary_stand.py`):

- канонические байты и SHA-256 обоих примеров `samples/low-process-boundary/`
  воспроизводятся из полей; неканонические байты, дубли ключей, BOM, LF, NaN,
  UUID не в lowercase, ведущие нули, неизвестные и отсутствующие ключи отвергаются;
- правила §4: первый переход без предшественника, разные writer и container ID,
  `last previous journal_seq + 1`, запрет повторного journal_id, пределы 64 журналов,
  65535 байт хвоста и 1 MiB документа;
- наблюдение каталога слота на фикстурах 11.10 и на журналах настоящего писателя:
  пропуск номера, развилка previous, повтор journal_id, чужая привязка,
  неканоническое имя, имя с префиксом экземпляра в другом регистре, объект, который
  не является обычным файлом, файл без полной header, хвост 910/885;
- граф состояний registry §8.4: скачки anchor в `next_started`, `next_bound`,
  `fenced` и `snapshots_published` отклоняются; переход требует ограждения первым
  ребром и снимков до создания контейнера; `committed`, `exported` и
  `failed_after_launch` через `advance` недостижимы; ребро принимает только свои
  поля, не допускает отсутствующих и не переписывает закреплённые значения;
  закрытие после запуска требует стадии с выданным RW и отсутствия документа;
- registry: один незавершённый intent, сожжённые идентичности после отказа,
  отказ второму anchor до создания контейнера, выделение boundary_seq только при
  commit, неизменность сохранённых байт, перечень снимков только для ограждённого
  перехода, блокировка привязки при неизвестном исходе, запрет повторного закрытия
  завершённого перехода, повторное выделение после снятой попытки, отказ чужого
  файла базы и второго владельца каталога;
- порядок §3 и восстановление §8.4 на подмене Engine, где журнал пишет **настоящий**
  писатель ловушки по аргументам, разобранным его же CLI: основной сценарий
  аварийного перезапуска, удержанный export, второй RW-доступ, неудавшееся
  удаление контейнера, отказ Engine на удалении прежнего писателя, другой daemon,
  другая revision до любых изменений, переход без anchor, bootstrap накопленного
  instance, непривязанный заголовок, подложенный файл в каталоге журнала, контейнер
  без запуска, нехватка квоты Low и квоты snapshot_root, второй переход со счётчиком 2;
- пути выдачи доказательства, которого не было (найдены ревью, см. §9.5):
  утрата registry при опубликованных документах, повторное ограждение при
  возобновлении из `fenced`, изменение байтов между измерением цепочки и копией,
  перенятие опубликованного каталога снимков без перечня, повторная публикация
  пропавшего документа, повторный `restart` при готовом переходе;
- раздельные пользователи R, C и L на Linux под root: R публикует документ,
  C в группе чтения читает его и получает отказ ядра на запись, дописывание,
  unlink, rename, chmod и создание своего файла в каталоге публикации, L без
  группы не читает документ, не перечисляет каталог и не подкладывает файл.
  Проверяются фактические операции в дочерних процессах с необратимо
  изменённой идентичностью, а не `os.access` и не биты из `stat`. Даже R не
  открывает опубликованный документ на запись, а повтор тех же байтов остаётся
  идемпотентным;
- публикация и снимки на Linux: режимы 0750, 0550 и 0440, владелец и группа,
  одна жёсткая ссылка, отказ при битах записи и второй ссылке, отказ по symlink
  в цепочке и по доступному на запись родителю, сверка пересечения корней по
  фактическим путям, повтор с теми же байтами, конфликт других байт, приватная
  подготовка 0700 и её удаление после назначения режима публикации, обычные копии
  вместо ссылок, SOURCE_CHANGED при изменении исходника, сверка опубликованного
  набора, пределы §8.5 до и после копирования.

Фактически выполненные прогоны на сочетании этой ветки с актуальным main:
`ruff`, `ruff format`, `mypy` (включая `--platform win32`) и `pytest`:

| Прогон | Результат |
| --- | --- |
| Windows, весь набор | 1693 passed, 71 skipped |
| Контейнер `python:3.12-slim`, root | 890 passed, 2 skipped |
| Тот же контейнер, непривилегированный `uid 1001` | 887 passed, 5 skipped |
| `HF_DOCKER_BOUNDARY=1 pytest tests/runtime/test_boundary_docker.py` на Docker 29.5.2 | 2 passed (с Windows и из Linux) |
| `HF_REQUIRE_LINUX_USERS=1 pytest tests/runtime/test_boundary_linux_users.py` под root в контейнере | 3 passed |

В контейнере не собираются файлы, которым нужны зависимости, установленные в
локальном окружении под Windows (`pydantic`, `rpds`): `tests/telemetry`,
`tests/runtime/test_executor*.py` и
`tests/infra/test_backend_websocket_packaging.py`. На Windows они проходят, а в
CI их выполняет шаг с установленными зависимостями. Прогон под
непривилегированным пользователем добавлен потому, что
шаг CI `runtime` работает именно так: под root биты режима не ограничивают сам
проверяющий процесс, поэтому root-прогон проверку прав не доказывает. Группа
чтения collector в тестах — группа процесса, когда он не root
(`tests/runtime/boundary_support.py`): смена группы разрешена владельцу только
на свою или дополнительную. На настоящем Docker проверены примитивы Engine:
create с полным ID до запуска, start и наблюдение по ID, поиск по метке, список
контейнеров с записью в каталог слота (на Linux, где Engine сообщает фактический
путь хоста), stop и remove по ID, отказ сверки на путях не-POSIX.

**Не проверено.** Полный совместный прогон §6 на согласованном Linux-стенде
Docker с настоящим образом ловушки и сборщиком Димы: ни один этап стенда
(`anchor`, `hold`, `finish`) на таком стенде не выполнялся, сторона collector не
реализована. Раздельные R, C и L проверены только для публикации документов:
каталог слота и снимки под теми же пользователями не проверялись, а смена
владельца каталога слота в тестах producer по-прежнему подменена, как в тестах
драйвера Low. Не проверены фактическое заполнение
snapshot_root, файловая система без RENAME_NOREPLACE, ветвь renameat2 через номер
системного вызова (glibc в прогонах экспортирует сам символ, поэтому маршалинг
varargs не исполнялся), `chattr +i`, SELinux-метки и удалённая доставка
документов. Права и renameat2 проверены на overlayfs контейнера, а не на целевой
файловой системе стенда. Переменная `HF_REQUIRE_LINUX_BOUNDARY` защищает только
от запуска этих проверок на другой ОС и факта их выполнения не доказывает.
Все эти пункты остаются обязательными до слияния по §7.

### 9.5 Результаты ревью и оставшиеся расхождения

Перед push выполнен цикл трёх субагентов по AGENTS.md §4: два независимых отчёта
и третий рецензент, получивший оба и перепроверивший каждую гипотезу, включая
опровергнутые. Найденное исправлено в этом же изменении:

| Находка | Что сделано |
| --- | --- |
| Linux-проверки требовали root или членства в gid 65532 и упали бы в CI (31 падение) | Группа чтения берётся по роли процесса; прогон подтверждён и под `uid 1001` |
| Утрата registry при опубликованных документах давала **второй anchor** того же instance | Выдача документов отказывает с RUNTIME_OBSERVATION_UNKNOWN; история по каталогу не восстанавливается (§8.4) |
| Возобновление из `fenced` не перепроверяло RW-доступ: proof при живом держателе | Ограждение подтверждается наблюдением заново перед снимками (§2, §3 шаг 2) |
| Байты, дописанные после ограждения, закреплялись как «старые» без SOURCE_CHANGED | Копия сверяется с измерением цепочки; расхождение блокирует переходы instance |
| Сбой между публикацией каталога снимков и записью перечня запирал переход | Опубликованный каталог перенимается со сверкой байтов цепочки |
| Строка `exported` §8.4 не была реализована: пропавший документ не публиковался | `republish` публикует сохранённые байты; вызывается из reconcile |
| Назначение сверялось после ограждения и снимков | Сверка вынесена в начало операции, тест усилен |
| Квота snapshot_root (§8.3 «Место») не была реализована | Проверка свободного места и квоты оператора до ограждения и до копирования |
| Уборка подготовки не работала без root и подменяла исходную ошибку | Режим возвращается к 0700 перед удалением |
| Повторный `restart` при готовом переходе делал лишнюю ротацию писателя | Готовый переход доводится и возвращается без второй ротации |
| Контейнер в `exited` без заголовка получал второй `start` того же writer_id | Такая попытка снимается как непривязанная (§2, §8.4) |
| Сверка пересечения путей была лексической | Пути приводятся к фактическим; каталог слота сверяется и после подготовки |
| Неполный ответ `docker inspect` мог дать пустой список держателей | Каждый ID пачки наблюдается, молчание — отказ наблюдения |
| Исчезновение файла в каталоге слота выглядело как «слот пуст» | Отсутствие каталога отличается от пропажи поколения |
| Тесты «второй anchor» и «другая revision» проходили не по заявленной причине | Переписаны на фактическую проверку |
| §9.4 переоценивал покрытие | Переписан; мёртвый переключатель подмены закрыт тестом |

Отдельным проходом по итоговой версии найдены и исправлены ещё два расхождения:

| Находка | Что сделано |
| --- | --- |
| Имя файла журнала могло обрушить producer: проверка последовательности строила полный диапазон от первого номера до максимального, а номер приходит из имени, которое пишет сама ловушка | Номера сверяются попарно; число знаков в имени ограничено, более длинное имя считается неизвестным; предел 64 журналов проверяется до чтения заголовков |
| Повторная публикация не сравнивала байты: подменённый опубликованный anchor или proof молча пропускался | Содержимое сверяется с сохранёнными в registry байтами, несовпадение — BOUNDARY_PUBLICATION_CONFLICT без перезаписи (§8.4) |
| Тем же классом: очень длинная строка цифр в счётчике документа поднимала ValueError мимо кодов отказа | Длина десятичной строки проверяется до разбора в int |

Отдельная проверка registry соседней сессией подтвердила пять расхождений с
графом §8.4; все исправлены здесь же:

| Находка | Что сделано |
| --- | --- |
| `advance` сравнивал номера состояний, поэтому anchor мог пройти `intent → next_bound` и выдать документ в обход create и start | Разрешены только соседние рёбра графа своего вида |
| anchor принимал стадии перехода (`fenced`, `snapshots_published`) | Граф anchor их не содержит |
| `advance(committed)` с последующим `mark_published` завершал intent без созданных байт документа (падало утверждением) | Состояния фиксации, публикации и закрытия через `advance` недостижимы; `mark_published` требует сохранённый документ |
| поздний параметр чужой стадии переписывал уже выделенный номер первого поколения | Ребро принимает ровно свои поля и только пока они не заполнены |
| `fail_after_launch` после фиксации блокировал привязку с уже выданным документом | Закрытие допустимо только со стадий с выданным RW и только без документа |

Остались известными и не исправленными: §3 шаг 1 обещает замок «на binding и
каталог», а реализован межпроцессный замок каталога состояния, поэтому два
producer с разными каталогами состояния и одновременная работа обычного start
исполнителя с этой операцией не разделены (см. `runtime-agent-executor.md`);
временный `.tmp-*` получает группу G и режим 0440 непосредственно перед
атомарным переименованием (§8.2); ловушка с RW доступом к каталогу слота может
подложить файл с префиксом своего instance и оставить слот без писателя до
вмешательства оператора (§8.3 это предписывает, но как сервисный риск не назван).

Повторный цикл трёх субагентов на итоговой версии после этих правок **не
выполнялся** по решению владельца рабочей копии; AGENTS.md §4 его требует, и до
слияния он остаётся незакрытым пунктом наряду с совместным прогоном §6.
