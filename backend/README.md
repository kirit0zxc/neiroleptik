# Серверный голос Юми

Django 5.2, Python 3.12, локальный Piper на CPU. API описан в
`../specs/openapi.json`, интеграция — в `../specs/mascot.md`, раздел 5.4.
Нужны два процесса: Django и Vite. Текст не отправляется в облако;
интернет требуется при установке пакетов и однократной загрузке модели.

Из корня репозитория (PowerShell):

```powershell
python -m venv backend/.venv
backend/.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
Copy-Item backend/.env.example backend/.env
backend/.venv/Scripts/python.exe backend/manage.py download_tts_voice
backend/.venv/Scripts/python.exe backend/manage.py migrate
backend/.venv/Scripts/python.exe backend/manage.py runserver 127.0.0.1:8000
```

Linux/macOS: активация venv и команды через `backend/.venv/bin/python`.
Во втором терминале:

```sh
cd frontend
npm ci
npm run dev
```

Открой адрес Vite и нажми «сказать». Приложение использует серверный
голос по умолчанию, лаборатория — браузерный синтез. Если Web Audio
заблокирован политикой автозапуска, речь запускается пользовательским
кликом; сбой оставляет текстовый пузырь.

`backend/.env` читается автоматически. Для PostgreSQL задай DATABASE_URL;
SQLite — локальный запасной вариант. Для deployment: уникальный
DJANGO_SECRET_KEY, DJANGO_DEBUG=false, явные DJANGO_ALLOWED_HOSTS,
HTTPS и reverse proxy, отдающий фронтенд и /api с одного origin.
Cookie CSRF в deployment имеет Secure. При отдельном trusted origin
его задают в DJANGO_CSRF_TRUSTED_ORIGINS; CORS в эту интеграцию не входит.

Голос скачивается в `backend/voices/`, исключённый из Git. Стандартная
модель — `ru_RU-irina-medium` (22 050 Гц); это голос Piper, а не
Microsoft Irina. Можно задать TTS_MODEL_PATH к другому совместимому
локальному ONNX; рядом нужен файл `.onnx.json`.
Источники: [Piper и его условия](https://github.com/OHF-Voice/piper1-gpl),
[карточка голоса Irina](https://huggingface.co/rhasspy/piper-voices/blob/main/ru/ru_RU/irina/medium/MODEL_CARD).
Карточка этой модели не указывает лицензию обучающего датасета;
право распространять саму модель этим проектом не заявляется.

Синтез в одном процессе выполняется последовательно, без очереди:
занятый процесс отвечает 503. Отмена на фронте прекращает запрос
и звук, но уже начатое CPU-вычисление сервер дорабатывает; общая
модель остаётся загруженной. Реплики ограничены 400 символами.

Проверки из backend:

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test --noinput
```

В CI API проверяется с WAV-фикстурой и подменённым синтезатором:
голос и большие веса не скачиваются. Piper дополнительно проверяется
с подменой только загрузки модели; реальный синтез нужен при ручной
проверке установки. PostgreSQL 16 поднимается существующим CI job.
