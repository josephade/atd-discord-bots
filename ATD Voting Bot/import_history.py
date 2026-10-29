"""Import an old response Sheet for on-demand receipts, without polling or DMs."""
import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from sheets import Sheets, sheet_reference
from storage import Store
from votes import parse_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('draft')
    parser.add_argument('sheet_url')
    parser.add_argument('--tiers', type=int, default=10)
    args = parser.parse_args()
    if not args.draft.isdecimal() or not 1 <= args.tiers <= 50:
        parser.error('Use a numeric draft and tier count between 1 and 50.')
    base = Path(__file__).resolve().parent
    load_dotenv(base/'.env', override=True, encoding='utf-8-sig')
    # Resolve paths the same way as a bot started from its own directory.
    os.chdir(base)
    store = Store(Path(os.getenv('STATE_DIR', 'state'))/'voting.db')
    try:
        existing = store.db.execute('SELECT 1 FROM tracks WHERE draft=?', (args.draft,)).fetchone()
        if existing:
            raise SystemExit(f'ATD {args.draft} is already registered; no changes made.')
        sheet, gid = sheet_reference(args.sheet_url)
        sheets = Sheets()
        if gid is None:
            gid = sheets.resolve_gid(sheet)
        ballots = parse_rows(sheets.read(sheet, gid), os.getenv('NAME_COLUMN', 'Enter Discord Name'))
        if not ballots or any(b.error for b in ballots):
            raise SystemExit('Import stopped: no original ballots found or some ballots are incomplete. No changes made.')
        store.register(args.draft, sheet, gid, args.tiers, ballots, active=False)
        print(f'ATD {args.draft}: imported {len(ballots)} historical ballots; {len(ballots[0].teams)} teams per ballot.')
        print(f'Available through !mytierlist {args.draft}. Automatic polling is disabled. No DMs sent.')
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
