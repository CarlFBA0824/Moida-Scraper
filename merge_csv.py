"""
Merge multiple Moida scrape CSVs (e.g. one per brand) into a single file to
submit to ScanUnlimited/Keepa/master.py as one batch, instead of running
that pipeline separately per brand.

All input files must share the same header (they will, if produced by this
repo's scrape.py) - this just concatenates rows, no deduplication, since
different --vendor-filter runs cover different, non-overlapping products.

Run:
    python merge_csv.py output/moidaus_all-Medicube_*.csv output/moidaus_all-Celimax_*.csv --output output/moidaus_combined.csv
"""

import argparse
import csv
from pathlib import Path
from typing import List


def merge_csvs(input_paths: List[Path], output_path: Path) -> int:
    fieldnames = None
    all_rows = []

    for path in input_paths:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if fieldnames is None:
                fieldnames = reader.fieldnames
            elif reader.fieldnames != fieldnames:
                raise SystemExit(
                    f"{path} has different columns than the first file:\n"
                    f"  first file: {fieldnames}\n"
                    f"  {path.name}: {reader.fieldnames}"
                )
            rows = list(reader)
            all_rows.extend(rows)
            print(f"{path}: {len(rows)} rows")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    return len(all_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge multiple Moida scrape CSVs into one file.")
    parser.add_argument("inputs", nargs="+", help="Paths to the CSV files to merge, in order")
    parser.add_argument("--output", type=str, required=True, help="Path to write the merged CSV")
    args = parser.parse_args()

    input_paths = [Path(p) for p in args.inputs]
    for path in input_paths:
        if not path.exists():
            raise SystemExit(f"Input file not found: {path}")

    output_path = Path(args.output)
    total = merge_csvs(input_paths, output_path)
    print(f"Wrote {total} total rows to {output_path}")


if __name__ == "__main__":
    main()
