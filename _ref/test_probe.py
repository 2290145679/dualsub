import sys
sys.path.insert(0, "/app")
from app.core.plugin import PluginManager

pm = PluginManager()
pid = "DualSub"
inst = pm.running_plugins.get(pid)
if not inst:
    print("DualSub NOT running, trying to get instance")
    # 尝试从 _plugins 取类
    print("loaded plugins:", list(pm._plugins.keys()))
    sys.exit(1)

print(f"DualSub instance: {type(inst).__name__}")
print(f"_enabled: {getattr(inst, '_enabled', '?')}")

# 测试探测几个视频文件
test_files = [
    "/vol2/1000/Emby入库/Lucky.2026.S01.2160p.ATVP.WEB-DL.DDP5.1.Atmos.DV.H.265-DepWeb/Lucky.2026.S01E01.2160p.ATVP.WEB-DL.DDP5.1.Atmos.DV.H.265-DepWeb.mkv",
    "/vol2/1000/Emby入库/Beyond.Times.Gaze.S01.2160p.YOUKU.WEB-DL.AAC2.0.H.265-MWeb/Beyond.Times.Gaze.S01E20.2160p.YOUKU.WEB-DL.AAC2.0.H.265-MWeb.mkv",
    "/vol2/1000/Emby入库/光阴之外.Beyond.Time.S01.2025.2160p.HQ.WEB-DL.H265.AAC-CHDWEB/Beyond.Time.S01E26.2025.2160p.HQ.WEB-DL.H265.AAC-CHDWEB.mkv",
]
for f in test_files:
    import os
    if not os.path.exists(f):
        print(f"\nSKIP (not found): {f}")
        continue
    probe = inst.probe_subtitles(f)
    zh, en = inst.probe_zh_en_tracks(f)
    import os.path as p
    name = p.basename(f)
    print(f"\n{name}")
    print(f"  zh_track={zh}, en_track={en}")
    if "tracks" in probe:
        for t in probe["tracks"]:
            print(f"    track #{t['index']} lang={t['lang']} title={t['title']}")
    elif "error" in probe:
        print(f"    ERROR: {probe['error']}")
