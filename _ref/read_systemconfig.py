import sqlite3, sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

src = r"C:\Users\ZhenZhenNa\Desktop\web\_ref\user_readonly.db"
conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
cur = conn.cursor()
# systemconfig 表结构
cols = [r[1] for r in cur.execute("pragma table_info(systemconfig)").fetchall()]
print("systemconfig columns:", cols)
# 查看 plugin. 开头的键
rows = cur.execute("select * from systemconfig where key like 'plugin.%'").fetchall()
print(f"\nplugin config keys: {len(rows)}")
for r in rows:
    d = dict(zip(cols, r))
    key = d.get("key", "")
    val = str(d.get("value", ""))
    # 截断显示
    print(f"  {key} = {val[:120]}{'...' if len(val)>120 else ''}")
# 特别看 plugin.DualSub 是否存在
dual = cur.execute("select * from systemconfig where key = 'plugin.DualSub'").fetchall()
print(f"\nplugin.DualSub rows: {len(dual)}")
conn.close()
