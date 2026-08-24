import sys
sys.path.insert(0, "/app")
from app.core.plugin import PluginManager

pid = "DualSub"
pm = PluginManager()

# 默认配置(启用插件 + 开启入库监听, 但不开启通知和手动执行)
conf = {
    "enabled": True,
    "listen_transfer_event": True,
    "send_notify": False,
    "run_now": False,
    "path_list": "",
    "mode": "srt",
    "order": "en_first",
    "backup": True,
    "overwrite": False,
    "min_file_size": 0,
    "skip_if_exists": True,
    "scan_subdirs": True,
    "clear_history": False,
}
print("=== saving config ===")
pm.save_plugin_config(pid, conf)
print("=== init_plugin ===")
pm.init_plugin(pid, conf)
print("=== verify ===")
running = pm.running_plugins
if pid in running:
    inst = running[pid]
    print(f"RUNNING: {type(inst).__name__}")
    print(f"  _enabled: {getattr(inst, '_enabled', '?')}")
    print(f"  _listen_transfer_event: {getattr(inst, '_listen_transfer_event', '?')}")
    try:
        print(f"  get_state: {inst.get_state()}")
    except Exception as e:
        print(f"  get_state error: {e}")
    # 检查消费线程
    ct = getattr(inst, '_consumer_thread', None)
    print(f"  consumer_thread alive: {ct.is_alive() if ct else 'None'}")
    print(f"  _running: {getattr(inst, '_running', '?')}")
else:
    print("DualSub NOT running!")
print("=== plugin config stored ===")
stored = pm.get_plugin_config(pid)
print(f"  enabled in stored: {stored.get('enabled')}")
