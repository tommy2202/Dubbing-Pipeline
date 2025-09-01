import json, pathlib
def save(obj, path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
def load(path: pathlib.Path):
    import json
    return json.loads(path.read_text())
