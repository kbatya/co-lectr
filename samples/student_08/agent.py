import json
from typing import List

def load_config(path="config.json"):
    with open(path) as f:
        return json.load(f)

def run_agent(question: str) -> str:
    cfg = load_config()
    tools = cfg.get("tools", [])
    for t in tools:
        try:
            if t["name"] in question:
                return t["reply"]
        except:
            continue
    return "no tool matched"
