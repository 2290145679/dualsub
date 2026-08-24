import ast
import sys

path = r"C:\Users\ZhenZhenNa\Desktop\web\dualsub-web\plugins\dualsub\__init__.py"
with open(path, encoding="utf-8") as f:
    tree = ast.parse(f.read())

found = []
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef):
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "_PluginBase":
                found.append(node.name)
            elif isinstance(base, ast.Attribute) and base.attr == "_PluginBase":
                found.append(node.name)

print("Plugin classes inheriting _PluginBase:", found)
for n in found:
    print(f"  {n}: starts_with_underscore={n.startswith('_')}")

# Also check required abstract methods are implemented
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name in found:
        methods = [m.name for m in node.body if isinstance(m, ast.FunctionDef)]
        required = ["init_plugin", "get_state", "get_form", "get_page", "get_api", "stop_service"]
        print(f"\n{node.name} methods check:")
        for r in required:
            print(f"  {r}: {'OK' if r in methods else 'MISSING'}")
        break
