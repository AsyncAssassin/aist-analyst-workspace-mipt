"""Compatibility entrypoint for the extended single migration chain."""
from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).with_name("d07_migrations.py")),run_name="__main__")
