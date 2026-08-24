import sys
sys.path.insert(0, "/app")
from app.core.plugin import PluginManager

pm = PluginManager()
print("=== reloading DualSub ===")
pm.reload_plugin("DualSub")

print("=== plugin ids ===")
ids = pm.get_plugin_ids()
print(ids)

print("=== DualSub loaded? ===")
if "DualSub" in pm._plugins:
    cls = pm._plugins["DualSub"]
    print(f"  class: {cls.__name__}")
    print(f"  plugin_name: {getattr(cls, 'plugin_name', '?')}")
    print(f"  plugin_desc: {getattr(cls, 'plugin_desc', '?')}")
    print(f"  plugin_version: {getattr(cls, 'plugin_version', '?')}")
else:
    print("  DualSub NOT loaded!")

print("=== running plugins ===")
running = pm.running_plugins
print(f"  DualSub running: {'DualSub' in running}")
if "DualSub" in running:
    inst = running["DualSub"]
    print(f"  instance type: {type(inst).__name__}")
    try:
        state = inst.get_state()
        print(f"  get_state: {state}")
    except Exception as e:
        print(f"  get_state error: {e}")
