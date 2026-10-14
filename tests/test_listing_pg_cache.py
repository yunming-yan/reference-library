"""PostgreSQL integration checks for exact generation-scoped listing optimization.

Supply REFERENCE_LIBRARY_TEST_PG_CONFIG for a new empty loopback database whose
name begins rl_perf_review_. No existing catalog, original files, or real library
is reset. The test retains its synthetic catalog for inspection.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib,json,os,sys,threading,unittest,uuid
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
from pg_catalog import Catalog,CatalogConflict

class ListingGenerationIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        location=os.environ.get("REFERENCE_LIBRARY_TEST_PG_CONFIG")
        if not location:raise unittest.SkipTest("A dedicated new PostgreSQL test config is required")
        cls.config=json.loads(Path(location).read_text(encoding="utf-8-sig"))
        if cls.config.get("host")!="127.0.0.1" or not cls.config.get("dbname","").startswith("rl_perf_review_"):
            raise RuntimeError("Dedicated loopback synthetic-database guard refused")
        import psycopg
        with psycopg.connect(**cls.config) as connection:
            if connection.execute("SELECT to_regclass('public.rl_reports')").fetchone()[0] is not None:
                raise RuntimeError("Existing catalog is retained; use a new empty test database")
        cls.cat=Catalog(cls.config)
        cls.cat.bind_library(str(uuid.uuid4()))
        cls.writer=Catalog(cls.config,initialize=False)
        cls.package=hashlib.sha256(b"listing-generation-integration-v1").hexdigest()
        cls.ids=[cls.package[:24]+"-"+str(i) for i in range(320)]
        records=[];documents={}
        for i,rid in enumerate(cls.ids):
            records.append(dict(id=rid,package=cls.package,md="md/原件/"+str(i)+".md",html="htm/"+str(i)+".html",
                title="Synthetic "+str(i),category="Alpha" if i%3==0 else "alpha" if i%3==1 else "Beta",
                date="2026-10-14",issue=str(i).zfill(8),importedAt="2026-10-14T00:00:00+00:00"))
            documents[rid]="市场 中 common Straße İstanbul Ωμέγα 🔬 literal_%\\path X\x00Y\n"+"body "*10
        cls.cat.import_package(dict(package=cls.package,sha256=cls.package,objectId=cls.package,reports=records,files=[]),documents)
        cls.original_generation=cls.cat.health()["generation"]

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls,"writer"):cls.writer.close()
        if hasattr(cls,"cat"):cls.cat.close()

    def test_01_exact_broad_predicates_and_bounded_cursors(self):
        expected=list(reversed(self.ids))
        for query in ("市场","中","common","strasse","i̇stanbul","Ωμέγα","🔬","literal_%\\path","X\x00Y"):
            first=self.cat.list_reports(query,limit=200)
            self.assertEqual(first["filteredCount"],320)
            self.assertEqual(first["pageRoute"],"ordered-exact-match")
            second=self.cat.list_reports(query,limit=200,cursor=first["nextCursor"])
            ids=[r["id"] for r in first["reports"]+second["reports"]]
            self.assertEqual(ids,expected)
            self.assertFalse(second["hasMore"])
        absent=self.cat.list_reports("absent-unicode-🔎")
        self.assertEqual(absent["filteredCount"],0)
        self.assertEqual(self.cat.list_reports("absent-unicode-🔎")["filteredCount"],0)
        self.assertEqual(self.cat.list_reports(category="ALPHA")["filteredCount"],214)

    def test_02_concurrent_returns_cannot_mutate_shared_statistics(self):
        def read(_):
            result=self.cat.list_reports("市场",limit=50)
            self.assertEqual(result["total"],320)
            self.assertEqual(result["filteredCount"],320)
            result["categories"].clear()
            return result["generation"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(set(pool.map(read,range(24))),{self.original_generation})
        self.assertEqual(self.cat.list_reports()["categories"],["Alpha","alpha","Beta"])

    def test_03_external_writer_changes_invalidate_counts_categories_and_cursor(self):
        old=self.cat.list_reports("市场",limit=50)
        self.writer.update_metadata(self.ids[0],{"title":"Changed","category":"Moved"})
        current=self.cat.list_reports()
        self.assertEqual(current["filteredCount"],320)
        self.assertIn("Moved",current["categories"])
        self.assertEqual(self.cat.list_reports(category="ALPHA")["filteredCount"],213)
        self.writer.set_trash(self.ids[0])
        deleted=self.cat.list_reports(trash=True)
        self.assertEqual((deleted["total"],deleted["trashTotal"],deleted["filteredCount"]),(319,1,1))
        self.assertNotIn("Moved",self.cat.list_reports()["categories"])
        self.assertEqual(self.cat.list_reports("市场")["filteredCount"],319)
        with self.assertRaises(CatalogConflict):
            self.cat.list_reports("市场",limit=50,cursor=old["nextCursor"])
        self.writer.set_trash(self.ids[0],restore=True)
        self.assertEqual(self.cat.list_reports("市场")["filteredCount"],320)
        self.assertIn("Moved",self.cat.list_reports()["categories"])

    def test_04_older_repeatable_read_cannot_publish_over_new_generation(self):
        old_generation=self.cat.health()["generation"]
        entered=threading.Event();release=threading.Event();actual_pool=self.cat.pool
        # Clear only this test's derived in-memory summary, never authoritative data.
        with self.cat._list_cache_lock:self.cat._list_summary=None
        class Connection:
            def __init__(self,c):self.c=c
            def __getattr__(self,name):return getattr(self.c,name)
            def execute(self,sql,*args,**kwargs):
                if threading.current_thread().name=="old-snapshot" and "GROUP BY category,category_fold" in str(sql):
                    entered.set()
                    if not release.wait(10):raise RuntimeError("Synthetic snapshot barrier timed out")
                return self.c.execute(sql,*args,**kwargs)
        class Pool:
            @contextmanager
            def connection(self,*args,**kwargs):
                with actual_pool.connection(*args,**kwargs) as connection:yield Connection(connection)
            def __getattr__(self,name):return getattr(actual_pool,name)
        self.cat.pool=Pool();old_result=[];errors=[]
        def old_reader():
            try:old_result.append(self.cat.list_reports())
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=old_reader,name="old-snapshot")
        try:
            thread.start();self.assertTrue(entered.wait(10))
            self.writer.set_trash(self.ids[1])
            new=self.cat.list_reports()
            self.assertEqual((new["total"],new["trashTotal"]),(319,1))
            self.assertGreater(new["generation"],old_generation)
            release.set();thread.join(10);self.assertFalse(thread.is_alive());self.assertFalse(errors)
            self.assertEqual((old_result[0]["generation"],old_result[0]["total"]),(old_generation,320))
            current=self.cat.list_reports()
            self.assertEqual((current["generation"],current["total"]), (new["generation"],319))
            self.assertEqual(self.cat._list_generation,new["generation"])
        finally:
            release.set();thread.join(10);self.cat.pool=actual_pool
        self.writer.set_trash(self.ids[1],restore=True)

    def test_05_restart_and_cache_bound(self):
        cold=Catalog(self.config,initialize=False)
        try:
            result=cold.list_reports()
            self.assertEqual((result["total"],result["trashTotal"]),(320,0))
            self.assertIn("Moved",result["categories"])
            for i in range(140):
                self.assertEqual(cold.list_reports("absent-exact-"+str(i))["filteredCount"],0)
            self.assertEqual(len(cold._list_counts),128)
            self.assertEqual(cold.list_reports("absent-exact-0")["filteredCount"],0)
        finally:cold.close()

if __name__=="__main__":unittest.main()
