"""Workbook parser for Audit Trail.

Turns an .xlsx into a plain JSON dump the agent and the sandbox can both use:
  - every non-empty cell: value, formula, computed result, input flag
  - formulas in relative (R1C1-style) form, so a formula that differs from
    its neighbours in the same row is easy to spot
  - visibility: hidden sheets, hidden rows and columns, white or tiny text
  - comments and defined names

Nothing in the workbook is executed here. Formulas are evaluated by the
`formulas` library, which interprets Excel functions in Python and runs no
macros. All text from the workbook is untrusted data.
"""

from __future__ import annotations

import json
import re
import sys
import warnings
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

warnings.filterwarnings("ignore")

REF_RE = re.compile(
    r"(?P<sheet>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_\.]*)!)?"
    r"(?P<cabs>\$?)(?P<col>[A-Z]{1,3})(?P<rabs>\$?)(?P<row>\d+)(?![\d(])"
)
WHITE = {"FFFFFFFF", "00FFFFFF", "FFFFFF"}
SUSPICIOUS = re.compile(
    r"\b(ignore|disregard|do not report|don't report|system|assistant|ai review|automated review|"
    r"curl|wget|post|upload|send|env|api[_ ]?key|token|password|http[s]?://)\b",
    re.I,
)


def _rgb(font) -> str | None:
    try:
        c = font.color
        if c is not None and c.type == "rgb" and isinstance(c.rgb, str):
            return c.rgb.upper()
    except Exception:
        pass
    return None


def relative_formula(formula: str, row: int, col: int) -> str:
    """=SUM(D9:D11) in D13 becomes =SUM(R[-4]C:R[-2]C). Absolute parts stay absolute."""

    def sub(m: re.Match) -> str:
        sheet = m.group("sheet") or ""
        c = column_index_from_string(m.group("col"))
        r = int(m.group("row"))
        cs = f"C{c}" if m.group("cabs") else ("C" if c == col else f"C[{c - col}]")
        rs = f"R{r}" if m.group("rabs") else ("R" if r == row else f"R[{r - row}]")
        return f"{sheet}{rs}{cs}"

    # leave text in quotes alone
    parts = re.split(r'("[^"]*")', formula)
    return "".join(p if p.startswith('"') else REF_RE.sub(sub, p) for p in parts)


def compute_values(path: Path) -> dict[tuple[str, str], object]:
    import formulas

    model = formulas.ExcelModel().loads(str(path)).finish()
    sol = model.calculate()
    out: dict[tuple[str, str], object] = {}
    for k, v in sol.items():
        k = str(k)
        if "!" not in k or ":" in k.split("!")[-1]:
            continue
        sheet = k.split("]", 1)[1].rsplit("'!", 1)[0]
        cell = k.split("!")[-1]
        try:
            val = v.value[0][0]
        except Exception:
            continue
        if hasattr(val, "item"):
            val = val.item()
        if isinstance(val, (int, float, str, bool)):
            out[(sheet.upper(), cell.upper())] = val
        else:
            out[(sheet.upper(), cell.upper())] = str(val)  # Excel errors like #REF!
    return out


def parse_workbook(path: str | Path, compute: bool = True) -> dict:
    path = Path(path)
    wb = load_workbook(path, data_only=False)
    compute_note = None
    computed = {}
    if compute:
        try:
            computed = compute_values(path)
        except Exception as e:  # unsupported function or broken reference: fall back to Excel's cached values
            compute_note = f"formula engine failed ({type(e).__name__}); using values cached in the file"
            cached = load_workbook(path, data_only=True)
            computed = {(ws.title.upper(), c.coordinate): c.value
                        for ws in cached.worksheets for row in ws.iter_rows() for c in row if c.value is not None}

    sheets = []
    for ws in wb.worksheets:
        hidden_rows = sorted(r for r, d in ws.row_dimensions.items() if d.hidden)
        hidden_cols = sorted(c for c, d in ws.column_dimensions.items() if d.hidden)
        cells = []
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                ref = cell.coordinate
                is_formula = isinstance(cell.value, str) and cell.value.startswith("=")
                rgb = _rgb(cell.font)
                size = cell.font.sz if cell.font and cell.font.sz else None
                hidden_reasons = []
                if ws.sheet_state != "visible":
                    hidden_reasons.append(f"sheet {ws.sheet_state}")
                if cell.row in hidden_rows:
                    hidden_reasons.append("row hidden")
                if cell.column_letter in hidden_cols:
                    hidden_reasons.append("column hidden")
                if rgb in WHITE:
                    hidden_reasons.append("white text")
                if size and size < 6:
                    hidden_reasons.append("tiny text")
                entry = {
                    "ref": ref,
                    "row": cell.row,
                    "col": cell.column_letter,
                    "formula": cell.value if is_formula else None,
                    "rel": relative_formula(cell.value, cell.row, cell.column) if is_formula else None,
                    "value": computed.get((ws.title.upper(), ref), None if is_formula else cell.value),
                    "input": not is_formula and isinstance(cell.value, (int, float)),
                    "is_text": isinstance(cell.value, str) and not is_formula,
                }
                if hidden_reasons:
                    entry["hidden"] = hidden_reasons
                if cell.comment:
                    entry["comment"] = cell.comment.text
                cells.append(entry)
        sheets.append({
            "name": ws.title,
            "state": ws.sheet_state,
            "hidden_rows": hidden_rows,
            "hidden_cols": hidden_cols,
            "cells": cells,
        })

    names = {}
    try:
        for n, d in wb.defined_names.items():
            names[n] = d.attr_text
    except Exception:
        pass

    flags = security_scan(sheets)
    for n, text in names.items():
        hits = sorted({h.lower() for h in SUSPICIOUS.findall(str(text))})
        if len(hits) >= 2:
            flags.append({"location": f"defined name '{n}'", "kind": "defined name", "concealed": ["defined name"],
                          "keywords": hits, "excerpt": str(text)[:300]})
    return {"file": path.name, "sheets": sheets, "defined_names": names,
            "security_flags": flags, "compute_note": compute_note}


