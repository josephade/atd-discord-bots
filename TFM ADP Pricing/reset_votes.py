"""Clear test voters and votes before opening the real vote. This can't be undone."""
import argparse
import os
import sys

from store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yes', action='store_true', help='Confirm permanently removing voters and votes.')
    args = parser.parse_args()
    if not args.yes:
        print('Nothing changed. Add --yes to permanently remove all voters and votes.')
        return 1
    store = Store(os.getenv('DB_PATH', 'data/tfm_adp_pricing.sqlite3'))
    print(store.clear_voting_data())
    store.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
