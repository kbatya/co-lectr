import json

def load_config(path="config.json"):
    with open(path) as f:
        return json.load(f)

def ask(question, history=[]):
    history.append(question)
    return history

def run_agent(question):
    try:
        cfg = load_config()
        return ask(question)
    except:
        return None
