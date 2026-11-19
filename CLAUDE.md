# Meeting Agent — заметки для разработки

Локальный аналог Plaud для MacBook Pro M1 (16 ГБ): запись встреч Zoom → расшифровка → спикеры → протокол.
Встречи рабочие и конфиденциальные: звук и расшифровка никогда не покидают Mac. Язык встреч и интерфейса — русский.

## Устройство

```
Meeting Agent.app
├─ Contents/MacOS/Meeting Agent      Swift: app/*.swift + recorder/Recorder.swift
│    строка меню, окно WKWebView, запись (ScreenCaptureKit + AVAudioEngine), автозапись Zoom
├─ Contents/Resources/python         python-build-standalone 3.12 + пакеты (копия .venv)
└─ Contents/Resources/app            meeting_agent/, templates/, ui/, config.toml
```

- Оболочка запускает `python -m meeting_agent serve --port <свободный>` с `MEETING_AGENT_TOKEN`,
  `MEETING_AGENT_HOME=~/Library/Application Support/Meeting Agent`, `HF_HOME` там же, `HF_HUB_OFFLINE=1`.
- Сервер (`meeting_agent/server.py`, stdlib http.server, только 127.0.0.1): отдаёт `ui/`, а `/api/*`
  принимает только с заголовком `X-Token` и правильным `Host`. Очередь: каждая встреча обрабатывается
  отдельным процессом `python -m meeting_agent process`, этапы читаются из строк `@stage …` в его логе.
- Запись идёт в Swift, чтобы разрешения macOS были у приложения. После остановки оболочка шлёт `POST /api/process`.
  Окно управляет записью через `window.webkit.messageHandlers.app` (`startRecording`, `stopRecording`, `reloadSettings`, `openURL`).
- Настройки: `DEFAULTS` ← `config.toml` ← `settings.json` (в MEETING_AGENT_HOME, пишется из окна).
  Секреты: `.hf_token`, `.anthropic_key`, `.gemini_key`, `.openai_key` (права 600) там же; из терминала — в папке проекта.
- Модели (`meeting_agent/models.py`): Whisper и Qwen качаются своим загрузчиком с докачкой
  в `MEETING_AGENT_HOME/models/<repo>`. Штатный загрузчик HF (xet) на сети пользователя подвисает.
  Если модель уже лежит в `~/.cache/huggingface`, она берётся оттуда. pyannote качается самим pyannote (gated, нужен токен).
- Протокол (`llm.backend`): `local` — mlx-lm в процессе; `claude-code` — `claude -p --tools "" --system-prompt …`
  во временной пустой папке, по подписке пользователя; `claude` — Anthropic API (`claude-opus-5-5`, effort medium,
  `fallbacks: "default"`). Ollama больше не используется: её реестр у пользователя недоступен.
  `gemini` — Gemini API по REST (`.gemini_key`, gemini.model, по умолчанию gemini-3.8-flash — есть в бесплатном уровне, модели старше 3.1 скрыты и заменяются на неё;
  список моделей для выбора — `GET /api/gemini/models`); `openai` — OpenAI
  Chat Completions по REST (`.openai_key`, openai.model, по умолчанию gpt-5, без temperature). Обе модели при 404
  сами берут актуальную из списка моделей. Облачные варианты (`summarize.CLOUD`) получают расшифровку целиком.
- Аудио: `afconvert` (встроен в macOS) → 16 кГц моно; ffmpeg только как запасной вариант для ogg/opus/webm.
  Whisper получает numpy-массив, поэтому ffmpeg внутри mlx-whisper не вызывается.
- Встреча = папка в `~/MeetingAgent/recordings/<дата>` с `mic.*`, `others.*`, `meta.json`.
  `segments.json` хранит готовую расшифровку: пересоздание протокола её не переделывает (`--retranscribe` — переделать).
  `.done` содержит путь к заметке. При пересоздании старая заметка удаляется.

