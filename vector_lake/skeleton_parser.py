import os
import json
import ast
from vector_lake.yaml_utils import load_yaml
from vector_lake.template_loader import render_template

_MAX_PY_SKELETON_BYTES = 512 * 1024


def parse_static_skeleton(filepath: str) -> str:
    """
    Deterministically parses highly structured files (Python, JSON, YAML)
    to extract their skeleton without relying on an LLM.
    """
    ext = os.path.splitext(filepath)[1].lower()
    template_name = ""
    variables = {}
    
    if ext not in [".json", ".py", ".yaml", ".yml"]:
        return ""
        
    try:
        if ext == ".json":
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                keys = list(data.keys())
                schema_preview = {k: type(v).__name__ for k, v in data.items()}
                template_name, variables = "wiki/static_skeleton/json_object.md", {"keys": ", ".join(keys), "schema": json.dumps(schema_preview)}
            elif isinstance(data, list):
                length = len(data)
                first_type = type(data[0]).__name__ if length > 0 else "N/A"
                template_name, variables = "wiki/static_skeleton/json_array.md", {"length": length, "first_type": first_type}
                
        elif ext == ".py":
            if os.path.getsize(filepath) > _MAX_PY_SKELETON_BYTES:
                raise ValueError(f"Python file exceeds {_MAX_PY_SKELETON_BYTES} bytes static skeleton limit.")
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            tree = ast.parse(content)
            
            classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
            functions = [
                node.name for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.append(node.names[0].name)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            
            template_name, variables = "wiki/static_skeleton/python.md", {
                "imports": ", ".join(imports) if imports else "None",
                "classes": ", ".join(classes) if classes else "None",
                "functions": ", ".join(functions) if functions else "None",
            }
            
        elif ext in [".yaml", ".yml"]:
            with open(filepath, "r", encoding="utf-8") as f:
                data = load_yaml(f)
            if isinstance(data, dict):
                keys = list(data.keys())
                template_name, variables = "wiki/static_skeleton/yaml.md", {"keys": ", ".join(keys)}
    except Exception as e:
        template_name, variables = "wiki/static_skeleton/error.md", {"error": str(e)}
        
    # Rendering is outside the parse-error handler: missing templates must fail closed.
    return render_template(template_name, **variables) if template_name else ""
