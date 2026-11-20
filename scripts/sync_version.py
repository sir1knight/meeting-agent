"""Переносит верхний выпущенный раздел CHANGELOG.md в version.json: номер версии (app) и список изменений
(changes) — его показывает окно «Обновления» у пользователей. Запускается сборкой (build_app.sh,
build_windows.sh); можно и вручную перед отправкой на GitVerse:  python3 scripts/sync_version.py
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def latest_release(text: str) -> tuple[str, str, dict[str, list[str]]]:
    m = re.search(r"^## (\d+\.\d+\.\d+) \(([^)]*)\)\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        raise SystemExit("В CHANGELOG.md нет раздела вида «## 1.2.3 (дата)»")
    groups: dict[str, list[str]] = {}
    current = "Новое"
    for line in m.group(3).splitlines():
        if h := re.match(r"^### (.+)$", line):
            current = h.group(1).strip()
        elif b := re.match(r"^- (.+)$", line):
            item = re.sub(r"\*\*(.+?)\*\*", r"\1", b.group(1))
            item = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", item).replace("`", "")
            groups.setdefault(current, []).append(item.strip())
    return m.group(1), m.group(2), groups


def main() -> None:
    version, date, changes = latest_release((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    path = ROOT / "version.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    first = next(iter(changes.values()), [""])[0]
    new = dict(data, app=version, date=date, changes=changes, notes=first.split(":")[0].split("—")[0].strip())
    if new != data:
        path.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"version.json: {version} ({date})")


if __name__ == "__main__":
    main()
