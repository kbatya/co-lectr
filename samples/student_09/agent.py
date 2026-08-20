import json
import os

def load_config(path="config.json"):
    with open(path) as f:
        return json.load(f)

def run_agent(question):
    cfg = load_config()
    result = None
    try:
        result = os.popen("echo " + question).read()
    except:
        pass
    return result
