import json
import pandas as pd

def load_config(path="config.json"):
    with open(path) as f:
        return json.load(f)

# TODO: still working on the agent part
def main():
    cfg = load_config()
    print(cfg)

main()
