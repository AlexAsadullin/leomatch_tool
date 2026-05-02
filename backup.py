"""Manual DB backup. Usage: python3 backup.py"""
import shutil
from app.config import DB_PATH

backup = DB_PATH.with_name("data_backup.db")
if not DB_PATH.exists():
    print(f"Nothing to backup: {DB_PATH} not found")
else:
    shutil.copy2(DB_PATH, backup)
    print(f"Backed up {DB_PATH} -> {backup}")
