"""Merge a verified ATD 112 addition into the live archive without changing history."""
import hashlib
import json
import os
from pathlib import Path


def main():
    target = Path('/data/lottos.json')
    original = target.read_bytes()
    expected = Path('/tmp/atd-112-archive-sha.txt').read_text().strip()
    if hashlib.sha256(original).hexdigest() != expected:
        raise RuntimeError('Live archive changed since inspection; refresh the backup first')
    archive = json.loads(original)
    additions = json.loads(Path('/tmp/atd-112-addition.json').read_text())
    assert set(additions) == {'ATD 112'}
    assert len(additions['ATD 112']) == 30
    assert sum(len(e['players']) for e in additions['ATD 112'].values()) == 300
    if 'ATD 112' in archive:
        raise RuntimeError('ATD 112 already exists; refusing to replace it')
    backup = Path('/data/lottos-before-atd112.json')
    if backup.exists():
        raise RuntimeError('Backup already exists; inspect the previous attempt first')
    backup.write_bytes(original)
    archive.update(additions)
    temporary = target.with_suffix('.112.tmp')
    temporary.write_text(json.dumps(archive, indent=2, ensure_ascii=False), encoding='utf-8')
    assert target.read_bytes() == original, 'Live archive changed before save'
    os.replace(temporary, target)
    saved = json.loads(target.read_text(encoding='utf-8'))
    assert {k: v for k, v in saved.items() if k != 'ATD 112'} == json.loads(original)
    assert saved['ATD 112'] == additions['ATD 112']
    print('Imported and verified ATD 112: 30 teams, 300 roster entries, 38 drafters.')
    print('All previous drafts preserved. The bot reloads this file for each command.')


if __name__ == '__main__':
    main()
