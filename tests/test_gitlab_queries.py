"""Cross-source query contracts, independent of the remote GitLab service."""
import unittest
from knowledge import db, graph, models, search
from knowledge.mcp_server import McpServer


class GitLabQueryTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(':memory:')
        self.addCleanup(self.conn.close)
        for uid in ('confluence:1', 'confluence:2', 'jira:API-2323', 'gitlab:7:abc'):
            kind, sid = uid.split(':', 1)
            doc = models.FetchedDocument(uid, kind, sid, 'Architecture', '', 'Architecture')
            db.save_document(self.conn, doc, [models.Chunk('', 'Architecture')])
        self.link('gitlab:7:abc', 'jira:API-2323')
        self.link('jira:API-2323', 'confluence:2')

    def link(self, source, target, kind='mentions_jira'):
        db.add_edge(self.conn, source, models.LinkTarget(target, kind))

    def test_search_sources_and_scores(self):
        results = search.search(self.conn, 'Architecture')['results']
        self.assertEqual(len(results), 4)
        self.assertEqual(results[0]['uid'], 'jira:API-2323')
        self.assertEqual(results[0]['importance_score'], 2)
        filtered = search.search(self.conn, 'Architecture', include_gitlab=False)['results']
        self.assertEqual(len(filtered), 3)
        self.assertTrue(all(r['type'] != 'gitlab' for r in filtered))
        jira = next(r for r in filtered if r['type'] == 'jira')
        self.assertEqual(jira['importance_score'], 1)
        self.assertEqual(len(search.search(self.conn, 'Architecture', type='gitlab')['results']), 1)
        self.assertEqual(search.search(self.conn, 'Architecture', type='gitlab', include_gitlab=False)['results'], [])

    def test_importance_distinct_links_and_exact_issue(self):
        before = search.search(self.conn, 'Architecture')['results'][0]['importance_score']
        self.link('jira:API-2323', 'confluence:2', 'another_evidence_kind')
        self.link('jira:API-2323', 'jira:API-2323')
        after = search.search(self.conn, 'Architecture')['results'][0]['importance_score']
        self.assertEqual(before, after)
        self.assertEqual(search.search(self.conn, 'API-2323 Architecture')['results'][0]['uid'], 'jira:API-2323')

    def test_graph_only_with_gitlab(self):
        doc = models.FetchedDocument('jira:UNLINKED', 'jira', 'UNLINKED', 'Unlinked Issue', '', 'Unlinked')
        db.save_document(self.conn, doc, [models.Chunk('', 'Unlinked')])
        res_all = graph.get_subgraph(self.conn, None, types='jira', only_with_gitlab=False)
        uids_all = {n['uid'] for n in res_all['nodes']}
        self.assertIn('jira:API-2323', uids_all)
        self.assertIn('jira:UNLINKED', uids_all)

        res_filtered = graph.get_subgraph(self.conn, None, types='jira', only_with_gitlab=True)
        uids_filtered = {n['uid'] for n in res_filtered['nodes']}
        self.assertIn('jira:API-2323', uids_filtered)
        self.assertNotIn('jira:UNLINKED', uids_filtered)

        res_jg = graph.get_subgraph(self.conn, None, types='jira,gitlab', only_with_gitlab=True)
        uids_jg = {n['uid'] for n in res_jg['nodes']}
        self.assertEqual(uids_jg, {'jira:API-2323', 'gitlab:7:abc'})
        self.assertEqual(len(res_jg['edges']), 1)

    def test_graph_without_confluence_and_excluded_center(self):
        result = graph.get_subgraph(self.conn, None, types='jira,gitlab')
        self.assertEqual({n['type'] for n in result['nodes']}, {'jira', 'gitlab'})
        self.assertEqual(len(result['edges']), 1)
        self.assertIsNone(graph.get_subgraph(self.conn, 'gitlab:7:abc', include_gitlab=False))
        result = graph.get_subgraph(self.conn, None, include_gitlab=False)
        self.assertTrue(all(n['type'] != 'gitlab' for n in result['nodes']))

    def test_mcp_filter_all_document_tools(self):
        server = McpServer(self.conn)
        cases = [
            ('search_knowledge', {'query': 'Architecture'}),
            ('get_document', {'uid': 'jira:API-2323'}),
            ('get_document_neighbors', {'uid': 'jira:API-2323'}),
            ('get_graph_subgraph', {}),
            ('find_related_documents', {'uid': 'jira:API-2323'}),
        ]
        import json
        for tool, args in cases:
            with self.subTest(tool=tool):
                response = server._execute_tool(tool, {**args, 'include_gitlab': False})
                self.assertFalse(response['isError'])
                self.assertNotIn('gitlab:7:abc', json.dumps(response['structuredContent']))
                self.assertTrue(server._execute_tool(tool, {**args, 'include_gitlab': 'false'})['isError'])
        self.assertTrue(server._execute_tool('get_document', {'uid': 'gitlab:7:abc', 'include_gitlab': False})['isError'])

    def test_filter_before_neighbor_limit(self):
        result = graph.get_neighbors(self.conn, 'jira:API-2323', direction='in', limit=1, include_gitlab=False)
        self.assertEqual(result['total'], 0)
        self.assertEqual(graph.get_document_detail(self.conn, 'jira:API-2323', include_gitlab=False)['incoming'], [])
        # A hidden commit must not consume the limit before a visible neighbour.
        self.link('confluence:1', 'jira:API-2323')
        result = graph.get_neighbors(self.conn, 'jira:API-2323', direction='in', limit=1, include_gitlab=False)
        self.assertEqual(result['incoming'][0]['source']['uid'], 'confluence:1')
        detail = graph.get_document_detail(self.conn, 'jira:API-2323', neighbor_limit=1, include_gitlab=False)
        self.assertEqual(detail['incoming'][0]['source']['uid'], 'confluence:1')

    def test_related_does_not_traverse_excluded_commit(self):
        self.link('gitlab:7:abc', 'confluence:1')
        result = graph.find_related(self.conn, 'jira:API-2323', include_gitlab=False)
        self.assertNotIn('confluence:1', {d['uid'] for d in result['related']})


class ImportanceCandidateTests(unittest.TestCase):
    def test_hub_survives_candidate_cap(self):
        conn = db.open_db(':memory:')
        self.addCleanup(conn.close)
        for n in range(210):
            uid = f'confluence:{n}'
            doc = models.FetchedDocument(uid, 'confluence', str(n), 'Architecture', '', 'Architecture')
            db.save_document(conn, doc, [models.Chunk('', 'Architecture')])
        db.add_edge(conn, 'confluence:209', models.LinkTarget('confluence:208', 'links_to'))
        results = search.search(conn, 'Architecture')['results']
        self.assertEqual({r['uid'] for r in results[:2]}, {'confluence:208', 'confluence:209'})
