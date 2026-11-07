"""TFM ADP Player Pricing: voting site, JSON API and admin page."""
import asyncio
import csv
import hmac
import io
import json
import logging
import os
import time

from aiohttp import web

import emailer
import pricing
import results
import security
import sheets_export
from config import load_settings
from store import Store, utc_now

log = logging.getLogger('tfm_adp_pricing')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
PLAYERS_FILE = os.path.join(BASE_DIR, 'players.json')
PHOTOS_FILE = os.path.join(BASE_DIR, 'player_photos.json')

SESSION_COOKIE = 'tfm_session'
ADMIN_COOKIE = 'tfm_admin'
ADMIN_SESSION_SECONDS = 12 * 60 * 60
VOTER_COOKIE_SECONDS = 180 * 24 * 60 * 60
MAX_BODY_BYTES = 16 * 1024
REASON_MAX = 500

NEW_VOTER_LIMIT, NEW_VOTER_WINDOW = 10, 60 * 60
VOTE_LIMIT, VOTE_WINDOW = 60, 60
EMAIL_LIMIT, EMAIL_WINDOW = 3, 60 * 60
LOGIN_FAILURE_LIMIT, LOGIN_LOCKOUT = 5, 15 * 60

CSP = (
    "default-src 'self'; img-src 'self' data: https://cdn.nba.com https://thumb.wikimedia.org https://upload.wikimedia.org; "
    "style-src 'self'; script-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

SETTINGS = web.AppKey('settings', object)
STORE = web.AppKey('store', Store)
LIMITER = web.AppKey('limiter', security.RateLimiter)
EMAIL_SENDER = web.AppKey('email_sender', object)
SHEETS_EXPORTER = web.AppKey('sheets_exporter', object)
EXPORT_LOCK = web.AppKey('export_lock', asyncio.Lock)
PHOTOS = web.AppKey('photos', dict)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


@web.middleware
async def api_middleware(request, handler):
    is_api = request.path.startswith('/api/')
    try:
        if is_api and request.method in {'POST', 'PUT', 'DELETE'} and request.content_type != 'application/json':
            raise ApiError(415, 'Send JSON.')
        response = await handler(request)
    except ApiError as exc:
        response = web.json_response({'error': exc.message}, status=exc.status)
    except web.HTTPException as exc:
        if is_api:
            response = web.json_response({'error': exc.reason}, status=exc.status)
        else:
            response = web.Response(text=exc.reason, status=exc.status)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception('Unhandled error on %s %s', request.method, request.rel_url.path)
        response = web.json_response({'error': 'Something went wrong.'}, status=500)

    response.headers['Content-Security-Policy'] = CSP
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store' if is_api else 'no-cache'
    return response


# Helpers

def client_ip(request):
    if request.app[SETTINGS].trust_fly_client_ip:
        forwarded = request.headers.get('Fly-Client-IP')
        if forwarded:
            return forwarded
    return request.remote or 'unknown'


async def read_json(request):
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, 'That request could not be read.')
    if not isinstance(body, dict):
        raise ApiError(400, 'That request could not be read.')
    return body


def int_param(request, name):
    try:
        value = int(request.match_info[name])
    except ValueError:
        raise web.HTTPNotFound()
    # SQLite can't take integers past 64 bits, and no real ID comes close.
    if not 0 <= value < 2 ** 31:
        raise web.HTTPNotFound()
    return value


def require_open(request):
    if not request.app[STORE].voting_open():
        raise ApiError(403, 'Voting is closed.')


def session_hash(settings, voter_id):
    return security.sha256(security.session_token(settings.session_secret, voter_id))


