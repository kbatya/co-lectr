import json
import time

def load_config(path = "config.json"):
    with open(path) as f :
        return json.load(f)

def run_agent(question, memory = {}):
    memory[question] = time.time()
    try:
        cfg = load_config()
    except:
        cfg = {}
    return memory
