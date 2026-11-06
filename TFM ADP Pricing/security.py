"""Tokens, hashes and in-memory rate limits."""
import base64
import hashlib
import hmac
import re
import time
from collections import defaultdict, deque

USERNAME_MIN = 2
USERNAME_MAX = 32
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _sign(secret, message):
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode()


def sha256(value):
    return hashlib.sha256(value.encode('utf-8', 'replace')).hexdigest()


def _same(left, right):
    # compare_digest refuses non-ASCII text, which anyone can put in a URL or cookie.
    return hmac.compare_digest(left.encode('utf-8', 'replace'), right.encode('utf-8', 'replace'))


def session_token(secret, voter_id):
    return _sign(secret, f'session|{voter_id}')


def resume_token(secret, voter_id):
    return f"{voter_id}.{_sign(secret, f'resume|{voter_id}')}"


def voter_id_from_resume_token(secret, token):
    voter_id, _, _ = str(token).partition('.')
    if not voter_id.isascii() or not voter_id.isdigit():
        return None
    if not _same(token, resume_token(secret, voter_id)):
        return None
    return int(voter_id)


def ip_hash(salt, ip):
    return _sign(salt, f'ip|{ip}')


def clean_username(value):
    """Return the cleaned username, or None when it can't be used."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if name.startswith('@'):
        name = name[1:].strip()
    if not USERNAME_MIN <= len(name) <= USERNAME_MAX:
        return None
    if any(not ch.isprintable() for ch in name):
        return None
    return name


def valid_email(value):
    return isinstance(value, str) and len(value) <= 254 and EMAIL_RE.match(value) is not None


def admin_token(secret, password, nonce, expires_at):
    return f"{expires_at}.{_sign(secret, f'admin|{expires_at}|{nonce}|{sha256(password)}')}"


def admin_token_valid(secret, password, nonce, token, now=None):
    expires_at, _, _ = str(token).partition('.')
    if not expires_at.isascii() or not expires_at.isdigit() or len(expires_at) > 12:
        return False
    if int(expires_at) <= (time.time() if now is None else now):
        return False
    return _same(token, admin_token(secret, password, nonce, expires_at))


class RateLimiter:
    """Sliding-window counts per key. Resets on restart, which suits one machine."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._hits = defaultdict(deque)

    def _live(self, key, window):
        hits = self._hits[key]
        cutoff = self._clock() - window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def blocked(self, key, limit, window):
        return len(self._live(key, window)) >= limit

    def hit(self, key):
        self._hits[key].append(self._clock())

    def allow(self, key, limit, window):
        if self.blocked(key, limit, window):
            return False
        self.hit(key)
        return True

    def clear(self, key):
        self._hits.pop(key, None)
