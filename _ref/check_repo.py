import sys
sys.path.insert(0, "/app")
from app.core.config import settings
print("PLUGIN_LOCAL_REPO_PATHS =", repr(settings.PLUGIN_LOCAL_REPO_PATHS))
from app.helper.plugin import PluginHelper
c = PluginHelper().get_local_plugin_candidates()
print("local candidates keys:", list(c.keys()))
for pid, info in c.items():
    print(f"  {pid}: version={info.get('version')}, v2={info.get('v2')}, path={info.get('path')}, compat={info.get('system_version_compatible')}")
