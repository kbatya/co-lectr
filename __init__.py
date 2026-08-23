# The .env has to be read before anything else in the package: reviewer.py reads
# COLECTR_MODEL at import time, and agent.py builds root_agent from it, both of
# which happen on the line below.
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from . import agent  # noqa: E402