- Слепки голоса (`meeting_agent/voices.py`): `~/MeetingAgent/voices/<id>/profile.json` + `sample.wav`.
  Эмбеддинги — `speaker_embeddings` пайплайна pyannote 3.1 (256 чисел, wespeaker). `diarize.py` собирает пайплайн
  из его `config.yaml` с заглушкой PLDA: в pyannote 4.x `Pipeline.from_pretrained` для 3.1 требует доступ
  к закрытому `speaker-diarization-community-1`, хотя при агломеративной кластеризации PLDA не используется.
  В папке встречи: `speakers.json` (метка → base «Собеседник N», duration, embedding, sample, name, voice_id,
  score, uncertain, auto, manual) и `voices/<метка>.wav` — образцы 10–15 с, выбранные по сходству с голосом.
  В репликах `segments.json` поле `spk` — метка pyannote; имена подставляются `voices.apply_names` при каждой записи заметки.
  Переименование меняет текст заметки заменой (`replace_in_note`) без повторного вызова LLM.
  Уверенное совпадение автоматически пополняет слепок (auto=True); при повторной обработке такие эмбеддинги встречи сначала удаляются.
  Предполагаемые имена (`meeting_agent/names.py`): перед протоколом, если в speakers.json нет `names_guessed`,
  модель протокола (`summarize.chat` со своей system-инструкцией) возвращает JSON {«Собеседник N»: {name, evidence}};
  имя принимается, только если звучит в расшифровке, и только одному собеседнику. Хранится в `guess`/`guess_evidence`,
  отображается «Имя*» (`voices.display_name`). Сброс имени ставит `guess_rejected`.
  Список участников (`meeting_agent/participants.py`): `<встреча>/participants.json`, для следующей встречи —
  `MEETING_AGENT_HOME/next_participants.json` (забирается при `POST /api/recording` с новой папкой и при импорте).
  pipeline кладёт список в `cfg["meeting"]["participants"]`; его читают transcribe (initial_prompt), diarize
  (min_speakers = n−1 при n ≥ 3), voices.match (participant_bonus), names, summarize.
  Дорожка собеседников: сначала diarize, потом распознавание по репликам (`transcribe.regions_from_speakers`,
  куски ≤ 20 с одного человека, у реплики сразу `spk`). Без разделения — вся дорожка целиком.
  Ручное разделение: `voices.reassign` меняет `spk` реплики в segments.json (или создаёт `MANUAL_NN`),
  ставит `dirty`/`protocol_stale`, переписывает расшифровку в заметке; при пересоздании протокола
  `voices.refresh_dirty` пересчитывает слепки/образцы изменённых собеседников (`diarize.refresh_speakers`).
  Распознавание по ходу записи (`meeting_agent/live.py`): сервер при `POST /api/recording` запускает
  `taskpolicy -c utility python -m meeting_agent live <папка>`; тот дочитывает растущие WAV (заголовок ещё с нулевым
  размером), режет по паузам и пишет `<встреча>/live/{mic,others}.json` (until, asr, segments). Перед итоговой
  обработкой Jobs ждёт его завершения (SIGTERM). pipeline берёт готовое (если тот же движок и не --retranscribe),
  распознаёт хвост после until, реплики собеседников раскладывает по разметке, смешанные куски — заново по репликам.
  `audio.repair_wav` чинит незакрытые заголовки перед обработкой.
  Умный поиск (`meeting_agent/ask.py`, `POST /api/ask`): фрагменты заметок (пункты протокола + реплики),
  фильтр по датам из вопроса, BM25 с усечением слов до 5 букв, ответ модели с [n]; backend ask.backend
  (claude-code / claude / gemini через REST с ключом `.gemini_key` / local — Qwen в подпроцессе `ask-llm`).
  Протокол не составился → `protocol_error.txt` в папке встречи, заметка с расшифровкой и плашкой; повтор — без ASR.
  Порог 0,75 откалиброван на встрече 2026-10-26 (свой голос 0,84–0,92, чужие ≤ 0,54, смешанные кластеры до 0,71).

## Windows-версия

Один код на обе системы; различия ОС — в `meeting_agent/osutil.py` (уведомления, Проводник, приоритет,
батарея, блокировка, устройство PyTorch: mps/cuda/cpu). Только для Windows:
- `winapp.py` — оболочка вместо Swift: трей (pystray), окно (pywebview, Edge WebView2), сервер в фоне
  (`python.exe -m meeting_agent serve`, CREATE_NO_WINDOW, `MEETING_AGENT_PARENT_PID`), автозапись по окну Zoom
  (`detect.py`, EnumWindows), автозапуск (HKCU\…\Run), ярлык в «Пуск». Данные — `%LOCALAPPDATA%\Meeting Agent`.
- `winrec.py` — запись: микрофон + петля WASAPI с устройства вывода (весь звук компьютера, не только Zoom),
  через soundcard; беззвучный поток в динамики, чтобы петля не молчала; пропуски — нулями по часам.
- Whisper — faster-whisper (CUDA, если есть cuBLAS/cuDNN, иначе процессор int8), репозитории в `models.ASR_ENGINES`;
  Qwen — llama-cpp-python 0.3.19 (CPU), GGUF `models.WIN_LLM`; аудио — PyAV; live — только GigaAM.
- Остановка live: файл `<встреча>/live/.stop` (на Windows terminate() убивает сразу).
- Сборка на Mac: `./build_windows.sh` → `build/Meeting-Agent-Windows.zip` (python-build-standalone для Windows,
  пакеты — `uv pip install --python-platform x86_64-pc-windows-msvc` по `requirements-windows.lock`,
  `Meeting Agent.exe` — `windows/launcher.c` через `zig cc`). Проверить на Windows отсюда нельзя — тестирует коллега.

## Сборка и проверка

```bash
./build_app.sh [--install]          # ~1,5 мин; окружение Python кэшируется в build/cache
./agent process <файл|папка> --llm claude-code
MEETING_AGENT_TOKEN=dev ./agent serve --port 8765   # интерфейс: http://127.0.0.1:8765/#token=dev
.venv/bin/python -m pyflakes meeting_agent/
```

Подпись: самоподписанный сертификат «Meeting Agent Local» из связки ключей пользователя (designated requirement =
identifier + certificate leaf), поэтому разрешения macOS сохраняются между сборками. Без сертификата — ad-hoc,
и после каждой пересборки TCC пишет «Failed to match existing code requirement» — разрешения нужно выдавать заново.

## Особенности окружения пользователя

- Anaconda (base) в PATH. Для проекта используется только `.venv` на python@3.12 из Homebrew.
- Сеть нестабильная (бывает раздача с iPhone): большие загрузки только с докачкой.
- Claude Code: `/opt/homebrew/bin/claude`, вход по подписке.
