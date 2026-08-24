import sys
sys.path.insert(0, "/app")
from app.helper.plugin import PluginHelper

pid = "DualSub"
repo_url = "local://DualSub?path=/config/plugins_repo"
print(f"Installing local plugin {pid} from {repo_url} ...")
state, msg = PluginHelper().install_local(pid=pid, repo_url=repo_url, force_install=True)
print(f"install_local result: state={state}, msg={msg}")

# 注册到已安装列表
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey
install_plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
print(f"installed before: {install_plugins}")
if pid not in install_plugins:
    install_plugins.append(pid)
    SystemConfigOper().set(SystemConfigKey.UserInstalledPlugins, install_plugins)
    print(f"added {pid} to installed list")
else:
    print(f"{pid} already in installed list")
print(f"installed after: {SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins)}")
