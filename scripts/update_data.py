"""Refresh public NBA data for COURTSIDE. Standard library only; no API keys."""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import copy
import datetime as dt
import gzip
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
from zoneinfo import ZoneInfo

NBA_TEAMS = {1610612737:'ATL',1610612738:'BOS',1610612739:'CLE',1610612740:'NOP',1610612741:'CHI',1610612742:'DAL',1610612743:'DEN',1610612744:'GSW',1610612745:'HOU',1610612746:'LAC',1610612747:'LAL',1610612748:'MIA',1610612749:'MIL',1610612750:'MIN',1610612751:'BKN',1610612752:'NYK',1610612753:'ORL',1610612754:'IND',1610612755:'PHI',1610612756:'PHX',1610612757:'POR',1610612758:'SAC',1610612759:'SAS',1610612760:'OKC',1610612761:'TOR',1610612762:'UTA',1610612763:'MEM',1610612764:'WAS',1610612765:'DET',1610612766:'CHA'}
TYPES = {1:'Pre Season',2:'Regular Season',3:'Playoffs'}
BOX_FIELDS = ['PLAYER_ID','PLAYER_NAME','TEAM_ID','GAME_ID','GAME_DATE','MATCHUP','WL','MIN','PTS','REB','AST','FGM','FGA','FG3M','FG3A','STL','BLK']
HTTP_HEADERS = {
    'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36',
    'Referer':'https://www.nba.com/', 'Origin':'https://www.nba.com',
    'Accept':'application/json,text/html;q=0.9,*/*;q=0.8',
    'Accept-Language':'en-US,en;q=0.9',
}

def decode_response(raw, as_json):
    if len(raw)>25_000_000:
        raise ValueError('來源回應超過預期大小')
    if raw[:2]==b'\x1f\x8b':
        raw = gzip.decompress(raw)
    if len(raw)>25_000_000:
        raise ValueError('解壓縮後的來源回應超過預期大小')
    text = raw.decode('utf-8')
    return json.loads(text) if as_json else text

def headers_for(url):
    headers = dict(HTTP_HEADERS)
    if (urllib.parse.urlsplit(url).hostname or '').endswith('.espn.com'):
        headers.pop('Origin',None)
        headers['Referer'] = 'https://www.espn.com/'
    return headers

def curl_request(url, as_json, timeout):
    """Try the runner's system HTTP client when urllib receives a rejection."""
    executable = shutil.which('curl')
    if not executable:
        raise RuntimeError('系統未提供 curl 備援連線')
    command = [executable,'--silent','--show-error','--fail','--location',
               '--proto','=https','--proto-redir','=https','--max-redirs','3',
               '--compressed','--connect-timeout','10','--max-time',str(timeout),
               '--max-filesize','25000000']
    for name,value in headers_for(url).items():
        command.extend(['--header',name+': '+value])
    command.append(url)
    result = subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                            timeout=timeout+5,check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8',errors='replace').strip()[:400])
    return decode_response(result.stdout,as_json)

def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

def normalize(value):
    return re.sub(r'[^a-z0-9\u3400-\u9fff]', '', unicodedata.normalize('NFKD', value or '').lower())

def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def request(url, as_json=True):
    last_error = None
    cdn = urllib.parse.urlsplit(url).hostname=='cdn.nba.com'
    small_box = cdn and '/liveData/boxscore/' in url
    # The season schedule is several MB; do not give it the short box-score timeout.
    timeout = 10 if small_box else 45 if cdn else 25
    attempts = 2 if small_box else 3
    if (urllib.parse.urlsplit(url).hostname or '').endswith('.espn.com'):
        timeout,attempts = 20,2
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers_for(url))
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return decode_response(response.read(25_000_001),as_json)
        except Exception as error:
            last_error = error
            if isinstance(error,urllib.error.HTTPError) and error.code in (403,404):
                if error.code==403:
                    try:
                        return curl_request(url,as_json,timeout)
                    except Exception as curl_error:
                        last_error = RuntimeError(f'urllib: HTTP 403; curl: {curl_error}')
                break
            if attempt<attempts-1:
                time.sleep(2*(attempt+1))
    raise RuntimeError(f'來源讀取失敗 [{url}]：{last_error}') from last_error

def table(payload, name, required):
    result = next((r for r in payload.get('resultSets', []) if r.get('name')==name), None)
    if result is None or not set(required).issubset(result.get('headers', [])) or not isinstance(result.get('rowSet'), list):
        raise ValueError('NBA 資料格式變更，保留舊資料')
    headers = result['headers']
    if any(len(row)!=len(headers) for row in result['rowSet']):
        raise ValueError('NBA 回傳資料欄位不完整')
    return [dict(zip(headers, row)) for row in result['rowSet']]

def summarize(games):
    n = len(games)
    def avg(key):
        return sum(g[key] for g in games)/n if n else None
    def pct(made, attempted):
        denominator = sum(g[attempted] for g in games)
        return sum(g[made] for g in games)/denominator*100 if denominator else None
    return {'gp':n, 'pts':avg('pts'), 'reb':avg('reb'), 'ast':avg('ast'), 'min':avg('min'),
            'stl':avg('stl'), 'blk':avg('blk'), 'fg':pct('fgm','fga'), 'tp':pct('tpm','tpa')}

