import os
import json
import requests

def load_config(path="config.json"):
    f = open(path)
    return json.load(f)

def run_agent(question):
    cfg = load_config()
    try:
        r = requests.post(cfg["url"], json={"q": question})
        return r.json()["answer"]
    except:
        print("something went wrong")
