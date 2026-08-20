import json


def load_config(path="config.json"):
    with open(path) as f:
        return json.load(f)


def run_agent(question):
    """Answer a question using the configured greeting."""
    if not question:
        raise ValueError("question must not be empty")
    cfg = load_config()
    return f"{cfg.get('greeting', 'hello')}: {question}"
