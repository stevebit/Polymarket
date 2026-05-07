"""Apply pending migrations + seed station registry. Idempotent."""

from __future__ import annotations

import argparse

from ..db import init_schema_and_seed, table_counts
from ._common import add_common_args, configure_logging


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    args = p.parse_args()
    configure_logging(args.verbose)

    init_schema_and_seed()
    counts = table_counts()
    print("Schema OK. Row counts:")
    for k, v in counts.items():
        print(f"  {k:20s} {v}")


if __name__ == "__main__":
    main()
