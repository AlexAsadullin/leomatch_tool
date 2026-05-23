#!/usr/bin/env python3
"""Покажи дубликаты в profiles через SQLAlchemy ORM.

Дубликаты раскладываются на три класса:
  1) одинаковый текст анкеты, разные хеши первого фото;
  2) одинаковый хеш первого фото, разные тексты анкеты;
  3) одинаковые И текст, И хеш.

Замечание: в схеме (app/db.py) висит `UNIQUE(description, first_media_hash)`,
поэтому класс 3 при штатной вставке всегда даёт 0.

Usage:
    python3 scripts/find_duplicates.py
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import Integer, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.config import DB_PATH


class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    first_media_hash: Mapped[str] = mapped_column(Text, nullable=False)
    seen_count: Mapped[int] = mapped_column(Integer)
    first_seen_at: Mapped[str] = mapped_column(Text)
    last_seen_at: Mapped[str] = mapped_column(Text)


def _shrink(text: str, n: int = 60) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def main() -> None:
    engine = create_engine(f"sqlite:///{DB_PATH}", future=True)

    with Session(engine) as session:
        total = session.scalar(select(func.count(Profile.id))) or 0
        print(f"Всего анкет в БД: {total}\n")

        # ── Класс 1: одинаковый текст, разные хеши фото ──────────────────────
        # Считаем только длинную форму описания — "Name, age, city – <about>".
        # Короткие "Name, age, city" без хвоста — игнорируем (там много шумных
        # совпадений по одним городам).
        long_form = Profile.description.like("% – %")
        class1_stmt = (
            select(
                Profile.description.label("desc"),
                func.count(Profile.id).label("size"),
                func.count(func.distinct(Profile.first_media_hash)).label("hashes"),
            )
            .where(long_form)
            .group_by(Profile.description)
            .having(func.count(Profile.id) > 1)
            .having(func.count(func.distinct(Profile.first_media_hash)) > 1)
            .order_by(func.count(Profile.id).desc())
        )
        class1 = session.execute(class1_stmt).all()

        # ── Класс 2: одинаковый хеш фото, разные тексты анкеты ───────────────
        class2_stmt = (
            select(
                Profile.first_media_hash.label("hash"),
                func.count(Profile.id).label("size"),
                func.count(func.distinct(Profile.description)).label("descs"),
            )
            .group_by(Profile.first_media_hash)
            .having(func.count(Profile.id) > 1)
            .having(func.count(func.distinct(Profile.description)) > 1)
            .order_by(func.count(Profile.id).desc())
        )
        class2 = session.execute(class2_stmt).all()

        # ── Класс 3: одинаковые И текст, И хеш ───────────────────────────────
        # При UNIQUE(description, first_media_hash) тут всегда пусто — но
        # проверяем честно, на случай если ограничение когда-то ослабят.
        class3_stmt = (
            select(
                Profile.description.label("desc"),
                Profile.first_media_hash.label("hash"),
                func.count(Profile.id).label("size"),
            )
            .group_by(Profile.description, Profile.first_media_hash)
            .having(func.count(Profile.id) > 1)
            .order_by(func.count(Profile.id).desc())
        )
        class3 = session.execute(class3_stmt).all()

    def report(title: str, rows, sample_fmt) -> tuple[int, int, int]:
        n_groups = len(rows)
        rows_in_groups = sum(r.size for r in rows)
        extra = rows_in_groups - n_groups  # «лишних» — сколько было бы убрано при склейке
        print(f"=== {title} ===")
        print(f"  групп: {n_groups}    записей в группах: {rows_in_groups}    "
              f"«лишних»: {extra}")
        if rows:
            print("  топ-5 групп:")
            for r in rows[:5]:
                print(f"    size={r.size:>3}  {sample_fmt(r)}")
        print()
        return n_groups, rows_in_groups, extra

    g1, _, e1 = report(
        "Класс 1: одинаковый текст, разные хеши фото",
        class1,
        lambda r: f"desc={_shrink(r.desc)!r}",
    )
    g2, _, e2 = report(
        "Класс 2: одинаковый хеш фото, разные тексты",
        class2,
        lambda r: f"hash={r.hash[:12]}…  (разных описаний: {r.descs})",
    )
    g3, _, e3 = report(
        "Класс 3: одинаковые И текст, И хеш",
        class3,
        lambda r: f"desc={_shrink(r.desc)!r}  hash={r.hash[:12]}…",
    )
    if g3 == 0:
        print("  (ожидаемо 0 — в схеме висит UNIQUE(description, first_media_hash))\n")

    print("=== ИТОГО ===")
    print(f"  всего групп-совпадений по всем классам: {g1 + g2 + g3}")
    print(f"  всего «лишних» записей (избыточные строки во всех группах): {e1 + e2 + e3}")


if __name__ == "__main__":
    main()
