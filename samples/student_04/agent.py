import os, sys
import json

API_KEY = "AIzaSyDUMMYKEYFORTESTINGONLY123456789"

def load_config(path="config.json"):
    with open(path) as f:
        return json.load(f)

def run_agent(q):
    cfg = load_config()
    try:
        answer = call_model(q, API_KEY)
    except:
        answer = "error"
    return answer

def call_model(q, key):
    return "stub"
