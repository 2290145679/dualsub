import sqlite3, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

src = r"C:\Users\ZhenZhenNa\Desktop\web\_ref\user_readonly.db"
conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
cur = conn.cursor()
tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'").fetchall()]
print("tables:", tables)

# 找含 hashed_password 列的表(用户表)
for t in tables:
    cols = [r[1] for r in cur.execute(f"pragma table_info({t})").fetchall()]
    if "hashed_password" in cols:
        print(f"\n== USER TABLE: {t} ==")
        print(f"columns: {cols}")
        for row in cur.execute(f"select * from {t}").fetchall():
            d = dict(zip(cols, row))
            hp = str(d.get("hashed_password") or "")
            print(f"  name={d.get('name')} email={d.get('email')} "
                  f"superuser={d.get('is_superuser')} active={d.get('is_active')} "
                  f"hash_prefix={hp[:25] if hp else None}")
conn.close()
