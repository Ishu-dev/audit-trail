"""Helpers available inside the sandbox as `from auditlib import *`.

Reads workbook.json (written by the parser). Standard library only, so the
sandbox image needs no packages and no network.

  v("P&L!D13")          computed value
  f("P&L!D13")          formula text, or None for a typed value
  rel("P&L!D13")        formula in relative form, e.g. =SUM(R[-4]C:R[-2]C)
  is_input("BS!D5")     True if the cell is a typed number, not a formula
  cell("BS!D5")         full record (value, formula, rel, input, hidden, ...)
  row("P&L", 13)        list of cell records in that row, left to right
  label("P&L", 13)      text label of the row (column A or B)
  find_row("P&L", "EBITDA")  row number whose label contains the text
  sheets()              sheet names
  show_row("P&L", 13)   print the row with formulas and values
"""

import json
import os

_WB = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "workbook.json")))
_IDX = {}
for _s in _WB["sheets"]:
    for _c in _s["cells"]:
        _IDX[(_s["name"].upper(), _c["ref"].upper())] = _c


def _split(ref):
    sheet, cell_ref = ref.rsplit("!", 1)
    return sheet.strip("'"), cell_ref.replace("$", "")


def _col_num(col):
    n = 0
    for ch in col:
        n = n * 26 + ord(ch) - 64
    return n


def sheets():
    return [s["name"] for s in _WB["sheets"]]


def cell(ref):
    sheet, c = _split(ref)
    return _IDX.get((sheet.upper(), c.upper()))


def v(ref):
    c = cell(ref)
    return None if c is None else c["value"]


def f(ref):
    c = cell(ref)
    return None if c is None else c["formula"]


def rel(ref):
    c = cell(ref)
    return None if c is None else c["rel"]


def is_input(ref):
    c = cell(ref)
    return bool(c and c["input"])


def row(sheet, r):
    for s in _WB["sheets"]:
        if s["name"].upper() == sheet.upper():
            return sorted((c for c in s["cells"] if c["row"] == r), key=lambda c: _col_num(c["col"]))
    raise KeyError(f"no sheet {sheet!r}; sheets are {sheets()}")


def label(sheet, r):
    for c in row(sheet, r):
        if c["col"] in ("A", "B") and c["is_text"]:
            return c["value"]
    return ""


def find_row(sheet, text):
    for s in _WB["sheets"]:
        if s["name"].upper() == sheet.upper():
            for c in s["cells"]:
                if c["col"] in ("A", "B") and c["is_text"] and text.lower() in str(c["value"]).lower():
                    return c["row"]
    return None


def show_row(sheet, r):
    print(f"{sheet} row {r} '{label(sheet, r)}':")
    for c in row(sheet, r):
        if c["col"] in ("A", "B") and c["is_text"]:
            continue
        print(f"  {c['ref']}: {c['formula'] or 'typed'} | rel {c['rel']} | value {c['value']}")
