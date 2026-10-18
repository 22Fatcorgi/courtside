import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse

SPEC = importlib.util.spec_from_file_location('update_data',Path(__file__).parents[1]/'scripts/update_data.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

FIELDS = ['PLAYER_ID','PLAYER_NAME','TEAM_ID','GAME_ID','GAME_DATE','MATCHUP','WL','MIN','PTS','REB','AST','FGM','FGA','FG3M','FG3A','STL','BLK']

def payload(typ=1, rows=None):
    rows = rows if rows is not None else [game(i) for i in range(1,7)]
    return {'parameters':{'SeasonYear':'2026-27','SeasonType':m.TYPES[typ],'LastNGames':0},
            'resultSets':[{'name':'PlayerGameLogs','headers':FIELDS,'rowSet':[[r[k] for k in FIELDS] for r in rows]}]}

def game(i, pid=1, team=1610612747, pts=None):
    return {'PLAYER_ID':pid,'PLAYER_NAME':'Player '+str(pid),'TEAM_ID':team,'GAME_ID':f'00126000{i:02}',
            'GAME_DATE':f'2026-10-{i:02}T00:00:00','MATCHUP':'LAL vs. BOS','WL':'W','MIN':20,'PTS':i if pts is None else pts,
            'REB':5,'AST':3,'FGM':2,'FGA':5,'FG3M':1,'FG3A':3,'STL':1,'BLK':0}

def news_html():
    item={'id':1,'title':'NBA &amp; news','permalink':'https://www.nba.com/news/sample','status':'publish',
          'date':'2026-10-18T00:00:00Z','excerpt':'<p>Short summary</p>','featuredImage':'https://cdn.nba.com/test.jpg',
          'taxonomy':{'teams':{'1610612747':'Los Angeles Lakers'},'players':{'2544':'LeBron James'}}}
    return '<script id="__NEXT_DATA__" type="application/json">'+json.dumps({'props':{'pageProps':{'latest':{'items':[item]}}}})+'</script>'

class UpdaterTests(unittest.TestCase):
    def test_season_and_last_five_are_independent(self):
        stats, focus=m.build_period(payload(),2027,1,{}, {},'2026-10-18T00:00:00Z')
        p=stats['players'][0]
        self.assertEqual(p['gp'],6)
        self.assertEqual(p['pts'],3.5)
        self.assertEqual([g['pts'] for g in focus['players'][0]['games']],[6,5,4,3,2])
        self.assertEqual(p['fg'],40)
        self.assertAlmostEqual(p['tp'],100/3)

    def test_tot_only_for_confirmed_multi_team(self):
        rows=[game(1),game(2,team=1610612744)]
        stats,_=m.build_period(payload(rows=rows),2027,1,{}, {},'2026-10-18T00:00:00Z')
        self.assertEqual(stats['players'][0]['team'],'TOT')

    def test_international_and_dnp_excluded(self):
        inactive=game(7,pid=3);inactive['MIN']=0
        rows=[game(1),game(2,pid=2,team=123),inactive]
        stats,_=m.build_period(payload(rows=rows),2027,1,{}, {},'2026-10-18T00:00:00Z')
        self.assertEqual([p['id'] for p in stats['players']],['1'])

    def test_duplicates_do_not_inflate_gp(self):
        stats,_=m.build_period(payload(rows=[game(1),game(1)]),2027,1,{}, {},'2026-10-18T00:00:00Z')
        self.assertEqual(stats['players'][0]['gp'],1)

    def test_conflicting_duplicates_fail(self):
        with self.assertRaises(ValueError):
            m.build_period(payload(rows=[game(1),game(1,pts=9)]),2027,1,{}, {},'2026-10-18T00:00:00Z')

    def test_wrong_period_fails(self):
        with self.assertRaises(ValueError):
            m.build_period(payload(1),2027,2,{}, {},'2026-10-18T00:00:00Z')

    def test_missing_numeric_value_fails(self):
        r=game(1);r['PTS']=None
        with self.assertRaises(ValueError):
            m.build_period(payload(rows=[r]),2027,1,{}, {},'2026-10-18T00:00:00Z')

    def test_regression_preserves_previous(self):
        old={'players':[{'id':'1','gp':6}],'lastGameDate':'2026-10-16'}
        for new in [{'players':[]},{'players':[{'id':'1','gp':5}],'lastGameDate':'2026-10-16'},
                    {'players':[{'id':'1','gp':6}],'lastGameDate':'2026-10-15'}]:
            with self.subTest(new=new),self.assertRaises(ValueError):m.prevent_regression(old,new)

    def test_news_metadata_and_identity(self):
        article=m.parse_nba_news(news_html())[0]
        self.assertEqual(article['title'],'NBA & news')
        self.assertEqual(article['summary'],'Short summary')
        self.assertEqual(article['teamIds'],['LAL'])
        self.assertEqual(article['playerIds'],['2544'])

    def test_news_failure_is_not_empty_success(self):
        with self.assertRaises(ValueError):m.parse_nba_news('<html>error</html>')

    def test_atomic_file_contains_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'latest.json';m.write_atomic(p,{'test':'繁體中文'})
            self.assertEqual(json.loads(p.read_text()),{'test':'繁體中文'})
            self.assertFalse(p.with_suffix('.tmp').exists())

    def fixture(self):
        stats,focus=m.build_period(payload(),2027,1,{}, {},'2026-10-17T00:00:00Z')
        old={'schemaVersion':1,'nbaSets':{'2027:1':stats},'spotlightSets':{'2027:1':focus},
             'currentRoster':{},'rosterFetchedAt':'2026-10-17T00:00:00Z',
             'news':m.parse_nba_news(news_html()),'newsFetchedAtBySource':{'NBA':'2026-10-17T00:00:00Z'},
             'defaultSeason':2027,'defaultType':1,'lastSuccessfulAt':'2026-10-17T00:00:00Z'}
        base={'nba':[],'espn':[],'idMap':{}}
        return base,old

    def test_all_source_failure_preserves_data_and_timestamps(self):
        base,old=self.fixture();before=copy.deepcopy(old)
        def fail(*args):raise RuntimeError('offline')
        new=m.refresh(base,old,fail,dt.datetime(2026,10,18,tzinfo=dt.timezone.utc))
        for key in ['nbaSets','spotlightSets','news','newsFetchedAtBySource','rosterFetchedAt','lastSuccessfulAt']:
            self.assertEqual(new[key],before[key])
        self.assertEqual(new['status'],'partial')
        self.assertEqual(len(new['errors']),5)
        self.assertEqual(old,before)

    def test_partial_failure_keeps_only_failed_components_old(self):
        base,old=self.fixture()
        def fetch(url,as_json=True):
            if 'nba.com/news' in url:return news_html()
            if 'playerindex' in url:raise RuntimeError('roster offline')
            q=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            typ=next(t for t,name in m.TYPES.items() if name==q['SeasonType'][0])
            if typ==1:raise RuntimeError('preseason offline')
            return payload(typ,rows=[game(i,pid=2) for i in range(1,7)] if typ==2 else [])
        new=m.refresh(base,old,fetch,dt.datetime(2026,10,18,tzinfo=dt.timezone.utc))
        self.assertEqual(new['nbaSets']['2027:1']['at'],old['nbaSets']['2027:1']['at'])
        self.assertGreater(new['nbaSets']['2027:2']['at'],old['nbaSets']['2027:1']['at'])
        self.assertEqual(new['defaultType'],2)
        self.assertEqual(new['newsFetchedAtBySource']['NBA'],'2026-10-18T00:00:00+00:00')

if __name__=='__main__':unittest.main()
