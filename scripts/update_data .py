"""Refresh public NBA data for COURTSIDE. Standard library only; no API keys."""
from __future__ import annotations

import argparse
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
import time
import unicodedata
import urllib.parse
import urllib.request
import urllib.error

NBA_TEAMS = {1610612737:'ATL',1610612738:'BOS',1610612739:'CLE',1610612740:'NOP',1610612741:'CHI',1610612742:'DAL',1610612743:'DEN',1610612744:'GSW',1610612745:'HOU',1610612746:'LAC',1610612747:'LAL',1610612748:'MIA',1610612749:'MIL',1610612750:'MIN',1610612751:'BKN',1610612752:'NYK',1610612753:'ORL',1610612754:'IND',1610612755:'PHI',1610612756:'PHX',1610612757:'POR',1610612758:'SAC',1610612759:'SAS',1610612760:'OKC',1610612761:'TOR',1610612762:'UTA',1610612763:'MEM',1610612764:'WAS',1610612765:'DET',1610612766:'CHA'}
TYPES = {1:'Pre Season',2:'Regular Season',3:'Playoffs'}
BOX_FIELDS = ['PLAYER_ID','PLAYER_NAME','TEAM_ID','GAME_ID','GAME_DATE','MATCHUP','WL','MIN','PTS','REB','AST','FGM','FGA','FG3M','FG3A','STL','BLK']

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
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36',
                'Referer':'https://www.nba.com/', 'Origin':'https://www.nba.com',
                'Accept':'application/json,text/html;q=0.9,*/*;q=0.8',
            })
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read(25_000_001)
                if len(raw)>25_000_000:
                    raise ValueError('來源回應超過預期大小')
                if raw[:2]==b'\x1f\x8b':
                    raw = gzip.decompress(raw)
                text = raw.decode('utf-8')
                return json.loads(text) if as_json else text
        except Exception as error:
            last_error = error
            if isinstance(error,urllib.error.HTTPError) and error.code in (403,404):
                break
            if attempt<attempts-1:
                time.sleep(2*(attempt+1))
    raise RuntimeError(f'來源讀取失敗：{last_error}') from last_error

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
    url = f"https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game['gameId']}.json?source=www.nba.com"
    try:
        return public_box_rows(fetch(url).get('game',{}),game),url
    except Exception as cdn_error:
        try:
            box = next_props(fetch(page,False)).get('game',{})
            return public_box_rows(box,game),page
        except Exception as page_error:
            raise ValueError(f'NBA 官方比賽頁與公開逐場資料均失敗：{page_error}; {cdn_error}') from cdn_error


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
        players = next_props(fetch('https://www.nba.com/players',False)).get('players')
        if not isinstance(players,list):
            raise ValueError('NBA 公開球員名單格式不符')
        roster = {str(p['PERSON_ID']):{'team':NBA_TEAMS.get(p.get('TEAM_ID'),'UNK'),'position':p.get('POSITION') or ''}
                  for p in players if str(p.get('PERSON_ID') or '').isdigit() and not p.get('HISTORIC')}
        if len(roster)<300:
            raise ValueError('NBA 公開球員名單不完整')
        feed['currentRoster'],feed['rosterFetchedAt'],feed['rosterIsComplete'] = roster,at,True
    except Exception as error:
        errors['roster'] = str(error)
    try:
        schedule_url = 'https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json?source=www.nba.com'
        games = public_schedule(fetch(schedule_url),year)
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
    parser.add_argument('--source-method',choices=['auto','public','stats'],default='auto')
    args = parser.parse_args()
    base = load_base(args.source)
    path = Path(args.output)
    previous = json.loads(path.read_text()) if path.exists() else None
    use_public = args.source_method!='stats'
    print('COURTSIDE updater v2 source: '+('NBA official public pages' if use_public else 'NBA statistics API'),flush=True)
    feed = refresh(base,previous,public=use_public)
    write_atomic(path,feed)
    print(json.dumps({'status':feed['status'],'attemptedAt':feed['attemptedAt'],'defaultSeason':feed['defaultSeason'],'defaultType':feed['defaultType'],'errors':feed['errors']},ensure_ascii=False))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary,'a',encoding='utf-8') as file:
            file.write(f"COURTSIDE data update: **{feed['status']}**\n\nAttempted: {feed['attemptedAt']}\n\n")
            file.write('Source: '+feed.get('dataMethod','NBA statistics API')+'\n\n')
            for key,error in feed['errors'].items():
                file.write(f'- {key}: {error}\n')
    return 1 if feed['errors'] else 0

if __name__=='__main__':
    raise SystemExit(main())
