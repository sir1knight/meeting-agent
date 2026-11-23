"""Переносит верхний выпущенный раздел CHANGELOG.md в version.json: номер версии (app) и список изменений
(changes) — его показывает окно «Обновления» у пользователей. Запускается сборкой (build_app.sh,
build_windows.sh); можно и вручную перед отправкой на GitVerse:  python3 scripts/sync_version.py
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _parse_section(body: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    current = "Новое"
    for line in body.splitlines():
        if h := re.match(r"^### (.+)$", line):
            current = h.group(1).strip()
        elif b := re.match(r"^- (.+)$", line):
            item = re.sub(r"\*\*(.+?)\*\*", r"\1", b.group(1))
            item = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", item).replace("`", "")
            groups.setdefault(current, []).append(item.strip())
    return groups


def all_releases(text: str, limit: int = 20) -> list[dict]:
    out: list[dict] = []
    for m in re.finditer(r"^## (\d+\.\d+\.\d+) \(([^)]*)\)\s*$(.*?)(?=^## |\Z)", text, re.M | re.S):
        out.append({"version": m.group(1), "date": m.group(2), "changes": _parse_section(m.group(3))})
        if len(out) >= limit:
            break
    if not out:
        raise SystemExit("В CHANGELOG.md нет раздела вида «## 1.2.3 (дата)»")
    return out


def main() -> None:
    releases = all_releases((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    top = releases[0]
    version, date, changes = top["version"], top["date"], top["changes"]
    path = ROOT / "version.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    first = next(iter(changes.values()), [""])[0]
    new = dict(data, app=version, date=date, changes=changes, history=releases,
               notes=first.split(":")[0].split("—")[0].strip())
    if new != data:
        path.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"version.json: {version} ({date}), history: {len(releases)} релизов")


if __name__ == "__main__":
    main()
