import json
import numpy as np

def load_config(path="config.json"):
    with open(path) as f:
        return json.load(f)

def run_agent(question):
    steps = []
    for i in range(3):
        try:
            steps.append(think(question, steps))
        except:
            pass
    return steps

def think(q, steps):
    return q.upper()
