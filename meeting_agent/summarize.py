"""Саммари расшифровки (llm.backend): локально (MLX, прямо в процессе), через Claude Code
по подписке Claude, через Claude API, Gemini API или OpenAI API (ChatGPT) по ключу.

Локальная модель читает длинные встречи в два прохода (тезисы по кускам → итог),
облачные модели получают расшифровку целиком.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

import requests

from . import config
from .config import TEMPLATES_DIR, anthropic_key, openai_key
from .osutil import IS_WIN

log = logging.getLogger(__name__)

SYSTEM = ("Ты — ассистент, который пишет протоколы рабочих встреч на русском языке. "
          "Опирайся только на расшифровку, ничего не придумывай. Расшифровка сделана "
          "автоматически и может содержать ошибки распознавания — исправляй очевидные "
          "по смыслу, но не меняй факты, цифры и имена. Имя со звёздочкой (Олег*) угадано "
          "по разговору, имя со знаком (?) — неуверенно узнано по голосу: пиши такие имена "
          "ровно так же, с пометкой.")

CHUNK_PROMPT = """Это фрагмент {i} из {n} расшифровки встречи.
Выпиши подробные тезисы этого фрагмента:
- о чём говорили (с цифрами и фактами);
- какие решения приняли;
- задачи: кто, что, к какому сроку;
- вопросы, оставшиеся без ответа.
В начале каждого тезиса указывай время [чч:мм:сс]. Ничего не добавляй от себя.

