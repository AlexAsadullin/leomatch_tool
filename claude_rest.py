#!/usr/bin/env python3
"""
gen_claude_settings.py
Парсит .gitignore → генерирует .claude/settings.json с deny-правилами.
"""

import json
import os
import re
from pathlib import Path

# Bash-команды которые трогают файлы
DANGEROUS_BASH_CMDS = [
    "rm", "unlink", "shred",           # удаление
    "mv", "cp",                         # перемещение / копирование
    "truncate", "dd",                   # перезапись
    "chmod", "chown",                   # права
    "nano", "vim", "vi", "emacs",       # редакторы
    "tee", "cat",                       # запись через pipe
    "sqlite3",                          # прямая работа с .db
]


def parse_gitignore(gitignore_path: Path) -> list[str]:
    """Возвращает список паттернов из .gitignore (без комментов и пустых строк)."""
    patterns = []
    with open(gitignore_path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n").rstrip()
            # пропускаем комменты, пустые строки и негации (! — это включение, не исключение)
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            patterns.append(line)
    return patterns


def normalize_pattern(raw: str) -> str:
    """
    Нормализует паттерн из .gitignore в glob-совместимый вид для Claude.
    .gitignore:   venv/       → venv/**
    .gitignore:   *.pyc       → **/*.pyc
    .gitignore:   data.db     → data.db (как есть)
    """
    p = raw

    # убираем leading slash — он означает «от корня», нам это не нужно
    if p.startswith("/"):
        p = p[1:]

    # trailing slash → это директория → добавляем **
    if p.endswith("/"):
        p = p + "**"

    # если нет слеша и нет ** → это может быть файл в любом месте дерева
    if "/" not in p and "**" not in p and not p.startswith("*"):
        # оставляем как есть — Claude сматчит по имени файла
        pass

    return p


def build_deny_rules(patterns: list[str]) -> list[str]:
    """Строит список deny-правил для каждого паттерна."""
    deny = []

    for raw in patterns:
        p = normalize_pattern(raw)

        # Read и Edit — прямые паттерны
        deny.append(f"Read({p})")
        deny.append(f"Edit({p})")

        # Bash — для каждой опасной команды
        # Claude Code сматчит: Bash(rm:*data.db*) → rm ... data.db ...
        for cmd in DANGEROUS_BASH_CMDS:
            deny.append(f"Bash({cmd}:*{p}*)")

        # Отдельно блокируем shell-редирект вида `> file`
        # Bash(*> pattern*) — ловит `echo x > data.db` и т.д.
        deny.append(f"Bash(*> {p}*)")
        deny.append(f"Bash(*> ./{p}*)")

    # дедупликация с сохранением порядка
    seen = set()
    result = []
    for rule in deny:
        if rule not in seen:
            seen.add(rule)
            result.append(rule)

    return result


def load_existing_settings(settings_path: Path) -> dict:
    """Загружает существующий settings.json или возвращает пустой скелет."""
    if settings_path.exists():
        with open(settings_path, encoding="utf-8") as f:
            return json.load(f)
    return {"permissions": {"deny": [], "allow": []}}


def merge_deny(existing: list[str], new_rules: list[str]) -> list[str]:
    """Мёрджит новые правила в существующие без дублей."""
    combined = list(existing)
    existing_set = set(existing)
    for rule in new_rules:
        if rule not in existing_set:
            combined.append(rule)
            existing_set.add(rule)
    return combined


def main():
    repo_root = Path.cwd()
    gitignore_path = repo_root / ".gitignore"
    settings_dir = repo_root / ".claude"
    settings_path = settings_dir / "settings.json"

    # --- валидация ---
    if not gitignore_path.exists():
        print(f"[ERROR] .gitignore не найден: {gitignore_path}")
        return

    # --- парсим ---
    patterns = parse_gitignore(gitignore_path)
    print(f"[INFO] Найдено паттернов в .gitignore: {len(patterns)}")
    for p in patterns:
        print(f"       {p}")

    # --- строим правила ---
    new_rules = build_deny_rules(patterns)
    print(f"\n[INFO] Сгенерировано deny-правил: {len(new_rules)}")

    # --- мёрджим с существующим settings.json ---
    settings = load_existing_settings(settings_path)

    existing_deny = settings.get("permissions", {}).get("deny", [])
    merged_deny = merge_deny(existing_deny, new_rules)

    settings.setdefault("permissions", {})["deny"] = merged_deny

    # --- пишем ---
    settings_dir.mkdir(exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

    print(f"\n[OK] Записано в: {settings_path}")
    print(f"     Всего deny-правил: {len(merged_deny)}")


if __name__ == "__main__":
    main()