def build_period(payload, year, typ, identities, roster, at):
    parameters = payload.get('parameters', {})
    season = f'{year-1}-{str(year)[-2:]}'
    if parameters.get('SeasonYear')!=season or parameters.get('SeasonType')!=TYPES[typ] or parameters.get('LastNGames')!=0:
        raise ValueError('NBA 回傳賽季、賽事類型或完整出賽範圍不符')
    required = ['PLAYER_ID','PLAYER_NAME','TEAM_ID','GAME_ID','GAME_DATE','MATCHUP','WL','MIN','PTS','REB','AST','FGM','FGA','FG3M','FG3A','STL','BLK']
    rows = table(payload, 'PlayerGameLogs', required)
    groups, names = {}, {}
    for row in rows:
        team = NBA_TEAMS.get(row.get('TEAM_ID'))
        if not team:  # Exhibitions can include visiting international teams.
            continue
        if row.get('WL') not in ('W','L'):
            continue
        if not number(row.get('MIN')) or row['MIN']<0:
            raise ValueError('NBA 出賽時間缺漏')
        if row['MIN']==0:
            continue
        pid, gid = str(row.get('PLAYER_ID') or ''), str(row.get('GAME_ID') or '')
        if not pid.isdigit() or not gid.isdigit() or not row.get('PLAYER_NAME'):
            raise ValueError('NBA 球員識別資料缺漏')
        if not all(number(row.get(k)) and row[k]>=0 for k in ['PTS','REB','AST','FGM','FGA','FG3M','FG3A','STL','BLK']):
            raise ValueError('NBA 逐場數值不完整')
        date = str(row['GAME_DATE'])[:10]
        dt.date.fromisoformat(date)
        game = {'id':gid,'date':date,'team':team,'matchup':row['MATCHUP'],'pts':row['PTS'],'reb':row['REB'],
                'ast':row['AST'],'min':row['MIN'],'fgm':row['FGM'],'fga':row['FGA'],'tpm':row['FG3M'],
                'tpa':row['FG3A'],'stl':row['STL'],'blk':row['BLK']}
        if gid in groups.setdefault(pid, {}) and groups[pid][gid]!=game:
            raise ValueError('同一球員的比賽資料互相衝突')
        groups[pid][gid] = game
        names[pid] = row['PLAYER_NAME']
    players, recent = [], []
    for pid, unique in groups.items():
        games = sorted(unique.values(), key=lambda g:(g['date'],g['id']), reverse=True)
        known = identities.get(pid, identities.get(normalize(names[pid]), {}))
        teams = set(g['team'] for g in games)
        p = {**known, 'id':pid,'nbaId':pid,'espnId':known.get('espnId'),'name':names[pid],
             'alias':known.get('alias',''),'team':next(iter(teams)) if len(teams)==1 else 'TOT',
             'position':roster.get(pid,{}).get('position') or known.get('position',''),
             'season':year,'seasonType':typ,'source':'NBA',**summarize(games)}
        players.append(p)
        recent.append({'player':p,'games':games[:5]})
    latest = max((r['games'][0]['date'] for r in recent), default=None)
    stats = {'players':players,'at':at,'lastGameDate':latest,'source':'NBA','complete':True}
    focus = {'players':recent,'at':at,'latestGameDate':latest,'source':'NBA','complete':True,'season':year,'seasonType':typ}
    return stats, focus

def prevent_regression(old, new):
    if not old:
        return
    previous = {p['id']:p for p in old.get('players', [])}
    incoming = {p['id']:p for p in new['players']}
    if previous and not incoming:
        raise ValueError('來源突然回傳空資料，保留上次成功結果')
    if len(incoming)<len(previous)*0.9:
        raise ValueError('來源球員數突然大量減少，保留上次成功結果')
    if any(p.get('gp',0)<old_p.get('gp',0) for pid,old_p in previous.items() if (p:=incoming.get(pid))):
        raise ValueError('出賽紀錄倒退，保留上次成功結果')
    old_day, new_day = old.get('lastGameDate'), new.get('lastGameDate')
    if old_day and (not new_day or new_day<old_day):
        raise ValueError('來源比賽日期倒退，保留上次成功結果')

def parse_roster(payload):
    rows = table(payload, 'PlayerIndex', ['PERSON_ID','TEAM_ID','POSITION'])
    roster = {}
    for r in rows:
        pid = str(r.get('PERSON_ID') or '')
        if pid.isdigit():
            roster[pid] = {'team':NBA_TEAMS.get(r.get('TEAM_ID'), 'UNK'),'position':r.get('POSITION') or ''}
    if len(roster)<300:
        raise ValueError('NBA 目前球員名單不完整')
    return roster

def plain(value):
    return html.unescape(re.sub(r'<[^>]+>', '', str(value or ''))).strip()

