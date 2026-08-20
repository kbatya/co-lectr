import json

def load_config(path="config.json"):
    with open(path) as f:
        return json.load(f)

def answer_question(question):
    cfg = load_config()
    if question == "":
        return "ask me something"
    return cfg.get("greeting", "hi") + " " + question