def current_voter(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return request.app[STORE].voter_by_session_hash(security.sha256(token))


def require_voter(request):
    voter = current_voter(request)
    if voter is None:
        raise ApiError(401, 'Enter your Discord username to start.')
    return voter


def set_session_cookie(response, request, voter_id):
    settings = request.app[SETTINGS]
    response.set_cookie(
        SESSION_COOKIE,
        security.session_token(settings.session_secret, voter_id),
        max_age=VOTER_COOKIE_SECONDS,
        path='/',
        httponly=True,
        secure=settings.cookie_secure,
        samesite='Lax',
    )


def resume_link(request, voter_id):
    settings = request.app[SETTINGS]
    base = settings.public_base_url or f'{request.scheme}://{request.host}'
    return f'{base}/r/{security.resume_token(settings.session_secret, voter_id)}'


def voter_payload(store, voter):
    return dict(
        username=voter['discord_username'],
        answered=store.answered_count(voter['id']),
        total=store.player_count(),
    )


def answer_payload(row):
    if row is None:
        return None
    return dict(answer_type=row['answer_type'], price=row['price'])


# Pages

async def index_page(request):
    return web.FileResponse(os.path.join(STATIC_DIR, 'index.html'))


async def admin_page(request):
    return web.FileResponse(os.path.join(STATIC_DIR, 'admin.html'))


async def healthz(request):
    return web.json_response({'ok': True})


async def open_resume_link(request):
    settings = request.app[SETTINGS]
    voter_id = security.voter_id_from_resume_token(settings.session_secret, request.match_info['token'])
    if voter_id is None or request.app[STORE].voter(voter_id) is None:
        return web.Response(status=302, headers={'Location': '/?link=invalid'})
    response = web.Response(status=302, headers={'Location': '/'})
    set_session_cookie(response, request, voter_id)
    return response


# Voter API

async def get_me(request):
    store = request.app[STORE]
    voter = current_voter(request)
    return web.json_response(dict(
        voting_open=store.voting_open(),
        voter=voter_payload(store, voter) if voter else None,
    ))


async def create_session(request):
    store = request.app[STORE]
    settings = request.app[SETTINGS]
    require_open(request)
    body = await read_json(request)
    username = security.clean_username(body.get('username'))
    if username is None:
        raise ApiError(400, 'Enter a Discord username from 2 to 32 characters.')
    existing = current_voter(request)
    if existing and existing['discord_username'].casefold() == username.casefold():
        response = web.json_response({'voter': voter_payload(store, existing)})
        set_session_cookie(response, request, existing['id'])
        return response
    if any(v['discord_username'].casefold() == username.casefold() for v in store.voters()):
        raise ApiError(409, 'That username already has a voting session. Use your private resume link to return to it. If you lost the link, ask the commissioner to recover your existing session. No new session was created.')
    ip = client_ip(request)
    if not request.app[LIMITER].allow(f'voter:{ip}', NEW_VOTER_LIMIT, NEW_VOTER_WINDOW):
        raise ApiError(429, 'Too many new voters from this network. Try again later.')
    voter = store.create_voter(
        username,
        pricing.build_order(store.players()),
        security.ip_hash(settings.ip_hash_salt, ip),
        lambda voter_id: session_hash(settings, voter_id),
    )
    response = web.json_response({'voter': voter_payload(store, voter)})
    set_session_cookie(response, request, voter['id'])
    return response


async def get_card(request):
    store = request.app[STORE]
    voter = require_voter(request)
    order = json.loads(voter['player_order'])
    answers = store.answers_for(voter['id'])
    raw_position = request.query.get('position')
    if raw_position is None:
        position = next((i for i, pid in enumerate(order, start=1) if pid not in answers), None)
    else:
        try:
            position = int(raw_position)
        except ValueError:
            raise ApiError(400, 'That position is not valid.')
        if not 1 <= position <= len(order):
            raise ApiError(400, 'That position is not valid.')

    if position is None:
        return web.json_response(dict(done=True, answered=len(answers), total=len(order)))

    player = store.player(order[position - 1])
    return web.json_response(dict(
        done=False,
        position=position,
        answered=len(answers),
        total=len(order),
        player=dict(id=player['id'], name=player['name'], current_price=player['current_price'], photo=request.app[PHOTOS].get(player['name'])),
        options=pricing.options_for(player['current_price']),
        answer=answer_payload(answers.get(player['id'])),
        other_max=pricing.OTHER_MAX,
    ))


async def get_my_votes(request):
    store = request.app[STORE]
    voter = require_voter(request)
    return web.json_response(voter_report(store, voter))


def voter_report(store, voter):
    """One report calculation shared by voters and authenticated admin views."""
    answers = store.answers_for(voter['id'])
    players = {p['id']: p for p in store.players()}
    rows = []
    for position, player_id in enumerate(json.loads(voter['player_order']), 1):
        player, answer = players[player_id], answers.get(player_id)
        priced = answer is not None and answer['answer_type'] != 'dont_know'
        rows.append(dict(player_id=player_id, position=position, name=player['name'],
                         current_price=player['current_price'], answered=answer is not None,
                         answer_type=answer['answer_type'] if answer else None,
                         price=answer['price'] if answer else None,
                         difference=answer['price']-player['current_price'] if priced else None,
                         excluded=bool(answer and answer['deleted_at'] is not None)))
    priced = [r for r in rows if r['difference'] is not None]
    return dict(username=voter['discord_username'], voting_open=store.voting_open(),
        total=len(rows), answered=len(answers), priced=len(priced),
        dont_know=sum(r['answer_type']=='dont_know' for r in rows),
        excluded=sum(r['excluded'] for r in rows), players=rows,
        biggest_increases=sorted((r for r in priced if r['difference']>0), key=lambda r:(-r['difference'],r['name']))[:10],
        biggest_decreases=sorted((r for r in priced if r['difference']<0), key=lambda r:(r['difference'],r['name']))[:10])


async def put_vote(request):
    store = request.app[STORE]
    voter = require_voter(request)
    require_open(request)
    if not request.app[LIMITER].allow(f"vote:{voter['id']}", VOTE_LIMIT, VOTE_WINDOW):
        raise ApiError(429, 'Slow down a little, then try again.')
    player_id = int_param(request, 'player_id')
    player = store.player(player_id)
    if player is None or player_id not in json.loads(voter['player_order']):
        raise ApiError(404, 'That player is not in this vote.')

    body = await read_json(request)
    answer_type = body.get('answer_type')
    price = body.get('price')
    if answer_type == 'dont_know':
        price = None
    elif answer_type == 'option':
        if type(price) is not int or price not in pricing.options_for(player['current_price']):
            raise ApiError(400, 'That price is not one of the options.')
    elif answer_type == 'other':
        if not pricing.valid_other(price):
            raise ApiError(400, f'Enter a whole dollar amount from ${pricing.OTHER_MIN} to ${pricing.OTHER_MAX}.')
    else:
        raise ApiError(400, 'That answer is not valid.')

    store.save_vote(voter['id'], player_id, answer_type, price)
    return web.json_response(dict(
        answered=store.answered_count(voter['id']),
        total=store.player_count(),
    ))


async def get_resume_link(request):
    voter = require_voter(request)
    return web.json_response({'link': resume_link(request, voter['id'])})


async def continue_later(request):
    store = request.app[STORE]
    settings = request.app[SETTINGS]
    voter = require_voter(request)
    require_open(request)
    body = await read_json(request)
    address = body.get('email')
    if isinstance(address, str):
        address = address.strip()
    if not security.valid_email(address):
        raise ApiError(400, 'Enter a valid email address.')

    link = resume_link(request, voter['id'])
    if not emailer.email_configured(settings) and request.app[EMAIL_SENDER] is emailer.send_resume_email:
        return web.json_response(dict(sent=False, link=link, error='Email isn\'t available right now. Copy your link instead.'))
    if not request.app[LIMITER].allow(f"email:{voter['id']}", EMAIL_LIMIT, EMAIL_WINDOW):
        raise ApiError(429, 'You\'ve requested 3 emails this hour. Copy your link instead.')

    try:
        await asyncio.to_thread(
            request.app[EMAIL_SENDER], settings, address, link,
            store.answered_count(voter['id']), store.player_count(),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The exception type only: SMTP errors can echo the recipient's address.
        log.warning('Resume email failed: %s', type(exc).__name__)
        return web.json_response(dict(sent=False, link=link, error='That email didn\'t send. Copy your link instead.'))
    return web.json_response(dict(sent=True, link=link))


# Admin auth

def require_admin(handler):
    async def wrapped(request):
        settings = request.app[SETTINGS]
        token = request.cookies.get(ADMIN_COOKIE)
        if not settings.admin_password or not token or not security.admin_token_valid(
            settings.session_secret, settings.admin_password, request.app[STORE].admin_nonce(), token
        ):
            raise ApiError(401, 'Log in to continue.')
        return await handler(request)
    return wrapped


async def admin_login(request):
    settings = request.app[SETTINGS]
    limiter = request.app[LIMITER]
    if not settings.admin_password:
        raise ApiError(503, 'The admin password has not been set.')
    key = f'login:{client_ip(request)}'
    if limiter.blocked(key, LOGIN_FAILURE_LIMIT, LOGIN_LOCKOUT):
        raise ApiError(429, 'Too many failed attempts. Try again in 15 minutes.')
    body = await read_json(request)
    password = body.get('password')
    if not isinstance(password, str) or not hmac.compare_digest(
        security.sha256(password), security.sha256(settings.admin_password)
    ):
        limiter.hit(key)
        raise ApiError(401, 'That password is not correct.')
    limiter.clear(key)
    expires_at = int(time.time()) + ADMIN_SESSION_SECONDS
    token = security.admin_token(
        settings.session_secret, settings.admin_password, request.app[STORE].admin_nonce(), expires_at
    )
    response = web.json_response({'ok': True})
    response.set_cookie(
        ADMIN_COOKIE, token, max_age=ADMIN_SESSION_SECONDS, path='/',
        httponly=True, secure=settings.cookie_secure, samesite='Strict',
    )
    return response


@require_admin
async def admin_logout(request):
    # Rotating the nonce ends every admin session, including any copy of this cookie.
    request.app[STORE].rotate_admin_nonce()
    response = web.json_response({'ok': True})
    response.del_cookie(ADMIN_COOKIE, path='/')
    return response


# Admin results

def results_snapshot(store):
    players = store.players()
    voters = store.voters()
    all_votes = store.all_votes()
    active = store.active_votes()
    rows = results.player_rows(players, active)
    return dict(
        overview=results.overview(players, voters, all_votes, active),
        players=rows,
        gaps=results.biggest_gaps(rows),
        fewest_votes=sorted(rows, key=lambda r: (r['votes'], r['id']))[:10],
        voters=results.voter_rows(players, voters, all_votes),
    )


@require_admin
async def admin_players(request):
    store = request.app[STORE]
    snapshot = results_snapshot(store)
    dashboard = results.dashboard(store.players(), store.voters(), store.all_votes(), store.active_votes(),
                                  snapshot['players'], snapshot['voters'])
    recent_actions = [dict(row) for row in store.db.execute(
        'SELECT action,target,created_at FROM admin_actions ORDER BY id DESC LIMIT 6')]
    export_row = store.db.execute("SELECT action,created_at FROM admin_actions WHERE action IN ('export_sheets','export_csv') ORDER BY id DESC LIMIT 1").fetchone()
    return web.json_response(dict(
        generated_at=utc_now(), dashboard=dashboard, recent_actions=recent_actions,
        last_export=dict(export_row) if export_row else None,
        voting_open=store.voting_open(),
        overview=snapshot['overview'],
        players=snapshot['players'],
        combined_adp=results.combined_adp(snapshot['players'], store.active_votes()),
        gaps=snapshot['gaps'],
        fewest_votes=snapshot['fewest_votes'],
        email_ready=emailer.email_configured(request.app[SETTINGS]),
        sheets_ready=sheets_export.configured(request.app[SETTINGS]),
    ))


@require_admin
async def admin_player_detail(request):
    store = request.app[STORE]
    player = store.player(int_param(request, 'player_id'))
    if player is None:
        raise ApiError(404, 'That player was not found.')
    votes = store.votes_for_player(player['id'])
    return web.json_response(dict(
        player=dict(id=player['id'], name=player['name'], current_price=player['current_price']),
        histogram=results.histogram(player['current_price'], votes),
        votes=[dict(
            id=v['id'],
            voter_id=v['voter_id'],
            username=v['discord_username'],
            answer_type=v['answer_type'],
            price=v['price'],
            updated_at=v['updated_at'],
        ) for v in votes],
        summary=results.player_rows([player], votes)[0],
    ))


@require_admin
async def admin_voters(request):
    store = request.app[STORE]
    return web.json_response({'voters': results.voter_rows(store.players(), store.voters(), store.all_votes())})


@require_admin
async def admin_voter_report(request):
    store = request.app[STORE]
    voter = store.voter(int_param(request, 'voter_id'))
    if voter is None:
        raise ApiError(404, 'That voter session does not exist or was deleted.')
    return web.json_response(voter_report(store, voter))


@require_admin
async def admin_voter_resume_link(request):
    voter = request.app[STORE].voter(int_param(request, 'voter_id'))
    if voter is None:
        raise ApiError(404, 'That voter session does not exist or was deleted.')
    return web.json_response({'link': resume_link(request, voter['id'])})


@require_admin
async def admin_voter_detail(request):
    store = request.app[STORE]
    voter = store.voter(int_param(request, 'voter_id'))
    if voter is None:
        raise ApiError(404, 'That voter was not found.')
    return web.json_response(dict(
        voter=dict(id=voter['id'], username=voter['discord_username']),
        votes=[dict(
            id=v['id'],
            player=v['player_name'],
            current_price=v['current_price'],
            answer_type=v['answer_type'],
            price=v['price'],
            created_at=v['created_at'],
            updated_at=v['updated_at'],
            deleted=v['deleted_at'] is not None,
        ) for v in store.votes_for_voter(voter['id'])],
    ))


async def read_reason(request):
    body = await read_json(request)
    reason = body.get('reason')
    reason = reason.strip() if isinstance(reason, str) else ''
    if not reason or len(reason) > REASON_MAX:
        raise ApiError(400, f'Enter a reason of up to {REASON_MAX} characters.')
    return reason


@require_admin
async def admin_delete_vote(request):
    reason = await read_reason(request)
    batch_id = request.app[STORE].delete_vote(int_param(request, 'vote_id'), reason)
    if batch_id is None:
        raise ApiError(409, 'That vote is already deleted or does not exist.')
    return web.json_response({'batch_id': batch_id})


@require_admin
async def admin_delete_voter_votes(request):
    reason = await read_reason(request)
    batch_id = request.app[STORE].delete_voter_votes(int_param(request, 'voter_id'), reason)
    if batch_id is None:
        raise ApiError(409, 'That voter has no votes left to delete.')
    return web.json_response({'batch_id': batch_id})


@require_admin
async def admin_delete_voter(request):
    reason = await read_reason(request)
    batch_id = request.app[STORE].delete_voter(int_param(request, 'voter_id'), reason)
    if batch_id is None:
        raise ApiError(409, 'That voter session was already deleted or does not exist.')
    return web.json_response({'batch_id': batch_id})


@require_admin
async def admin_deletion_batches(request):
    return web.json_response({'batches': [dict(
        id=b['id'],
        scope=b['scope'],
        voter_id=b['voter_id'],
        username=b['discord_username'],
        reason=b['reason'],
        vote_count=b['vote_count'],
        created_at=b['created_at'],
        restored_at=b['restored_at'],
        removes_voter=bool(b['removes_voter']),
    ) for b in request.app[STORE].deletion_batches()]})


@require_admin
async def admin_restore_batch(request):
    try:
        restored = request.app[STORE].restore_batch(int_param(request, 'batch_id'))
    except ValueError as exc:
        raise ApiError(409, str(exc))
    if restored is None:
        raise ApiError(409, 'That batch was already restored or does not exist.')
    return web.json_response({'restored': restored})


@require_admin
async def admin_set_voting_open(request):
    store = request.app[STORE]
    body = await read_json(request)
    if type(body.get('open')) is not bool:
        raise ApiError(400, 'Send true or false.')
    store.set_setting('voting_open', '1' if body['open'] else '0')
    store.log_action('voting_open' if body['open'] else 'voting_closed', 'site')
    return web.json_response({'voting_open': store.voting_open()})


def csv_safe(value):
    # Stops a username like =IMPORTXML(...) from running as a spreadsheet formula.
    if isinstance(value, str) and value[:1] in {'=', '+', '-', '@', '\t', '\r'}:
        return "'" + value
    return value


def export_vote_rows(store):
    names = {v['id']: v['discord_username'] for v in store.voters()}
    players = {p['id']: p['name'] for p in store.players()}
    return [dict(
        username=names[v['voter_id']],
        player=players[v['player_id']],
        answer_type=v['answer_type'],
        price=v['price'],
        time=v['updated_at'],
    ) for v in store.active_votes()]


@require_admin
async def admin_export_csv(request):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Username', 'Player', 'Answer type', 'Price', 'Time'])
    for row in export_vote_rows(request.app[STORE]):
        writer.writerow([
            csv_safe(row['username']), row['player'], row['answer_type'],
            '' if row['price'] is None else row['price'], row['time'],
        ])
    request.app[STORE].log_action('export_csv', 'votes')
    return web.Response(
        text=output.getvalue(),
        content_type='text/csv',
        headers={'Content-Disposition': 'attachment; filename="tfm-adp-pricing-votes.csv"'},
    )


@require_admin
async def admin_export_sheets(request):
    store = request.app[STORE]
    settings = request.app[SETTINGS]
    if not sheets_export.configured(settings) and request.app[SHEETS_EXPORTER] is sheets_export.export_to_sheets:
        raise ApiError(503, 'The Google Sheets export has not been set up yet.')
    lock = request.app[EXPORT_LOCK]
    if lock.locked():
        raise ApiError(409, 'An export is already running.')
    async with lock:
        snapshot = results_snapshot(store)
        tabs = sheets_export.build_tabs(
            snapshot['players'], snapshot['gaps'], export_vote_rows(store), snapshot['voters'], utc_now()
        )
        try:
            url = await asyncio.to_thread(request.app[SHEETS_EXPORTER], settings, tabs)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Google Sheets export failed')
            raise ApiError(502, 'The Google Sheets export failed. Check the server logs.')
    store.log_action('export_sheets', 'results spreadsheet')
    return web.json_response({'url': url})


# App

def load_players(path=PLAYERS_FILE):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def load_photos(path=PHOTOS_FILE):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def create_app(settings, email_sender=None, sheets_exporter=None, store=None):
    app = web.Application(middlewares=[api_middleware], client_max_size=MAX_BODY_BYTES)
    app[SETTINGS] = settings
    app[STORE] = store or Store(settings.db_path)
    app[LIMITER] = security.RateLimiter()
    app[EMAIL_SENDER] = email_sender or emailer.send_resume_email
    app[SHEETS_EXPORTER] = sheets_exporter or sheets_export.export_to_sheets
    app[EXPORT_LOCK] = asyncio.Lock()
    app[PHOTOS] = load_photos()
    app[STORE].seed_players(load_players())

    async def close_store(app):
        app[STORE].close()
    app.on_cleanup.append(close_store)

    app.add_routes([
        web.get('/', index_page),
        web.get('/admin', admin_page),
        web.get('/healthz', healthz),
        web.get('/r/{token}', open_resume_link),
        web.get('/api/me', get_me),
        web.get('/api/my-votes', get_my_votes),
        web.post('/api/session', create_session),
        web.get('/api/card', get_card),
        web.put('/api/votes/{player_id}', put_vote),
        web.get('/api/resume-link', get_resume_link),
        web.post('/api/continue-later', continue_later),
        web.post('/api/admin/login', admin_login),
        web.post('/api/admin/logout', admin_logout),
        web.get('/api/admin/players', admin_players),
        web.get('/api/admin/players/{player_id}', admin_player_detail),
        web.get('/api/admin/voters', admin_voters),
        web.get('/api/admin/voters/{voter_id}', admin_voter_detail),
        web.get('/api/admin/voters/{voter_id}/resume-link', admin_voter_resume_link),
        web.get('/api/admin/voters/{voter_id}/report', admin_voter_report),
        web.delete('/api/admin/voters/{voter_id}', admin_delete_voter),
        web.delete('/api/admin/votes/{vote_id}', admin_delete_vote),
        web.delete('/api/admin/voters/{voter_id}/votes', admin_delete_voter_votes),
        web.get('/api/admin/deletion-batches', admin_deletion_batches),
        web.post('/api/admin/deletion-batches/{batch_id}/restore', admin_restore_batch),
        web.put('/api/admin/settings/voting-open', admin_set_voting_open),
        web.post('/api/admin/export/sheets', admin_export_sheets),
        web.get('/api/admin/export.csv', admin_export_csv),
        web.static('/static', STATIC_DIR),
    ])
    return app


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    settings = load_settings()
    # No access log: resume links carry a private token in the URL.
    web.run_app(create_app(settings), host='0.0.0.0', port=int(os.getenv('PORT', '8080')), access_log=None)


if __name__ == '__main__':
    main()
