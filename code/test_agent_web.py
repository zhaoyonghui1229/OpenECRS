# -*- coding: utf-8 -*-
import unittest
from unittest import mock

import agent_web


SAMPLE = '''
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=x&amp;u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9h">Example A</a></h2><p>First <b>snippet</b>.</p></li>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=y&amp;u=a1aHR0cHM6Ly9leGFtcGxlLm9yZy9i">Example B</a></h2><p>Second snippet.</p></li>
<li class="b_algo"><h2><a href="https://example.com/a">Duplicate host</a></h2><p>dup</p></li>
'''


class WebSearchTests(unittest.TestCase):
    def test_parse_results_decodes_urls(self):
        results = agent_web.parse_results(SAMPLE, 5)
        self.assertEqual([r["title"] for r in results], ["Example A", "Example B", "Duplicate host"])
        self.assertEqual(results[0]["url"], "https://example.com/a")
        self.assertEqual(results[0]["snippet"], "First snippet.")
        self.assertEqual(results[0]["source"], "web")

    def test_empty_query(self):
        with self.assertRaises(agent_web.WebSearchError):
            agent_web.search("   ")

    def test_search_success(self):
        import agent_web as aw
        old = aw.SEARXNG_URL
        aw.SEARXNG_URL = ""
        page = '<html>' + SAMPLE + '</html>'
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = page.encode("utf-8")
        try:
            with mock.patch("urllib.request.urlopen", return_value=response):
                data = agent_web.search("ai agents", 3, "en")
        finally:
            aw.SEARXNG_URL = old
        self.assertEqual(data["provider"], "bing")
        self.assertEqual(len(data["results"]), 3)
        self.assertIn("not part of the audited evidence", data["note"])

    def test_search_failure(self):
        import agent_web as aw
        old = aw.SEARXNG_URL
        aw.SEARXNG_URL = ""
        try:
            with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
                with self.assertRaises(agent_web.WebSearchError):
                    agent_web.search("ai agents")
        finally:
            aw.SEARXNG_URL = old


if __name__ == "__main__":
    unittest.main()
