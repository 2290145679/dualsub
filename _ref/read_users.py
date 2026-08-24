import sqlite3, shutil, os

src = r"C:\Users\ZhenZhenNa\Desktop\web\_ref\user_readonly.db"
conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
cur = conn.cursor()
# tables
tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'").fetchall()]
print("tables:", tables)
# find user table
for t in tables:
    if "user" in t.lower():
        cols = [r[1] for r in cur.execute(f"pragma table_info({t})").fetchall()]
        print(f"\n{t} columns: {cols}")
        # only show name, is_superuser, is_active, and hashed password prefix (do not reveal full)
        for row in cur.execute(f"select * from {t}").fetchall():
            d = dict(zip(cols, row))
            hp = d.get("hashed_password") or ""
            print(f"  name={d.get('name')} email={d.get('email')} superuser={d.get('is_superuser')} active={d.get('is_active')} hash_prefix={hp[:15] if hp else None}")
conn.close()