def security_scan(sheets: list[dict]) -> list[dict]:
    """Deterministic scan for text that looks like instructions aimed at an AI.

    This runs without the LLM, so a flag is raised even if the model is fooled.
    """
    flags = []
    for s in sheets:
        for c in s["cells"]:
            texts = []
            if c["is_text"]:
                texts.append(("cell", str(c["formula"] or c["value"])))
            if c.get("comment"):
                texts.append(("comment", c["comment"]))
            for kind, t in texts:
                hits = sorted({h.lower() for h in SUSPICIOUS.findall(t)})
                concealed = bool(c.get("hidden"))
                if (concealed and t.strip()) or len(hits) >= 3:
                    flags.append({
                        "location": f"{s['name']}!{c['ref']}",
                        "kind": kind,
                        "concealed": c.get("hidden", []),
                        "keywords": hits,
                        "excerpt": t[:300],
                    })
    return flags


def label_for_row(sheet: dict, row: int) -> str:
    for c in sheet["cells"]:
        if c["row"] == row and c["col"] in ("A", "B") and c["is_text"]:
            return str(c["value"])
    return ""


def fmt_val(v) -> str:
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return str(v)


def column_headers(rows: dict[int, list[dict]]) -> dict:
    """Header row = first row with 3+ text cells right of column B, e.g. FY27E or Jan-27."""
    for r in sorted(rows):
        texts = {c["col"]: str(c["value"]) for c in rows[r]
                 if c["is_text"] and column_index_from_string(c["col"]) > 2}
        if len(texts) >= 3:
            return {**texts, "_row": r}
    return {"_row": None}


def render_for_llm(dump: dict, max_chars: int = 60000) -> str:
    """Compact text view of the workbook, one line per row."""
    out = [f"Workbook: {dump['file']}"]
    if dump.get("compute_note"):
        out.append(f"Note: {dump['compute_note']}")
    if dump.get("defined_names"):
        out.append("Defined names: " + "; ".join(f"{k} = {v}" for k, v in dump["defined_names"].items()))
    for s in dump["sheets"]:
        out.append(f"\n## Sheet '{s['name']}' (state: {s['state']})")
        if s["hidden_rows"] or s["hidden_cols"]:
            out.append(f"hidden rows: {s['hidden_rows']} hidden cols: {s['hidden_cols']}")
        rows: dict[int, list[dict]] = {}
        for c in s["cells"]:
            rows.setdefault(c["row"], []).append(c)
        heads = column_headers(rows)
        for r in sorted(rows):
            parts = []
            for c in sorted(rows[r], key=lambda x: column_index_from_string(x["col"])):
                tag = " [" + ", ".join(c["hidden"]) + "]" if c.get("hidden") else ""
                hd = f" [{heads[c['col']]}]" if c["col"] in heads and r != heads["_row"] else ""
                if c["formula"]:
                    parts.append(f"{c['ref']}{hd} {c['formula']} -> {fmt_val(c['value'])}{tag}")
                elif c["input"]:
                    parts.append(f"{c['ref']}{hd} {fmt_val(c['value'])} (input){tag}")
                else:
                    parts.append(f"{c['ref']} \"{c['value']}\"{tag}")
                if c.get("comment"):
                    parts.append(f"{c['ref']} comment: \"{c['comment']}\"")
            out.append(f"r{r}: " + " | ".join(parts))
    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[truncated: use run_python with the workbook helpers to read the rest]"
    return text


def main() -> None:
    src = Path(sys.argv[1])
    dump = parse_workbook(src)
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".json")
    dst.write_text(json.dumps(dump, indent=1, default=str))
    n = sum(len(s["cells"]) for s in dump["sheets"])
    print(f"{src.name}: {len(dump['sheets'])} sheets, {n} cells, "
          f"{len(dump['security_flags'])} security flags -> {dst}")
    for f in dump["security_flags"]:
        print("  FLAG", f["location"], f["concealed"], f["keywords"])


if __name__ == "__main__":
    main()
