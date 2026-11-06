import copy
import hashlib
import json
from . import _edit
from .contract import normalize

WORKFLOWS = {
    "qwen-image-2.1-edit": "qwen-image-2.1-unified.json",
    "qwen-image-2.1-t2i": "qwen-image-2.1-unified.json",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def topology(workflow):
    """Ignore display positions and literal values, retain executable structure/modes."""
    graphs = [workflow] + workflow.get("definitions", {}).get("subgraphs", [])
    structures = []
    for graph in graphs:
        nodes = {n["id"]: n for n in graph.get("nodes", []) if n["type"] != "MarkdownNote"}
        edges = []
        for link in graph.get("links", []):
            if isinstance(link, list):
                _, origin, slot, target, target_slot, kind = link
            else:
                origin, slot, target, target_slot, kind = (link[k] for k in ("origin_id", "origin_slot", "target_id", "target_slot", "type"))
            source = graph["inputs"][slot]["name"] if origin == -10 else slot
            destination = nodes[target]["inputs"][target_slot]["name"] if target in nodes else target_slot
            edges.append([origin, source, target, destination, kind])
        structures.append({
            "nodes": [{"id": n["id"], "type": n["type"], "mode": n.get("mode", 0),
                       "inputs": sorted([{k: i.get(k) for k in ("name", "type")} for i in n.get("inputs", [])], key=lambda x: x["name"]),
                       "widgets": sorted((n.get("widgets_values_named") or {}).keys())}
                      for n in sorted(nodes.values(), key=lambda n: n["id"])],
            "edges": sorted(edges, key=lambda e: json.dumps(e)),
            "inputs": sorted([{k: i.get(k) for k in ("name", "type")} for i in graph.get("inputs", [])], key=lambda x: x["name"]),
        })
    return digest(structures)


def compile_graph(alias, workflow, request, uploaded=None, prefix="comfy-workflow-api/preview"):
    request = normalize(request, alias)
    if alias not in WORKFLOWS:
        raise ValueError(f"Unknown workflow: {alias}")
    if alias == "qwen-image-2.1-t2i":
        if request.get('images'):
            raise ValueError('Use edit alias for reference images')
        request.setdefault('images', [])
    elif not request.get('images'):
        raise ValueError('Edit alias requires 1..10 images')
    if alias in WORKFLOWS:
        _edit.validate_request(request)
        if uploaded is None:
            uploaded = [f"unsubmitted/{i}.png" for i in range(1, len(request["images"]) + 1)]
        if len(uploaded) != len(request["images"]):
            raise ValueError("Uploaded image count differs from requested images")
        return _edit.build_prompt(workflow, uploaded, request, prefix)
    raise ValueError(f"Unknown workflow: {alias}")
