#!/usr/bin/env python3
"""Shim: keeps `python3 forecast.py area.csv` working from a source checkout.

The engine itself lives in src/backcountry_water_oracle/. This exists so the
README's commands and the /water-forecast skill don't have to care that it moved,
and so a clone stays runnable with no install step.

It is deliberately NOT a second API surface -- embedders should install the
package and `import backcountry_water_oracle`, not import this file.
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from backcountry_water_oracle import cli

if __name__ == "__main__":
    cli()
