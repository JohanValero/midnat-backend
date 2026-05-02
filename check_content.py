import sqlite3
import os

db_path = "db.sqlite"
if not os.path.exists(db_path):
    db_path = "midnat.db"

if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT content FROM TB_FRAGMENT LIMIT 1;")
    row = cursor.fetchone()
    if row:
        print(row[0])
    else:
        print("No fragments found.")
    conn.close()
else:
    print("Database not found.")
