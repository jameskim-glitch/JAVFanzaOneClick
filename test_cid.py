import unittest
from unittest.mock import patch

import jav_fanza_cid as cid
import fanza_core as core
import entity_images


class ProductIdentifiers(unittest.TestCase):
    def test_normalize(self):
        for value in ['EXAMPLE-007', '[HD] example – 007.mp4', 'ＥＸＡＭＰＬＥ－００７']:
            self.assertEqual(cid.normalize_subject_id(value), 'EXAMPLE-007')

    def test_invalid(self):
        for value in [None, '', 'movie.mp4', 'A-1', 'EXAMPLE-123456789', 'EXAMPLE-7ABC']:
            self.assertEqual(cid.make_cid_candidates(value), [])

    def test_bounded_deterministic_hints(self):
        self.assertEqual(cid.make_cid_candidates('EXAMPLE-007'), ['example00007', 'example007'])
        self.assertEqual(cid.make_cid_candidates('EXAMPLE-07A'), ['example00007a', 'example07a', 'example007a'])
        self.assertLessEqual(len(cid.make_cid_candidates('EXAMPLE-12345678')), 3)

    def test_no_supplier_guesses(self):
        self.assertTrue(all(v.startswith('example') for v in cid.make_cid_candidates('EXAMPLE-7')))
        self.assertFalse(hasattr(cid, 'CID_RULES'))

    def test_returned_ids(self):
        for value in ['example00007', '9example007', 'h_321example00007']:
            self.assertTrue(cid.matches_discovered_id('EXAMPLE-7', value))
        for value in ['other00007', 'example00070', 'example00007e', 'myexample00007', '../example00007', 'example00007/']:
            self.assertFalse(cid.matches_discovered_id('EXAMPLE-7', value))
        self.assertTrue(cid.matches_discovered_id('EXAMPLE-7A', 'h_321example00007a'))
        self.assertFalse(cid.matches_discovered_id('EXAMPLE-7A', 'h_321example00007'))

    def test_urls_are_fixed_origin(self):
        self.assertTrue(all(u.startswith('https://pics.dmm.co.jp/') for u in cid.make_image_urls('EXAMPLE-7')))
        self.assertEqual(cid.make_product_urls(None), [])

    def search(self, entries):
        calls = []
        def reply(url, body):
            calls.append(body)
            if body.get('operationName') == 'ContentPageData':
                return {'data': {'ppvContent': None}}
            return {'data': {'legacySearchPPV': {'result': {'contents': entries}}}}
        with patch.object(core.HTTP, 'post_json', side_effect=reply):
            result = core.fetch_fanza_graphql('EXAMPLE-007')
        return result, calls

    def test_discovery_not_table(self):
        result, calls = self.search([{'id': 'h_321example00007', 'title': 'Sample'}])
        self.assertEqual(result['title'], 'Sample')
        self.assertIn('h_321example00007', result['urls'][0])
        searches = [c for c in calls if c.get('operationName') != 'ContentPageData']
        self.assertIn('EXAMPLE-007', searches[0]['query'])

    def test_ambiguous_discovery_is_not_selected(self):
        result, _ = self.search([{'id': 'h_321example00007', 'title': 'Sample A'},
                                 {'id': 'h_654example00007', 'title': 'Sample B'}])
        self.assertIsNone(result)

    def test_title_alone_is_not_identity(self):
        result, _ = self.search([{'id': 'other00007', 'title': 'EXAMPLE-007'}])
        self.assertIsNone(result)

    def test_image_lookup_uses_validated_stored_url(self):
        profiles = entity_images.FanzaProfiles()
        with patch.object(profiles, 'content', return_value={'id': 'h_321example00007'}) as fetch:
            result = profiles.scene_sources({'urls': ['https://video.dmm.co.jp/av/content/?id=h_321example00007']}, 'EXAMPLE-007')
        self.assertEqual(result['id'], 'h_321example00007')
        fetch.assert_called_once_with('h_321example00007')


if __name__ == '__main__':
    unittest.main()