Фрагмент:
{text}"""


def template_text(name: str) -> str:
    path = TEMPLATES_DIR / f"{name}.md"
    if not path.exists():
        available = ", ".join(p.stem for p in TEMPLATES_DIR.glob("*.md"))
        raise ValueError(f"Нет шаблона «{name}». Доступны: {available}")
    return path.read_text(encoding="utf-8")


_model = None  # (имя, модель, токенизатор): загружаем один раз на процесс


def _load(name: str):
    global _model
    if _model is None or _model[0] != name:
        from mlx_lm import load

        log.info("Загружаю языковую модель %s…", name)
        model, tokenizer = load(name)
        _model = (name, model, tokenizer)
    return _model[1], _model[2]


def chat(cfg: dict, user: str, system: str = SYSTEM) -> str:
    backend = cfg["llm"].get("backend")
    if backend == "claude-code":
        return chat_claude_code(cfg, user, system)
    if backend == "claude":
        return chat_claude(cfg, user, system)
    if backend == "gemini":
        return chat_gemini(cfg, user, system)
    if backend == "openai":
        return chat_openai(cfg, user, system)
    return chat_local(cfg, user, system)


# У облачных моделей контекст от 400 тыс. токенов: любая встреча помещается целиком.
CLOUD = ("claude", "claude-code", "gemini", "openai")


def chunk_size(cfg: dict) -> int:
    return 10**9 if cfg["llm"].get("backend") in CLOUD else int(cfg["llm"]["chunk_chars"])


def claude_cli() -> str | None:
    """Путь к консольному Claude Code. У приложений из Finder PATH короткий, поэтому ищем и сами."""
    found = shutil.which("claude")
    if found:
        return found
    if IS_WIN:
        candidates = ("~/.local/bin/claude.exe", "~/AppData/Roaming/npm/claude.cmd")
    else:
        candidates = ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude")
    for p in candidates:
        p = os.path.expanduser(p)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def chat_claude_code(cfg: dict, user: str, system: str = SYSTEM) -> str:
    """Запрос через Claude Code (`claude -p`): расходует лимиты подписки Claude, без API-ключа.
    Текст расшифровки уходит в облако Anthropic."""
    exe = claude_cli()
    if not exe:
        raise RuntimeError("Claude Code не установлен: поставьте его по инструкции на code.claude.com, "
                           f"выполните `claude` в {'PowerShell' if IS_WIN else 'Терминале'} и войдите в аккаунт")
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # иначе Claude Code возьмёт платный ключ вместо подписки
    model = cfg["claude"].get("code_model") or ""
    # Своя системная инструкция вместо «программистской» и никаких инструментов: только текст в ответ.
    cmd = [exe, "-p", "--output-format", "json", "--system-prompt", system, "--tools", ""]
    if model:
        cmd += ["--model", model]
    # Пустая временная папка: Claude Code не видит ни проекта, ни записей — только текст из stdin.
    with tempfile.TemporaryDirectory() as tmp:
        try:
            r = subprocess.run(cmd, input=user, capture_output=True, text=True,
                               cwd=tmp, env=env, timeout=1800)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("Claude Code не ответил за 30 минут") from e
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        err = (r.stderr or r.stdout).strip()[-500:]
        if "login" in err.lower() or "auth" in err.lower():
            raise RuntimeError(f"Claude Code не выполнил вход: запустите `claude` в {'PowerShell' if IS_WIN else 'Терминале'} и войдите")
        raise RuntimeError(f"Claude Code завершился с ошибкой: {err}")
    if out.get("is_error"):
        raise RuntimeError(f"Claude Code вернул ошибку: {out.get('result') or out.get('subtype')}")
    return str(out.get("result", "")).strip()


def chat_claude(cfg: dict, user: str, system: str = SYSTEM) -> str:
    """Запрос к Claude API. Текст расшифровки уходит в облако Anthropic."""
    import anthropic

    c = cfg["claude"]
    key = anthropic_key(cfg)
    if not key:
        raise RuntimeError("Не задан ключ Claude API: впишите его в настройках "
                           "или в файл .anthropic_key рядом с config.toml")
    client = anthropic.Anthropic(api_key=key, max_retries=4)
    try:
        resp = client.beta.messages.create(
            model=c["model"],
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": c["effort"]},
            # Если модель откажется отвечать, запрос сам повторится на запасной модели.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise RuntimeError("Claude API не принял ключ: проверьте его в настройках") from e
    except anthropic.PermissionDeniedError as e:
        raise RuntimeError(f"Claude API отказал в доступе: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError("Нет связи с Claude API: проверьте интернет") from e
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude отказался составлять протокол по этой расшифровке")
    if resp.stop_reason == "max_tokens":
        log.warning("Ответ Claude обрезан по длине")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


GEMINI = "https://generativelanguage.googleapis.com/v1beta"
_gemini_models: dict[str, str] = {}  # заданная модель → рабочая (если заданную убрали)
# Модели бесплатного уровня Gemini API (ноябрь 2026); основная — первая.
GEMINI_FREE = ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
GEMINI_DEFAULT = GEMINI_FREE[0]
GEMINI_MIN_VERSION = (3, 1)  # старее — не показываем и не используем


def _gemini_version(name: str) -> tuple[int, int]:
    m = re.match(r"gemini-(\d+)(?:\.(\d+))?", name)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def gemini_model(name: str | None) -> str:
    """Заданная модель Gemini, а вместо устаревшей (2.x и т. п.) — основная бесплатная."""
    return name if name and _gemini_version(name) >= GEMINI_MIN_VERSION else GEMINI_DEFAULT


def chat_gemini(cfg: dict, user: str, system: str = SYSTEM, model: str | None = None) -> str:
    """Запрос к Gemini API по ключу. Текст уходит в облако Google."""
    key = config.read_secret("gemini_key")
    if not key:
        raise RuntimeError("Не задан ключ Gemini API: впишите его в Настройках → Gemini и ChatGPT")
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    wanted = gemini_model(model or cfg.get("gemini", {}).get("model"))
    model = _gemini_models.get(wanted, wanted)
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2}}
    try:
        r = requests.post(f"{GEMINI}/models/{model}:generateContent", headers=headers, json=body, timeout=900)
        if r.status_code == 404:  # модель сменили или убрали — берём актуальную того же семейства
            model = _pick_gemini_model(headers, "pro" if "pro" in wanted else "flash")
            r = requests.post(f"{GEMINI}/models/{model}:generateContent", headers=headers, json=body, timeout=900)
    except requests.RequestException as e:
        raise RuntimeError("Нет связи с Gemini API: проверьте интернет") from e
    if r.status_code == 429:
        hint = ("Бесплатный лимит обновляется со временем: попробуйте пересоздать протокол позже "
                "или подключите оплату в Google AI Studio." if "flash" in model else
                "Бесплатно доступны модели Flash — выберите такую в Настройках → Gemini и ChatGPT "
                "или подключите оплату в Google AI Studio.")
        raise RuntimeError(f"Исчерпан лимит Gemini API для модели {model}. {hint} Ответ Google: {_api_error(r)[:200]}")
    if r.status_code in (400, 401, 403):
        raise RuntimeError(f"Gemini API отказал: {_api_error(r)}")
    r.raise_for_status()
    _gemini_models[wanted] = model
    data = r.json()
    try:
        cand = data["candidates"][0]
        text = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought")).strip()
    except (KeyError, IndexError):
        reason = (data.get("promptFeedback") or {}).get("blockReason") or "пустой ответ"
        raise RuntimeError(f"Gemini не ответил: {reason}")
    if cand.get("finishReason") == "MAX_TOKENS":
        log.warning("Ответ Gemini обрезан по длине")
    return text


# Модели не для текста: картинки, речь, эмбеддинги, живой режим и т. п.
_GEMINI_SKIP = ("image", "tts", "embedding", "live", "audio", "native", "computer-use", "robotics", "aqa")


def gemini_models() -> list[dict]:
    """Текстовые модели Gemini, доступные по сохранённому ключу (для выбора в настройках)."""
    key = config.read_secret("gemini_key")
    if not key:
        return []
    try:
        r = requests.get(f"{GEMINI}/models", headers={"x-goog-api-key": key}, params={"pageSize": 1000}, timeout=20)
    except requests.RequestException as e:
        raise RuntimeError("Нет связи с Gemini API: проверьте интернет") from e
    if r.status_code in (400, 401, 403):
        raise RuntimeError(f"Gemini API отказал: {_api_error(r)}")
    r.raise_for_status()
    out = []
    for m in r.json().get("models", []):
        name = m["name"].removeprefix("models/")
        if (name.startswith("gemini") and "generateContent" in m.get("supportedGenerationMethods", [])
                and not any(x in name for x in _GEMINI_SKIP) and _gemini_version(name) >= GEMINI_MIN_VERSION):
            out.append({"id": name, "title": m.get("displayName") or name, "free": name in GEMINI_FREE})
    # Сначала бесплатные в порядке GEMINI_FREE, затем остальные — от новых к старым.
    return sorted(out, key=lambda m: (GEMINI_FREE.index(m["id"]) if m["free"] else len(GEMINI_FREE),
                                      tuple(-x for x in _gemini_version(m["id"])), m["id"]))


def _pick_gemini_model(headers: dict, family: str) -> str:
    r = requests.get(f"{GEMINI}/models", headers=headers, timeout=30)
    r.raise_for_status()
    names = [m["name"].removeprefix("models/") for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    good = [n for n in names if family in n and _gemini_version(n) >= GEMINI_MIN_VERSION
            and not any(x in n for x in ("lite", "image", "tts", "preview", "exp"))]
    if not (good or names):
        raise RuntimeError("Gemini API не предлагает подходящих моделей")
    return max(good, key=_gemini_version) if good else sorted(names)[-1]


# ---------- проверка ключей ----------

def check_key(service: str, cfg: dict) -> dict:
    """Пробный запрос к сервису по сохранённому ключу: {ok, message}. Тратит один крошечный запрос."""
    try:
        if service == "gemini":
            return _check_gemini(cfg)
        if service == "openai":
            return _check_openai(cfg)
        if service == "claude":
            return _check_claude(cfg)
    except requests.RequestException:
        return {"ok": False, "message": "Нет связи с сервисом: проверьте интернет (и VPN, если он нужен)"}
    raise ValueError("Неизвестный сервис")


def _check_gemini(cfg: dict) -> dict:
    key = config.read_secret("gemini_key")
    if not key:
        return {"ok": False, "message": "Ключ не сохранён"}
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    r = requests.get(f"{GEMINI}/models", headers=headers, params={"pageSize": 1}, timeout=20)
    if r.status_code in (400, 401, 403):
        return {"ok": False, "message": f"Google не принял ключ: {_api_error(r)[:200]}"}
    r.raise_for_status()
    model = gemini_model(cfg.get("gemini", {}).get("model"))
    r = requests.post(f"{GEMINI}/models/{model}:generateContent", headers=headers, timeout=60,
                      json={"contents": [{"role": "user", "parts": [{"text": "Ответь одним словом: готово"}]}],
                            "generationConfig": {"maxOutputTokens": 32}})
    if r.status_code == 200:
        return {"ok": True, "message": f"Ключ работает, модель {model} отвечает"}
    if r.status_code == 429:
        return {"ok": False, "message": f"Ключ верный, но для {model} лимит исчерпан или модель недоступна "
                                        f"без оплаты. Выберите другую модель или попробуйте позже. "
                                        f"Ответ Google: {_api_error(r)[:150]}"}
    if r.status_code == 404:
        return {"ok": False, "message": f"Ключ верный, но модели {model} нет — выберите другую в списке"}
    return {"ok": False, "message": f"Ключ верный, но запрос к {model} не прошёл: {_api_error(r)[:200]}"}


def _check_openai(cfg: dict) -> dict:
    key = openai_key(cfg)
    if not key:
        return {"ok": False, "message": "Ключ не сохранён"}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    model = cfg.get("openai", {}).get("model") or "gpt-5"
    r = requests.post(f"{OPENAI}/chat/completions", headers=headers, timeout=60,
                      json={"model": model, "max_completion_tokens": 16,
                            "messages": [{"role": "user", "content": "Ответь одним словом: готово"}]})
    if r.status_code == 404:  # модели нет — ключ проверим по списку моделей
        r2 = requests.get(f"{OPENAI}/models", headers=headers, timeout=20)
        if r2.status_code == 200:
            return {"ok": True, "message": f"Ключ работает; модели {model} нет — при запросе возьмётся актуальная"}
        r = r2
    if r.status_code == 200:
        return {"ok": True, "message": f"Ключ работает, модель {model} отвечает"}
    if r.status_code == 401:
        return {"ok": False, "message": "OpenAI не принял ключ: проверьте, что он скопирован целиком"}
    err = _api_error(r)
    if r.status_code == 429 and "quota" in err.lower():
        return {"ok": False, "message": "Ключ верный, но на счёте API нет средств: пополните баланс на "
                                        "platform.openai.com (подписка ChatGPT Plus сюда не относится)"}
    if r.status_code == 400 and "max_completion_tokens" in err:  # модель не дописала ответ — ключ рабочий
        return {"ok": True, "message": f"Ключ работает, модель {model} отвечает"}
    return {"ok": False, "message": f"OpenAI ответил ошибкой: {err[:200]}"}


def _check_claude(cfg: dict) -> dict:
    key = anthropic_key(cfg)
    if not key:
        return {"ok": False, "message": "Ключ не сохранён"}
    model = cfg["claude"]["model"]
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json={"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ok"}]})
    if r.status_code == 200:
        return {"ok": True, "message": f"Ключ работает, модель {model} отвечает"}
    err = _api_error(r)
    if r.status_code == 401:
        return {"ok": False, "message": "Anthropic не принял ключ: проверьте, что он скопирован целиком"}
    if "credit balance" in err.lower():
        return {"ok": False, "message": "Ключ верный, но на счёте API нет средств: пополните баланс в консоли "
                                        "Anthropic (подписка Claude сюда не относится)"}
    return {"ok": False, "message": f"Anthropic ответил ошибкой: {err[:200]}"}


OPENAI = "https://api.openai.com/v1"
_openai_models: dict[str, str] = {}


def chat_openai(cfg: dict, user: str, system: str = SYSTEM) -> str:
    """Запрос к OpenAI API (модели ChatGPT) по ключу. Текст уходит в облако OpenAI."""
    key = openai_key(cfg)
    if not key:
        raise RuntimeError("Не задан ключ OpenAI API: впишите его в Настройках → Gemini и ChatGPT")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    wanted = cfg.get("openai", {}).get("model") or "gpt-5"
    model = _openai_models.get(wanted, wanted)
    # temperature не передаём: у рассуждающих моделей GPT-5 её менять нельзя.
    body = {"model": model, "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}]}
    try:
        r = requests.post(f"{OPENAI}/chat/completions", headers=headers, json=body, timeout=1800)
        if r.status_code == 404:  # модель убрали или нет к ней доступа — берём актуальную
            model = body["model"] = _pick_openai_model(headers)
            r = requests.post(f"{OPENAI}/chat/completions", headers=headers, json=body, timeout=1800)
    except requests.RequestException as e:
        raise RuntimeError("Нет связи с OpenAI API: проверьте интернет") from e
    if r.status_code == 401:
        raise RuntimeError("OpenAI API не принял ключ: проверьте его в настройках")
    if r.status_code in (400, 403, 429):
        raise RuntimeError(f"OpenAI API отказал: {_api_error(r)}")
    r.raise_for_status()
    _openai_models[wanted] = model
    try:
        choice = r.json()["choices"][0]
    except (KeyError, IndexError, ValueError):
        raise RuntimeError("OpenAI API вернул пустой ответ")
    if choice.get("message", {}).get("refusal"):
        raise RuntimeError("ChatGPT отказался составлять протокол по этой расшифровке")
    if choice.get("finish_reason") == "length":
        log.warning("Ответ ChatGPT обрезан по длине")
    return (choice.get("message", {}).get("content") or "").strip()


def _pick_openai_model(headers: dict) -> str:
    r = requests.get(f"{OPENAI}/models", headers=headers, timeout=30)
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]
    skip = ("mini", "nano", "audio", "realtime", "search", "image", "tts", "transcribe", "codex", "chat", "pro")
    good = [i for i in ids if re.fullmatch(r"gpt-\d+(\.\d+)?", i) and not any(x in i for x in skip)]
    if not good:
        raise RuntimeError("OpenAI API не предлагает подходящих моделей")
    return max(good, key=lambda i: [float(x) for x in re.findall(r"\d+(?:\.\d+)?", i)])


def _api_error(r: requests.Response) -> str:
    try:
        err = r.json().get("error") or {}
        return (err.get("message") if isinstance(err, dict) else str(err)) or str(r.status_code)
    except ValueError:
        return str(r.status_code)


def chat_local(cfg: dict, user: str, system: str = SYSTEM) -> str:
    """Один запрос к локальной модели (без сервера и без сети): MLX на Mac, llama.cpp на Windows."""
    from .models import llm_repo, require

    llm = cfg["llm"]
    if IS_WIN:
        return _chat_llama(require(llm_repo(cfg), "Языковая модель для протоколов"), cfg, user, system)
    from mlx_lm import generate
    from mlx_lm.sample_utils import make_sampler

    try:
        model, tokenizer = _load(require(llm_repo(cfg), "Языковая модель для протоколов"))
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Не удалось загрузить модель {llm['model']}: {e}") from e
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True)
    text = generate(model, tokenizer, prompt=prompt, max_tokens=int(llm["max_tokens"]),
                    sampler=make_sampler(temp=float(llm["temperature"])), verbose=False)
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


_llama = None  # (путь, модель llama.cpp)
_gpu_failed = False


def _chat_llama(path: str, cfg: dict, user: str, system: str) -> str:
    global _llama, _gpu_failed
    llm = cfg["llm"]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    from . import gpu

    if not _gpu_failed and gpu.llm_on_gpu():
        try:
            text = gpu.chat(path, messages, int(llm["max_tokens"]), float(llm["temperature"]))
            return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        except Exception as e:
            _gpu_failed = True  # до перезапуска приложения — на процессоре, не тратим время на повторы
            gpu.stop_server()
            log.warning("Qwen на видеокарте не заработал (%s) — считаю на процессоре", e)
    if _llama is None or _llama[0] != path:
        try:
            from llama_cpp import Llama

            log.info("Загружаю языковую модель %s…", os.path.basename(path))
            # Кусок расшифровки (chunk_chars) + ответ (max_tokens) с запасом помещаются в 16 тыс. токенов.
            model = Llama(model_path=path, n_ctx=16384, n_gpu_layers=-1,
                          n_threads=max(1, (os.cpu_count() or 4) - 1), verbose=False)
        except Exception as e:
            raise RuntimeError(f"Не удалось загрузить модель {os.path.basename(path)}: {e}") from e
        _llama = (path, model)
    r = _llama[1].create_chat_completion(
        messages=messages,
        max_tokens=int(llm["max_tokens"]), temperature=float(llm["temperature"]))
    text = r["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def split_chunks(text: str, size: int) -> list[str]:
    chunks, current, length = [], [], 0
    for line in text.splitlines():
        if current and length + len(line) > size:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def summarize(transcript: str, cfg: dict, template: str) -> tuple[str, str]:
    """Возвращает (название, markdown-протокол)."""
    instructions = template_text(template)
    chunks = split_chunks(transcript, chunk_size(cfg))
    if len(chunks) <= 1:
        source = f"Расшифровка встречи:\n{transcript}"
    else:
        notes = []
        for i, chunk in enumerate(chunks, 1):
            log.info("Тезисы по фрагменту %d/%d…", i, len(chunks))
            notes.append(chat(cfg, CHUNK_PROMPT.format(i=i, n=len(chunks), text=chunk)))
        source = "Тезисы встречи по порядку (собраны из фрагментов расшифровки):\n\n" + "\n\n".join(notes)
    from .participants import of
    listed = of(cfg)
    if listed:
        source = (f"Участники встречи (список составлен вручную, кроме «Я»): {', '.join(listed)}. "
                  "Пиши их имена именно так.\n\n" + source)
    log.info("Итоговое саммари…")
    return parse_title(chat(cfg, f"{instructions}\n\n{source}"))


def parse_title(text: str) -> tuple[str, str]:
    title = "Встреча"
    lines = text.strip().splitlines()
    for i, line in enumerate(lines[:5]):
        m = re.match(r"\s*\**\s*(TITLE|НАЗВАНИЕ)\s*:\s*\**\s*(.+?)\s*\**\s*$", line, re.I)
        if m:
            title = m.group(2).strip(" #*\"«»")
            del lines[i]
            break
    else:  # модель не написала TITLE:, но начала с заголовка «# Тема» — берём название из него
        for i, line in enumerate(lines[:5]):
            m = re.match(r"\s*#\s+(.+?)\s*$", line)
            if m:
                title = m.group(1).strip(" *\"«»")
                del lines[i]
                break
    body = "\n".join(lines).strip()
    body = re.sub(r"^#\s+.*\n+", "", body)  # свой заголовок H1 ставим сами
    return title or "Встреча", body
