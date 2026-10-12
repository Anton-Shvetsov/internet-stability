"""Where the project keeps its files: next to the code, so the folder can be copied as is."""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATIC_DIR = os.path.join(ROOT, "static")
