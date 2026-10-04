import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import square_scraper as s


class ScraperTests(unittest.TestCase):
    def test_actual_product_ignores_corporate_pdf(self):
        page = s.parse_detail(Path('detail.sample.html').read_text(encoding='utf-8'), 147)
        self.assertEqual(page['brand_name'], 'Ace')
        self.assertEqual(page['generic_name'], 'Paracetamol')
        self.assertIn('1717581535_pdoc_Ace', page['pdf_url'])
        self.assertIn(' | ', page['page_sections']['dosage_and_administration'])

    def test_actual_ocr_sections_and_liquid_strength(self):
        sections = s.split_pdf_sections(Path('ace-prescribing.sample.txt').read_text(encoding='utf-8'))
        self.assertIn('Known sensitivity', sections['contraindications'])
        self.assertIn('out of the reach', sections['storage_conditions'])
        strength = s.find_strengths(sections['_composition'])
        self.assertIn('120 mg/5 ml', strength)
        self.assertIn('80 mg/1 ml', strength)
        self.assertIsNone(s.find_strengths('Each box contains 60 ml syrup'))

    def test_inline_composition_not_discarded(self):
        result = s.split_pdf_sections('COMPOSITION\nEach tablet contains Example 5 mg.\nINDICATIONS: Example indication')
        self.assertIn('5 mg', result['_composition'])
        self.assertEqual(result['indications'], 'Example indication')

    def test_existing_schema_update_preserves_missing_values(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (42,)
        s.upsert(conn, {'source_url': 'https://example.test/147', 'brand_name': 'Ace', 'pharmacology': None})
        sql, values = cursor.execute.call_args.args
        self.assertIn('UPDATE', sql)
        self.assertNotIn('pharmacology', sql)
        self.assertEqual(values, ['Ace', 42])
        conn.commit.assert_called_once()

    def test_insert_uses_requested_columns(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        s.upsert(conn, {'source_url': 'https://example.test/147', 'brand_name': 'Ace'})
        sql = cursor.execute.call_args.args[0]
        self.assertIn('INSERT INTO', sql)
        self.assertNotIn('`pid`', sql)
        self.assertNotIn('`pdf_url`', sql)

    def test_failed_list_page_does_not_silently_skip(self):
        response = MagicMock(text='<a href="products-by-tradename.php?type=pharma&char=B">B</a>')
        with patch.object(s, 'get', side_effect=[response, RuntimeError('offline')]):
            with self.assertRaises(RuntimeError):
                s.collect_pids(None)


if __name__ == '__main__':
    unittest.main()
