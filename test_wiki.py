import asyncio
import concurrent.futures
from pathlib import Path
import sys
import tempfile
import unittest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from corpus import ROOT
from fixtures import CASES, seed
from wiki import connect, initialize, page, replace, search
from site_view import article, export


class WikiTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)/"wiki.sqlite"
        initialize(self.path)
        seed(self.path)
        self.args = dict(doc="atlas-guide",sec=1,base_revision=1,body="Для EU — 2 повтора.",
                         source="atlas-decision@1#s1",request_id="abc",role="editor")

    def update(self, **changes):
        return replace(self.path, **(self.args | changes))

    def test_versions_search_and_retries(self):
        original = self.update()
        self.assertEqual(self.update(),original)
        with connect(self.path) as db:
            new, old = page(db,"atlas-guide"), page(db,"atlas-guide",1)
            self.assertEqual((new["revision"],old["revision"]),(2,1))
            self.assertNotEqual(new["sections"][0],old["sections"][0])
            self.assertEqual(new["sections"][1],old["sections"][1])
            hits = search(db,"повтора")
            self.assertEqual(hits[0]["url"],original["url"])
            self.assertEqual(page(db,"atlas-guide",2,1)["url"],original["url"])
            self.assertNotIn("atlas-guide",[h["doc"] for h in search(db,"5")])

    def test_rejections_are_atomic(self):
        for overrides in [dict(role="reader"),dict(source="bogus@1#s1"),
                          dict(source="atlas-decision@44#s1"),dict(sec=99),dict(base_revision=8)]:
            with self.assertRaises(ValueError):
                self.update(**overrides)
        self.update()
        with self.assertRaisesRegex(ValueError,"reused"):
            self.update(body="different")
        with connect(self.path) as db:
            self.assertEqual(page(db,"atlas-guide")["revision"],2)

    def test_concurrent_writers(self):
        def worker(key):
            try:
                return self.update(request_id=key)
            except ValueError as err:
                return str(err)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(worker,["one","two"]))
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        self.assertIn("Version conflict: current revision 2",results)

    def test_all_five_changes(self):
        for key,title,setting,new,old,unit,fact,_ in CASES:
            result=self.update(doc=key+"-guide",body=f"{setting}: {new} ({unit}) для EU.",
                               source=key+"-decision@1#s1",request_id=key)
            self.assertEqual(result["revision"],2)
            with connect(self.path) as db:
                self.assertEqual(page(db,key+"-guide",2,1)["sections"][0]["source"],result["source"])

    def test_html_links_and_escaping(self):
        self.update(body="<test> & EU")
        with connect(self.path) as db:
            html = article(page(db,"atlas-guide"))
        self.assertIn("&lt;test&gt; &amp; EU",html)
        self.assertIn('../atlas-decision/v1.html#s1',html)
        dest = self.path.parent/"public"
        export(self.path,dest)
        self.assertTrue((dest/"index.html").is_file())
        self.assertTrue((dest/"p/atlas-decision/v1.html").is_file())
        self.assertTrue((dest/"p/atlas-guide/v2.html").is_file())

    def test_stdio_roles(self):
        async def run():
            for role in ("reader","editor"):
                params = StdioServerParameters(command=sys.executable,args=[str(ROOT/"wiki_mcp.py"),
                                                "--db",str(self.path),"--role",role])
                async with stdio_client(params) as streams, ClientSession(*streams) as session:
                    await session.initialize()
                    names = {t.name for t in (await session.list_tools()).tools}
                    self.assertEqual("update_section" in names,role=="editor")
                    result = await session.call_tool("search_sections",{"query":"Atlas"})
                    self.assertFalse(result.isError)
                    self.assertIn("atlas",result.content[0].text.lower())
                    write = await session.call_tool("update_section", {k:v for k,v in self.args.items() if k!="role"})
                    self.assertEqual(write.isError,role=="reader")
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
