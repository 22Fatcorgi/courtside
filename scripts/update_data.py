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

NBA_TEAMS = {1610612737:'ATL',1610612738:'BOS',1610612739:'CLE',1610612740:'NOP',1610612741:'CHI',1610612742:'DAL',1610612743:'DEN',1610612744:'GSW',1610612745:'HOU',1610612746:'LAC',1610612747:'LAL',1610612748:'MIA',1610612749:'MIL',1610612750:'MIN',1610612751:'BKN',1610612752:'NYK',1610612753:'ORL',1610612754:'IND',1610612755:'PHI',1610612756:'PHX',1610612757:'POR',1610612758:'SAC',1610612759:'SAS',1610612760:'OKC',1610612761:'TOR',1610612762:'UTA',1610612763:'MEM',1610612764:'WAS',1610612765:'DET',1610612766:'CHA'}
TYPES = {1:'Pre Season',2:'Regular Season',3:'Playoffs'}

def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

def normalize(value):
    return re.sub(r'[^a-z0-9\u3400-\u9fff]', '', unicodedata.normalize('NFKD', value or '').lower())

def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def request(url, as_json=True):
    last_error = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent':'Mozilla/5.0 (compatible; COURTSIDE public statistics updater)',
                'Referer':'https://www.nba.com/', 'Accept':'application/json,text/html;q=0.9,*/*;q=0.8',
            })
            with urllib.request.urlopen(req, timeout=25) as response:
                raw = response.read(25_000_001)
                if len(raw)>25_000_000:
                    raise ValueError('來源回應超過預期大小')
                if raw[:2]==b'\x1f\x8b':
                    raw = gzip.decompress(raw)
                text = raw.decode('utf-8')
                return json.loads(text) if as_json else text
        except Exception as error:
            last_error = error
            if attempt<2:
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

def refresh(base, previous, fetch=request, now=None):
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
    args = parser.parse_args()
    base = load_base(args.source)
    path = Path(args.output)
    previous = json.loads(path.read_text()) if path.exists() else None
    feed = refresh(base,previous)
    write_atomic(path,feed)
    print(json.dumps({'status':feed['status'],'attemptedAt':feed['attemptedAt'],'defaultSeason':feed['defaultSeason'],'defaultType':feed['defaultType'],'errors':feed['errors']},ensure_ascii=False))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary,'a',encoding='utf-8') as file:
            file.write(f"COURTSIDE data update: **{feed['status']}**\n\nAttempted: {feed['attemptedAt']}\n\n")
            for key,error in feed['errors'].items():
                file.write(f'- {key}: {error}\n')
    return 1 if feed['errors'] else 0

if __name__=='__main__':
    raise SystemExit(main())