def parse_nba_news(raw):
    match = re.search(r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', raw, re.S)
    if not match:
        raise ValueError('NBA 新聞頁面格式變更')
    props = json.loads(match[1]).get('props',{}).get('pageProps',{})
    entries = props.get('latest',{}).get('items',[])
    output = []
    for article in entries:
        url = str(article.get('permalink') or '')
        if not url.startswith('https://www.nba.com/news/') or article.get('status')!='publish':
            continue
        title, published = plain(article.get('title')), article.get('date')
        if not title or not published:
            continue
        dt.datetime.fromisoformat(published.replace('Z','+00:00'))
        tax = article.get('taxonomy', {})
        team_ids = [NBA_TEAMS[int(t)] for t in tax.get('teams',{}) if t.isdigit() and int(t) in NBA_TEAMS]
        image = article.get('featuredImage') or ''
        output.append({'id':'nba'+str(article['id']),'title':title,'summary':plain(article.get('excerpt'))[:180],
                       'url':url,'image':image if image.startswith('https://') else '', 'publishedAt':published,
                       'source':'NBA','lang':'en','teamIds':team_ids,'playerIds':list(tax.get('players',{})),
                       'terms':list(tax.get('teams',{}).values())+list(tax.get('players',{}).values())})
    if not output:
        raise ValueError('NBA 沒有回傳可驗證的新聞')
    return output

def load_base(path):
    raw = Path(path).read_text(encoding='utf-8')
    match = re.search(r'<script id="courtside-data" type="application/json">(.*?)</script>', raw, re.S)
    if not match:
        raise ValueError('找不到既有網站的備援資料')
    return json.loads(match[1])

def seed_feed(base):
    year = base['defaultSeason']
    focus = {k:copy.deepcopy(v) for k,v in base.get('spotlightSets',{}).items() if k.startswith(str(year)+':')}
    # Focus records include season aggregates from the same complete game-log fetch.
    stats = {k:{'players':[copy.deepcopy(r['player']) for r in v['players']], 'at':v['at'],
                'lastGameDate':v.get('latestGameDate'),'complete':True,'source':'NBA'} for k,v in focus.items()}
    return {'schemaVersion':1,'nbaSets':stats,
            'spotlightSets':focus,
            'currentRoster':copy.deepcopy(base.get('currentRoster',{})), 'rosterFetchedAt':base.get('rosterFetchedAt'),
            'news':[n for n in base.get('news',[]) if n.get('source')=='NBA'],
            'newsFetchedAtBySource':{'NBA':base.get('newsFetchedAtBySource',{}).get('NBA',base.get('fetchedAt'))},
            'defaultSeason':base['defaultSeason'],'defaultType':base['defaultType']}

def refresh(base, previous, fetch=request, now=None, public=False):
    if public:
        return refresh_public(base, previous, fetch, now)
    now = now or dt.datetime.now(dt.timezone.utc)
    at = now.isoformat(timespec='seconds')
    taiwan = now.astimezone(dt.timezone(dt.timedelta(hours=8)))
    year = taiwan.year+(taiwan.month>=9)
    feed = copy.deepcopy(previous or seed_feed(base))
    if feed.get('schemaVersion')!=1:
        raise ValueError('既有更新資料版本不符')
    errors = {}
    identities = {}
    all_players = base['nba']+base.get('espn',[])+[p for s in feed['nbaSets'].values() for p in s.get('players',[])]
    for p in all_players:
        if p.get('nbaId'):
            identities[str(p['nbaId'])] = p
        identities[normalize(p['name'])] = p
    for name, value in base.get('idMap',{}).items():
        identities.setdefault(name, value)
    try:
        url = 'https://stats.nba.com/stats/playerindex?'+urllib.parse.urlencode({'LeagueID':'00','Season':f'{year-1}-{str(year)[-2:]}','Historical':0,'TeamID':0})
        feed['currentRoster'] = parse_roster(fetch(url))
        feed['rosterFetchedAt'] = at
        feed['rosterIsComplete'] = True
    except Exception as error:
        errors['roster'] = str(error)
    def period(typ):
        url = 'https://stats.nba.com/stats/playergamelogs?'+urllib.parse.urlencode({'LeagueID':'00','Season':f'{year-1}-{str(year)[-2:]}','SeasonType':TYPES[typ],'LastNGames':0,'PerMode':'Totals'})
        stats, focus = build_period(fetch(url),year,typ,identities,feed['currentRoster'],at)
        stats['url'] = focus['url'] = url
        prevent_regression(feed['nbaSets'].get(f'{year}:{typ}'),stats)
        return typ, stats, focus
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        jobs = {executor.submit(period,typ):typ for typ in TYPES}
        for job in concurrent.futures.as_completed(jobs):
            typ = jobs[job]
            try:
                _, stats, focus = job.result()
                feed['nbaSets'][f'{year}:{typ}'] = stats
                feed['spotlightSets'][f'{year}:{typ}'] = focus
            except Exception as error:
                errors[f'nba:{year}:{typ}'] = str(error)
    available = [typ for typ in TYPES if feed['nbaSets'].get(f'{year}:{typ}',{}).get('players')]
    if available:
        feed['defaultSeason'],feed['defaultType'] = year,max(available)
    elif all(f'{year}:{typ}' in feed['nbaSets'] for typ in TYPES):
        feed['defaultSeason'],feed['defaultType'] = year,1
    try:
        fresh = parse_nba_news(fetch('https://www.nba.com/news',False))
        merged = {n['url']:n for n in feed.get('news',[]) if n.get('source')=='NBA'}
        merged.update({n['url']:n for n in fresh})
        feed['news'] = sorted(merged.values(),key=lambda n:n.get('publishedAt') or '',reverse=True)[:150]
        feed['newsFetchedAtBySource']['NBA'] = at
    except Exception as error:
        errors['news:NBA'] = str(error)
    feed.update({'schemaVersion':1,'attemptedAt':at,'intervalHours':4,'status':'partial' if errors else 'success','errors':errors})
    if not errors:
        feed['lastSuccessfulAt'] = at
    return feed


def next_props(raw):
    match = re.search(r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',raw,re.S)
    if not match:
        raise ValueError('NBA 官方頁面格式變更')
    return json.loads(match[1]).get('props',{}).get('pageProps',{})


def public_roster(payload):
    players = payload.get('players')
    if not isinstance(players,list):
        raise ValueError('NBA 公開球員名單格式不符')
    roster = {str(p['PERSON_ID']):{'team':NBA_TEAMS.get(p.get('TEAM_ID'),'UNK'),'position':p.get('POSITION') or ''}
              for p in players if str(p.get('PERSON_ID') or '').isdigit() and not p.get('HISTORIC')}
    if len(roster)<300:
        raise ValueError('NBA 公開球員名單不完整')
    return roster


def fetch_public_roster(fetch):
    failures = []
    for url in ('https://www.nba.com/players','https://www.nba.com/players?team=all'):
        try:
            return public_roster(next_props(fetch(url,False)))
        except Exception as error:
            failures.append(f'{url}: {error}')
    # Next.js exposes the same page data separately. Discover its current
    # build ID from the accessible official news page instead of hardcoding it.
    try:
        raw = fetch('https://www.nba.com/news',False)
        match = re.search(r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',raw,re.S)
        build = json.loads(match[1]).get('buildId') if match else None
        if not isinstance(build,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',build):
            raise ValueError('NBA 頁面未提供有效的資料版本')
        url = f'https://www.nba.com/_next/data/{build}/players.json'
        payload = fetch(url)
        return public_roster(payload.get('pageProps',payload.get('props',{}).get('pageProps',{})))
    except Exception as error:
        failures.append(f'NBA 頁面資料: {error}')
    raise RuntimeError('NBA 球員名單所有入口均失敗；'+' | '.join(failures))


def fetch_public_schedule(fetch,year):
    failures = []
    for url in ('https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json',
                'https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json',
                'https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json?source=www.nba.com'):
        try:
            return public_schedule(fetch(url),year),url
        except Exception as error:
            failures.append(f'{url}: {error}')
    raise RuntimeError('NBA 賽程所有入口均失敗；'+' | '.join(failures))


def public_schedule(payload,year):
    league = payload.get('leagueSchedule',{})
    if league.get('seasonYear')!=f'{year-1}-{str(year)[-2:]}' or not isinstance(league.get('gameDates'),list):
        raise ValueError('NBA 公開賽程的賽季或格式不符')
    games = {}
    for day in league['gameDates']:
        for game in day.get('games',[]):
            gid = str(game.get('gameId') or '')
            if game.get('gameStatus')!=3 or gid[:3] not in ('001','002','004'):
                continue
            if len(gid)!=10 or not gid.isdigit():
                raise ValueError('NBA 公開賽程的比賽識別資料不完整')
            # Play-in and the NBA Cup championship have different IDs and are
            # intentionally excluded from regular-season / playoff statistics.
            date = str(game.get('gameDateEst') or '')[:10]
            dt.date.fromisoformat(date)
            if not any(game.get(k,{}).get('teamId') in NBA_TEAMS for k in ('homeTeam','awayTeam')):
                continue
            if gid in games and games[gid].get('gameDateEst')!=game.get('gameDateEst'):
                raise ValueError('NBA 公開賽程同一場次的日期互相衝突')
            games[gid] = game
    return games


def minutes(value):
    if value is None or str(value).strip()=='':
        return 0
    text = str(value).strip()
    if text.upper().startswith(('DNP','DND','NWT','INACTIVE')):
        return 0
    iso = re.fullmatch(r'PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?',text)
    if iso:
        return float(iso[1] or 0)*60+float(iso[2] or 0)+float(iso[3] or 0)/60
    clock = re.fullmatch(r'(\d+):(\d{2})(?:\.(\d+))?',text)
    if clock and int(clock[2])<60:
        return int(clock[1])+(int(clock[2])+float('0.'+(clock[3] or '0')))/60
    if number(value) and value>=0:
        return value
    raise ValueError('NBA 官方逐場時間格式不完整')


def public_box_rows(box,scheduled):
    gid = scheduled['gameId']
    if str(box.get('gameId'))!=gid or box.get('gameStatus')!=3:
        raise ValueError('NBA 官方逐場資料不是指定的已完賽場次')
    if box.get('gameEt') and str(box['gameEt'])[:10]!=scheduled['gameDateEst'][:10]:
        raise ValueError('NBA 官方逐場日期與賽程不符')
    sides = {key:box.get(key,{}) for key in ('homeTeam','awayTeam')}
    for key,team in sides.items():
        if team.get('teamId')!=scheduled.get(key,{}).get('teamId') or not isinstance(team.get('players'),list) or not number(team.get('score')):
            raise ValueError('NBA 官方逐場球隊或比分資料不完整')
        if team['score']!=scheduled[key].get('score'):
            raise ValueError('NBA 官方逐場比分與完賽賽程不符，保留舊資料')
    if sides['homeTeam']['score']==sides['awayTeam']['score']:
        raise ValueError('已完賽 NBA 場次比分相同，暫不計入')
    date = scheduled['gameDateEst'][:10]+'T00:00:00'
    rows = []
    for key,team in sides.items():
        if team['teamId'] not in NBA_TEAMS:
            continue
        other = sides['awayTeam' if key=='homeTeam' else 'homeTeam']
        own_code = NBA_TEAMS[team['teamId']]
        other_code = other.get('teamTricode') or NBA_TEAMS.get(other.get('teamId'))
        if not other_code:
            raise ValueError('NBA 官方對手球隊未提供')
        points = 0
        for p in team['players']:
            stat = p.get('statistics') or {}
            played = minutes(stat.get('minutes'))
            if played<=0:
                continue
            mapping = {'PTS':'points','REB':'reboundsTotal','AST':'assists','FGM':'fieldGoalsMade','FGA':'fieldGoalsAttempted',
                       'FG3M':'threePointersMade','FG3A':'threePointersAttempted','STL':'steals','BLK':'blocks'}
            if not all(number(stat.get(k)) and stat[k]>=0 for k in mapping.values()):
                raise ValueError('NBA 官方球員逐場數字缺漏')
            name = p.get('name') or ' '.join(str(p.get(k) or '').strip() for k in ('firstName','familyName')).strip()
            if not name or not str(p.get('personId') or '').isdigit():
                raise ValueError('NBA 官方球員姓名或 ID 缺漏')
            values = {k:stat[v] for k,v in mapping.items()}
            values.update({'PLAYER_ID':p['personId'],'PLAYER_NAME':name,'TEAM_ID':team['teamId'],'GAME_ID':gid,
                           'GAME_DATE':date,'MATCHUP':own_code+(' vs. ' if key=='homeTeam' else ' @ ')+other_code,
                           'WL':'W' if team['score']>other['score'] else 'L','MIN':played})
            rows.append([values[k] for k in BOX_FIELDS])
            points += stat['points']
        if points!=team['score']:
            raise ValueError('NBA 官方球員得分合計與球隊比分不符，保留舊資料')
    if not rows:
        raise ValueError('NBA 官方逐場資料沒有有效出賽球員')
    return rows


def fetch_public_box(game,fetch):
    # The public JSON feed avoids large HTML downloads. Official game pages
    # remain the fallback; neither path requires the blocked statistics host.
    away = game['awayTeam']['teamTricode'].lower()
    home = game['homeTeam']['teamTricode'].lower()
    page = f"https://www.nba.com/game/{away}-vs-{home}-{game['gameId']}/box-score"
    cdn = f"https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game['gameId']}.json"
    failures = []
    for url in (cdn,cdn+'?source=www.nba.com'):
        try:
            return public_box_rows(fetch(url).get('game',{}),game),url
        except Exception as error:
            failures.append(str(error))
    try:
        box = next_props(fetch(page,False)).get('game',{})
        return public_box_rows(box,game),page
    except Exception as page_error:
        raise ValueError(f'NBA 官方比賽頁與公開逐場資料均失敗：{page_error}; '+ '; '.join(failures)) from page_error


def game_signature(game):
    return [game['gameDateEst'][:10],game['homeTeam']['teamId'],game['awayTeam']['teamId'],
            game['homeTeam']['score'],game['awayTeam']['score']]


def refresh_public(base,previous,fetch=request,now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    at = now.isoformat(timespec='seconds')
    taiwan = now.astimezone(dt.timezone(dt.timedelta(hours=8)))
    year = taiwan.year+(taiwan.month>=9)
    feed = copy.deepcopy(previous or seed_feed(base))
    if feed.get('schemaVersion')!=1:
        raise ValueError('既有更新資料版本不符')
    errors = {}
    identities = {}
    for p in base['nba']+base.get('espn',[])+[p for s in feed['nbaSets'].values() for p in s.get('players',[])]:
        if p.get('nbaId'):
            identities[str(p['nbaId'])] = p
        identities[normalize(p['name'])] = p
    for name,value in base.get('idMap',{}).items():
        identities.setdefault(name,value)
    try:
        roster = fetch_public_roster(fetch)
        feed['currentRoster'],feed['rosterFetchedAt'],feed['rosterIsComplete'] = roster,at,True
    except Exception as error:
        errors['roster'] = str(error)
    try:
        games,schedule_url = fetch_public_schedule(fetch,year)
        season = f'{year-1}-{str(year)[-2:]}'
        cache = feed.get('_nbaBoxCache',{})
        if cache.get('season')!=season or cache.get('fields')!=BOX_FIELDS or not isinstance(cache.get('games'),dict):
            cache = {'season':season,'fields':BOX_FIELDS,'games':{}}
        # Cache completed games; recheck the last two NBA calendar days for corrections.
        cutoff = (now-dt.timedelta(days=2)).date().isoformat()
        todo = [g for gid,g in games.items() if gid not in cache['games'] or
                cache['games'][gid].get('signature')!=game_signature(g) or g['gameDateEst'][:10]>=cutoff]
        failed = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            jobs = {executor.submit(fetch_public_box,g,fetch):g['gameId'] for g in todo}
            for job in concurrent.futures.as_completed(jobs):
                gid = jobs[job]
                try:
                    rows,url = job.result()
                    cache['games'][gid] = {'rows':rows,'at':at,'url':url,'signature':game_signature(games[gid])}
                except Exception as error:
                    failed[gid] = str(error)
        # Retain only the current schedule's finished games, avoiding duplicate/rescheduled entries.
        cache['games'] = {gid:entry for gid,entry in cache['games'].items() if gid in games}
        feed['_nbaBoxCache'] = cache
        for typ,prefix in ((1,'001'),(2,'002'),(3,'004')):
            key = f'{year}:{typ}'
            try:
                ids = [gid for gid in games if gid.startswith(prefix)]
                if any(gid in failed or gid not in cache['games'] for gid in ids):
                    missing = [gid for gid in ids if gid in failed or gid not in cache['games']]
                    example = failed.get(missing[0],'沒有取得逐場內容')
                    raise ValueError(f'{len(missing)} 場 NBA 官方逐場資料未完成，保留整個期間：{example}')
                raw = {'parameters':{'SeasonYear':season,'SeasonType':TYPES[typ],'LastNGames':0},
                       'resultSets':[{'name':'PlayerGameLogs','headers':BOX_FIELDS,
                                      'rowSet':[row for gid in ids for row in cache['games'][gid]['rows']]}]}
                stats,focus = build_period(raw,year,typ,identities,feed['currentRoster'],at)
                prevent_regression(feed['nbaSets'].get(key),stats)
                stats.update({'url':schedule_url,'method':'NBA official schedule and box scores','completedGames':len(ids)})
                focus.update({'url':schedule_url,'method':'NBA official schedule and box scores','completedGames':len(ids)})
                feed['nbaSets'][key],feed['spotlightSets'][key] = stats,focus
            except Exception as error:
                errors['nba:'+key] = str(error)
    except Exception as error:
        for typ in TYPES:
            errors[f'nba:{year}:{typ}'] = str(error)
    available = [typ for typ in TYPES if feed['nbaSets'].get(f'{year}:{typ}',{}).get('players')]
    if available:
        feed['defaultSeason'],feed['defaultType'] = year,max(available)
    elif all(f'{year}:{typ}' in feed['nbaSets'] for typ in TYPES):
        feed['defaultSeason'],feed['defaultType'] = year,1
    try:
        fresh = parse_nba_news(fetch('https://www.nba.com/news',False))
        merged = {n['url']:n for n in feed.get('news',[]) if n.get('source')=='NBA'}
        merged.update({n['url']:n for n in fresh})
        feed['news'] = sorted(merged.values(),key=lambda n:n.get('publishedAt') or '',reverse=True)[:150]
        feed['newsFetchedAtBySource']['NBA'] = at
    except Exception as error:
        errors['news:NBA'] = str(error)
    feed.update({'schemaVersion':1,'attemptedAt':at,'intervalHours':4,'status':'partial' if errors else 'success',
                 'errors':errors,'dataMethod':'NBA official public pages'})
    if not errors:
        feed['lastSuccessfulAt'] = at
    return feed

ESPN_TEAMS = {'1':'ATL','2':'BOS','17':'BKN','30':'CHA','4':'CHI','5':'CLE','6':'DAL','7':'DEN','8':'DET','9':'GSW','10':'HOU','11':'IND','12':'LAC','13':'LAL','29':'MEM','14':'MIA','15':'MIL','16':'MIN','3':'NOP','18':'NYK','25':'OKC','19':'ORL','20':'PHI','21':'PHX','22':'POR','23':'SAC','24':'SAS','28':'TOR','26':'UTA','27':'WAS'}
ESPN_ROOT = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/'

def espn_number(value):
    if isinstance(value,bool) or value is None or value=='':
        raise ValueError('ESPN 數值缺漏')
    try:
        result = float(value)
    except (ValueError,TypeError) as error:
        raise ValueError('ESPN 數值格式不符') from error
    if not math.isfinite(result) or result<0:
        raise ValueError('ESPN 數值無效')
    return result

def espn_time(value):
    result = dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if result.tzinfo is None:
        raise ValueError('ESPN 比賽時間未提供時區')
    return result

def espn_event(event,year):
    season = event.get('season',{})
    if season.get('year')!=year or season.get('type') not in TYPES:
        return None
    if event.get('status',{}).get('type',{}).get('completed') is not True:
        return None
    competitions = event.get('competitions',[])
    if len(competitions)!=1:
        raise ValueError('ESPN 比賽結構不完整')
    comp = competitions[0]
    notes = ' '.join(str(n.get('headline') or '') for n in comp.get('notes',[]))
    # Play-in, All-Star and the Cup championship do not count toward the
    # corresponding standard regular-season or playoff player totals.
    if re.search(r'play[ -]?in|all[ -]?star|NBA Cup.{0,30}\b(?:championship|finals?)\b',notes,re.I):
        return None
    teams = {c.get('homeAway'):c for c in comp.get('competitors',[])}
    if set(teams)!={'home','away'}:
        raise ValueError('ESPN 主客隊資料不完整')
    if not any(str(c.get('id')) in ESPN_TEAMS for c in teams.values()):
        return None
    gid = str(event.get('id') or '')
    if not gid.isdigit():
        raise ValueError('ESPN 比賽 ID 缺漏')
    date = espn_time(event['date'])
    result = {'id':gid,'type':season['type'],'date':date.isoformat(),
              'day':date.astimezone(ZoneInfo('America/New_York')).date().isoformat()}
    for side,c in teams.items():
        result[side] = {'id':str(c['id']),'score':espn_number(c.get('score')),
                        'code':ESPN_TEAMS.get(str(c['id'])) or c.get('team',{}).get('abbreviation')}
        if not result[side]['code']:
            raise ValueError('ESPN 對手隊名缺漏')
    if result['home']['score']==result['away']['score']:
        raise ValueError('ESPN 完賽比分相同')
    return result

def fetch_espn_schedule(fetch,year,now):
    # Fetch every elapsed month of the season, including earlier preseason
    # games, rather than building season averages from a moving recent window.
    current = now.astimezone(ZoneInfo('America/New_York')).date()
    months = [(year-1,m) for m in range(9,13)]+[(year,m) for m in range(1,9)]
    months = [(y,m) for y,m in months if (y,m)<=(current.year,current.month)]
    games = {}
    for y,m in months:
        raw = fetch(ESPN_ROOT+f'scoreboard?dates={y}{m:02d}&limit=1000')
        if not isinstance(raw.get('events'),list) or not any(str(l.get('id'))=='46' for l in raw.get('leagues',[])):
            raise ValueError('ESPN NBA 賽程格式不符')
        if len(raw['events'])>=1000:
            raise ValueError('ESPN 賽程可能被截斷，保留舊資料')
        for event in raw['events']:
            game = espn_event(event,year)
            if game:
                if game['id'] in games and games[game['id']]!=game:
                    raise ValueError('ESPN 重複場次資料互相衝突')
                games[game['id']] = game
    return games

def espn_box_rows(raw,game,year):
    header = raw.get('header',{})
    if str(header.get('id'))!=game['id'] or header.get('season',{}).get('year')!=year or header.get('season',{}).get('type')!=game['type']:
        raise ValueError('ESPN 逐場 ID、賽季或類型不符')
    comps = header.get('competitions',[])
    if len(comps)!=1 or comps[0].get('status',{}).get('type',{}).get('completed') is not True:
        raise ValueError('ESPN 逐場尚未完賽')
    comp = comps[0]
    if espn_time(comp['date'])!=espn_time(game['date']):
        raise ValueError('ESPN 逐場日期與賽程不符')
    sides = {c.get('homeAway'):c for c in comp.get('competitors',[])}
    for side in ('home','away'):
        c = sides.get(side,{})
        if str(c.get('id'))!=game[side]['id'] or espn_number(c.get('score'))!=game[side]['score']:
            raise ValueError('ESPN 逐場球隊或比分與賽程不符')
    groups = raw.get('boxscore',{}).get('players')
    if not isinstance(groups,list):
        raise ValueError('ESPN 逐場球員資料缺漏')
    rows,seen,covered = [],set(),set()
    for group in groups:
        tid = str(group.get('team',{}).get('id'))
        side = next((s for s in ('home','away') if game[s]['id']==tid),None)
        if not side:
            raise ValueError('ESPN 逐場出現不符的球隊')
        if tid not in ESPN_TEAMS:
            continue
        if tid in covered:
            raise ValueError('ESPN 逐場球隊重複')
        covered.add(tid)
        other = game['away' if side=='home' else 'home']
        own = game[side]
        points = 0
        for stat in group.get('statistics',[]):
            names = stat.get('names',[])
            required = {'MIN','PTS','FG','3PT','REB','AST','STL','BLK'}
            if not required.issubset(names) or not isinstance(stat.get('athletes'),list):
                raise ValueError('ESPN 逐場欄位變更')
            for record in stat['athletes']:
                if record.get('didNotPlay') is True:
                    continue
                values = record.get('stats',[])
                if len(values)!=len(names):
                    raise ValueError('ESPN 球員逐場數字不完整')
                values = dict(zip(names,values))
                text = str(values['MIN']).strip()
                if text.upper().startswith(('DNP','DND','NWT')):
                    continue
                if text in ('--',''):
                    # ESPN occasionally includes a placeholder line with no
                    # minutes, all-zero stats and didNotPlay=false. It is not
                    # evidence of an appearance. A nonzero line is an error.
                    if all(re.fullmatch(r'[+\-]?0(?:\.0+)?(?:\s*-\s*0)?',str(v).strip()) for k,v in values.items() if k!='MIN'):
                        continue
                    raise ValueError('ESPN 球員有數據卻缺少出賽時間')
                played = espn_number(text) if re.fullmatch(r'\d+(?:\.\d+)?',text) else minutes(text)
                # ESPN rounds some box-score minutes to whole minutes. A
                # non-DNP line at 0 minutes still counts as an appearance.
                played = max(played,0.001)
                athlete = record.get('athlete',{})
                eid,name = str(athlete.get('id') or ''),athlete.get('displayName')
                if not eid.isdigit() or not name or eid in seen:
                    raise ValueError('ESPN 球員識別資料缺漏或重複')
                seen.add(eid)
                numeric = {k:espn_number(values[label]) for k,label in [('pts','PTS'),('reb','REB'),('ast','AST'),('stl','STL'),('blk','BLK')]}
                for label,made,attempted in [('FG','fgm','fga'),('3PT','tpm','tpa')]:
                    match = re.fullmatch(r'(\d+)\s*-\s*(\d+)',str(values[label]))
                    if not match or int(match[1])>int(match[2]):
                        raise ValueError('ESPN 投籃命中與出手數不完整')
                    numeric[made],numeric[attempted] = int(match[1]),int(match[2])
                if numeric['tpm']>numeric['fgm'] or numeric['tpa']>numeric['fga']:
                    raise ValueError('ESPN 三分數字與投籃數字不符')
                rows.append({'espnId':eid,'name':name,'position':athlete.get('position',{}).get('abbreviation',''),
                             'game':{'id':game['id'],'date':game['day'],'team':ESPN_TEAMS[tid],
                                     'matchup':ESPN_TEAMS[tid]+(' vs. ' if side=='home' else ' @ ')+other['code'],
                                     'opponent':other['code'],'home':side=='home','result':'W' if own['score']>other['score'] else 'L',
                                     'min':played,**numeric}})
                points += numeric['pts']
        if points!=own['score']:
            raise ValueError('ESPN 球員得分合計與完賽比分不符，保留舊資料')
    expected = {game[s]['id'] for s in ('home','away') if game[s]['id'] in ESPN_TEAMS}
    if covered!=expected or not rows:
        raise ValueError('ESPN 逐場球員或球隊未完整提供')
    return rows

def espn_identity_index(base,feed):
    by_id,by_name = {},{}
    players = base.get('nba',[])+base.get('espn',[])
    for sets in (base.get('nbaSets',{}),feed.get('nbaSets',{}),feed.get('espnSets',{})):
        players += [p for data in sets.values() for p in data.get('players',[])]
    for p in players:
        if p.get('espnId'):
            by_id[str(p['espnId'])] = p
        key = normalize(p.get('name'))
        if key and p.get('nbaId'):
            by_name.setdefault(key,{})[str(p['nbaId'])] = p
    names = {key:next(iter(v.values())) for key,v in by_name.items() if len(v)==1}
    return by_id,names

def espn_identity(eid,name,index):
    by_id,by_name = index
    known = by_id.get(eid) or by_name.get(normalize(name),{})
    nba = str(known['nbaId']) if known.get('nbaId') else None
    return {**known,'id':nba or 'e'+eid,'nbaId':nba,'espnId':eid,'name':name,
            'alias':known.get('alias',''),'source':'ESPN'}

def build_espn_period(rows,year,typ,index,roster,at):
    players,games,identities = {},{},{}
    for row in rows:
        p = espn_identity(row['espnId'],row['name'],index)
        pid,g = p['id'],row['game']
        p['position'] = row['position'] or p.get('position','')
        identities[pid] = p
        if g['id'] in games.setdefault(pid,{}) and games[pid][g['id']]!=g:
            raise ValueError('ESPN 同一球員逐場資料互相衝突')
        games[pid][g['id']] = g
    recent = []
    for pid,unique in games.items():
        ordered = sorted(unique.values(),key=lambda g:(g['date'],g['id']),reverse=True)
        teams = {g['team'] for g in ordered}
        p = {**identities[pid],**summarize(ordered),'season':year,'seasonType':typ,
             'team':next(iter(teams)) if len(teams)==1 else 'TOT'}
        p['position'] = roster.get(pid,{}).get('position') or p.get('position','')
        players[pid] = p
        recent.append({'player':p,'games':ordered[:5]})
    latest = max((r['games'][0]['date'] for r in recent),default=None)
    common = {'at':at,'source':'ESPN','complete':True,'season':year,'seasonType':typ,
              'method':'ESPN complete season box scores','url':'https://www.espn.com/nba/scoreboard'}
    return ({**common,'players':list(players.values()),'lastGameDate':latest},
            {**common,'players':recent,'latestGameDate':latest})

def refresh_espn(base,previous,fetch=request,now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    at = now.isoformat(timespec='seconds')
    tw = now.astimezone(dt.timezone(dt.timedelta(hours=8)))
    year = tw.year+(tw.month>=9)
    feed = copy.deepcopy(previous or seed_feed(base))
    if feed.get('schemaVersion') not in (1,2):
        raise ValueError('既有更新資料版本不符')
    feed.setdefault('espnSets',{})
    feed.setdefault('espnSpotlightSets',{})
    index = espn_identity_index(base,feed)
    errors = {}
    try:
        roster = {}
        def team_roster(tid):
            raw = fetch(ESPN_ROOT+f'teams/{tid}/roster')
            if str(raw.get('team',{}).get('id'))!=tid or raw.get('season',{}).get('year')!=year or not isinstance(raw.get('athletes'),list):
                raise ValueError('ESPN 球隊名單的賽季或球隊不符')
            result = {}
            for athlete in raw['athletes']:
                eid,name = str(athlete.get('id') or ''),athlete.get('displayName')
                if not eid.isdigit() or not name:
                    raise ValueError('ESPN 名單缺少球員識別資料')
                p = espn_identity(eid,name,index)
                result[p['id']] = {'team':ESPN_TEAMS[tid],'position':athlete.get('position',{}).get('abbreviation','')}
            return result
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            for part in executor.map(team_roster,ESPN_TEAMS):
                for pid,value in part.items():
                    if pid in roster and roster[pid]['team']!=value['team']:
                        raise ValueError('ESPN 同一球員目前球隊互相衝突')
                    roster[pid] = value
        if len(roster)<300:
            raise ValueError('ESPN 30 隊名單不完整')
        feed.update({'currentRoster':roster,'rosterFetchedAt':at,'rosterIsComplete':True,'rosterSource':'ESPN'})
        print(f'ESPN roster: {len(roster)} current players',flush=True)
    except Exception as error:
        errors['roster'] = str(error)
    try:
        games = fetch_espn_schedule(fetch,year,now)
        print(f'ESPN schedule: {len(games)} completed games',flush=True)
        stored = feed.get('_espnBoxCache',{})
        cache = {}
        if stored.get('season')==year and stored.get('version')==1 and stored.get('encoded'):
            decoded = gzip.decompress(base64.b64decode(stored['encoded'],validate=True))
            if len(decoded)>40_000_000:
                raise ValueError('ESPN 快取大小異常')
            cache = json.loads(decoded)
        def signature(g):
            return [g['date'],g['type'],g['home']['id'],g['away']['id'],g['home']['score'],g['away']['score']]
        cutoff = (now-dt.timedelta(days=2)).date().isoformat()
        todo = [g for gid,g in games.items() if gid not in cache or cache[gid].get('signature')!=signature(g) or g['day']>=cutoff]
        def box(g):
            return espn_box_rows(fetch(ESPN_ROOT+'summary?event='+g['id']),g,year)
        failed = {}
        print(f'ESPN box scores: {len(todo)} fetches; {len(games)-len(todo)} cached games',flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            jobs = {executor.submit(box,g):g for g in todo}
            for job in concurrent.futures.as_completed(jobs):
                game = jobs[job]
                try:
                    cache[game['id']] = {'signature':signature(game),'rows':job.result(),'at':at}
                except Exception as error:
                    failed[game['id']] = str(error)
        print(f'ESPN box scores: {len(games)-len(failed)} complete; {len(failed)} failed',flush=True)
        cache = {gid:entry for gid,entry in cache.items() if gid in games}
        encoded = gzip.compress(json.dumps(cache,ensure_ascii=False,separators=(',',':')).encode(),mtime=0)
        feed['_espnBoxCache'] = {'version':1,'season':year,'encoded':base64.b64encode(encoded).decode()}
        for typ in TYPES:
            key = f'{year}:{typ}'
            try:
                ids = [gid for gid,g in games.items() if g['type']==typ]
                missing = [gid for gid in ids if gid in failed or gid not in cache]
                if missing:
                    raise ValueError(f'{len(missing)} 場 ESPN 逐場資料未完成，保留整個期間：'+failed.get(missing[0],'資料缺漏'))
                rows = [row for gid in ids for row in cache[gid]['rows']]
                stats,focus = build_espn_period(rows,year,typ,index,feed['currentRoster'],at)
                prevent_regression(feed['espnSets'].get(key),stats)
                stats['completedGames'] = focus['completedGames'] = len(ids)
                feed['espnSets'][key],feed['espnSpotlightSets'][key] = stats,focus
            except Exception as error:
                errors['espn:'+key] = str(error)
    except Exception as error:
        for typ in TYPES:
            errors[f'espn:{year}:{typ}'] = str(error)
    available = [typ for typ in TYPES if feed['espnSets'].get(f'{year}:{typ}',{}).get('players')]
    if available:
        feed['defaultSeason'],feed['defaultType'] = year,max(available)
    elif all(f'{year}:{typ}' in feed['espnSets'] for typ in TYPES):
        feed['defaultSeason'],feed['defaultType'] = year,1
    try:
        fresh = parse_nba_news(fetch('https://www.nba.com/news',False))
        merged = {n['url']:n for n in feed.get('news',[]) if n.get('source')=='NBA'}
        merged.update({n['url']:n for n in fresh})
        feed['news'] = sorted(merged.values(),key=lambda n:n.get('publishedAt') or '',reverse=True)[:150]
        feed['newsFetchedAtBySource']['NBA'] = at
    except Exception as error:
        errors['news:NBA'] = str(error)
    feed.update({'schemaVersion':2,'attemptedAt':at,'intervalHours':4,'updaterVersion':4,
                 'status':'partial' if errors else 'success','errors':errors,
                 'dataMethod':'ESPN complete season box scores; NBA official news','preferredSource':'espn'})
    if not errors:
        feed['lastSuccessfulAt'] = at
    return feed

def write_atomic(path, feed):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(feed,ensure_ascii=False,separators=(',',':'),allow_nan=False),encoding='utf-8')
    os.replace(temporary,path)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source',default='index.html')
    parser.add_argument('--output',default='data/latest.json')
    parser.add_argument('--source-method',choices=['auto','espn','public','stats'],default='auto')
    args = parser.parse_args()
    base = load_base(args.source)
    path = Path(args.output)
    previous = json.loads(path.read_text()) if path.exists() else None
    use_espn = args.source_method in ('auto','espn')
    print('COURTSIDE updater v4 source: '+('ESPN complete season box scores + NBA news' if use_espn else args.source_method),flush=True)
    feed = refresh_espn(base,previous) if use_espn else refresh(base,previous,public=args.source_method=='public')
    feed['updaterVersion'] = 4
    write_atomic(path,feed)
    print(json.dumps({'status':feed['status'],'attemptedAt':feed['attemptedAt'],'defaultSeason':feed['defaultSeason'],'defaultType':feed['defaultType'],'errors':feed['errors']},ensure_ascii=False))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary,'a',encoding='utf-8') as file:
            file.write(f"COURTSIDE data update: **{feed['status']}**\n\nAttempted: {feed['attemptedAt']}\n\n")
            file.write('Updater: **v4**\n\n')
            file.write('Source: '+feed.get('dataMethod','NBA statistics API')+'\n\n')
            for key,error in feed['errors'].items():
                file.write(f'- {key}: {error}\n')
    return 1 if feed['errors'] else 0

if __name__=='__main__':
    raise SystemExit(main())
