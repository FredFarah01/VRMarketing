"""Background CQC synchronisation job launched from the admin portal."""
import argparse
import json

from account_scoring import score_all
from cqc_sync import run_sync


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--limit", type=int)
    group.add_argument("--full", action="store_true")
    args = parser.parse_args()
    limit = None if args.full else args.limit
    result = run_sync(limit=limit, dry_run=False)
    scoring = score_all()
    print(json.dumps({"sync": result, "scoring": scoring}))


if __name__ == "__main__":
    main()
