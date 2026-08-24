import sys
sys.path.insert(0, "/app")
from app.helper.plugin import PluginHelper
from app.core.plugin import PluginManager

# 重新安装(把仓库最新文件同步到 app/plugins/dualsub)
pid = "DualSub"
repo_url = "local://DualSub?path=/config/plugins_repo"
print(f"Re-installing {pid} ...")
state, msg = PluginHelper().install_local(pid=pid, repo_url=repo_url, force_install=True)
print(f"install_local: state={state}, msg={msg}")

# 重载
print("=== reloading ===")
pm = PluginManager()
pm.reload_plugin(pid)
print("plugin ids:", pm.get_plugin_ids())
if pid in pm._plugins:
    cls = pm._plugins[pid]
    print(f"LOADED: {cls.__name__}, name={getattr(cls,'plugin_name','?')}, version={getattr(cls,'plugin_version','?')}")
    running = pm.running_plugins
    if pid in running:
        inst = running[pid]
        print(f"RUNNING: {type(inst).__name__}")
        try:
            print(f"  get_state: {inst.get_state()}")
        except Exception as e:
            print(f"  get_state error: {e}")
    else:
        print("  NOT running")
else:
    print("DualSub NOT loaded!")
