#!/usr/bin/env python3
"""Read-only Halifax remortgage snapshot comparison (Python 3.8+).

First run:  python halifax_rate_compare.py
Later:      python halifax_remortgage_scraper.py
            python halifax_rate_compare.py
Accept:     python halifax_rate_compare.py --accept-baseline

The first run creates a local baseline from products.csv. Later runs compare
against it without changing the baseline unless --accept-baseline is passed.
No network calls, database access or notifications.
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ["product_code", "minimum_loan_gbp", "maximum_loan_gbp", "initial_rate_percent",
          "tracker_detail", "initial_period", "rate_type", "end_date", "ltv_band",
          "product_fee_gbp", "premier"]
NUMERIC = {"minimum_loan_gbp", "maximum_loan_gbp", "initial_rate_percent", "product_fee_gbp"}


def load(path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = set(FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError("Missing columns: " + ", ".join(sorted(missing)))
        records = {}
        for line, row in enumerate(reader, start=2):
            code = (row.get("product_code") or "").strip()
            if not code or code in records:
                raise ValueError("Missing/duplicate product code at line {}: {}".format(line, code))
            record = {}
            for field in FIELDS:
                value = (row.get(field) or "").strip()
                if field in NUMERIC:
                    try:
                        value = float(value) if field == "initial_rate_percent" else int(value)
                    except ValueError:
                        raise ValueError("Invalid {} at line {}".format(field, line))
                elif field == "premier":
                    if value.lower() not in ("true", "false"):
                        raise ValueError("Invalid premier at line {}".format(line))
                    value = value.lower() == "true"
                record[field] = value
            if record["minimum_loan_gbp"] > record["maximum_loan_gbp"]:
                raise ValueError("Invalid loan limits at line {}".format(line))
            records[code] = record
    if not records:
        raise ValueError("Snapshot is empty")
    return records


def compare(old, new):
    changes = []
    for code in sorted(set(new) - set(old)):
        changes.append({"product_code": code, "change_type": "new_product", "field": "", "old": "", "new": ""})
    for code in sorted(set(old) - set(new)):
        changes.append({"product_code": code, "change_type": "withdrawn_product", "field": "", "old": "", "new": ""})
    for code in sorted(set(old) & set(new)):
        for field in FIELDS:
            if field == "product_code":
                continue
            if old[code][field] != new[code][field]:
                changes.append({"product_code": code,
                                "change_type": "rate_change" if field == "initial_rate_percent" else "field_change",
                                "field": field, "old": old[code][field], "new": new[code][field]})
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="halifax_diagnostic/remortgage/products.csv")
    parser.add_argument("--baseline", default="halifax_diagnostic/remortgage/baseline.csv")
    parser.add_argument("--report-dir", default="halifax_diagnostic/remortgage/comparison")
    parser.add_argument("--accept-baseline", action="store_true", help="Replace baseline after reporting differences")
    args = parser.parse_args()
    source, baseline, report_dir = Path(args.input), Path(args.baseline), Path(args.report_dir)
    if not source.is_file():
        parser.error("Current snapshot missing: {}. Run the Halifax scraper first.".format(source))
    if source.resolve() == baseline.resolve():
        parser.error("Input and baseline paths must be different")
    try:
        current = load(source)
        if not baseline.exists():
            baseline.parent.mkdir(parents=True, exist_ok=True)
            baseline.write_bytes(source.read_bytes())
            print("Created local baseline: {} ({} products)".format(baseline, len(current)))
            print("Run the scraper again later, then rerun this comparison to detect changes.")
            return 0
        previous = load(baseline)
        changes = compare(previous, current)
        report_dir.mkdir(parents=True, exist_ok=True)
        with (report_dir / "changes.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["product_code", "change_type", "field", "old", "new"])
            writer.writeheader()
            writer.writerows(changes)
        report = {"compared_at": datetime.now(timezone.utc).isoformat(),
                  "baseline_count": len(previous), "current_count": len(current),
                  "change_count": len(changes), "changes": changes}
        (report_dir / "changes.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("Baseline: {} products | Current: {} products".format(len(previous), len(current)))
        print("Detected {} changes".format(len(changes)))
        for change in changes[:30]:
            print("  {product_code}: {change_type} {field} {old} -> {new}".format(**change))
        if len(changes) > 30:
            print("  ... {} more (see report)".format(len(changes) - 30))
        print("Reports: {}".format(report_dir))
        if args.accept_baseline:
            baseline.write_bytes(source.read_bytes())
            print("Baseline explicitly updated")
        else:
            print("Baseline unchanged; read-only comparison complete")
        return 0
    except (OSError, ValueError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
