from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import server


SHEETS = [
    {'sheetId': 101, 'title': 'Summary', 'index': 0, 'rowCount': 100, 'columnCount': 20},
    {'sheetId': 202, 'title': "Team's data", 'index': 1, 'rowCount': 200, 'columnCount': 30},
]


class ResolveNotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {'GOOGLE_ALLOWED_SPREADSHEET_IDS': ''}, clear=False)
        self.env.start()
        self.metadata = patch.object(server, 'list_sheet_metadata', return_value=SHEETS)
        self.metadata.start()

    def tearDown(self) -> None:
        self.metadata.stop()
        self.env.stop()

    def test_explicit_a1_range_passes_through(self) -> None:
        result = server.resolve_notation_to_range('book', "'Summary'!A1:B2")
        self.assertEqual(result['resolvedRange'], "'Summary'!A1:B2")
        self.assertEqual(result['mode'], 'explicit')

    def test_positional_notation_resolves_and_quotes_title(self) -> None:
        result = server.resolve_notation_to_range('book', '!!A1:G5')
        self.assertEqual(result['resolvedRange'], "'Team''s data'!A1:G5")
        self.assertEqual(result['sheetId'], 202)
        self.assertEqual(result['mode'], 'bang-notation')

    def test_stable_sheet_id_notation_survives_position_changes(self) -> None:
        result = server.resolve_notation_to_range('book', '@202!13:13')
        self.assertEqual(result['resolvedRange'], "'Team''s data'!13:13")
        self.assertEqual(result['sheetId'], 202)
        self.assertEqual(result['mode'], 'sheet-id-notation')

    def test_unknown_sheet_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, 'sheetId 999'):
            server.resolve_notation_to_range('book', '@999!A1')


class SafetyTests(unittest.TestCase):
    def test_allowlist_rejects_unlisted_spreadsheet(self) -> None:
        with patch.dict(os.environ, {'GOOGLE_ALLOWED_SPREADSHEET_IDS': 'allowed-1,allowed-2'}):
            with self.assertRaises(PermissionError):
                server.ensure_spreadsheet_allowed('other')

    def test_allowlist_accepts_listed_spreadsheet(self) -> None:
        with patch.dict(os.environ, {'GOOGLE_ALLOWED_SPREADSHEET_IDS': 'allowed-1,allowed-2'}):
            server.ensure_spreadsheet_allowed('allowed-2')

    def test_allowlist_parsing_trims_values(self) -> None:
        with patch.dict(os.environ, {'GOOGLE_ALLOWED_SPREADSHEET_IDS': 'allowed-1'}):
            self.assertEqual(server.allowed_spreadsheet_ids(), {'allowed-1'})

    def test_write_cell_limit_is_enforced(self) -> None:
        with patch.dict(os.environ, {'GOOGLE_MAX_WRITE_CELLS': '3'}):
            with self.assertRaisesRegex(ValueError, 'configured limit is 3'):
                server.validate_values([[1, 2], [3, 4]])

    def test_values_must_be_two_dimensional(self) -> None:
        with self.assertRaisesRegex(ValueError, r'values\[0\] must be an array'):
            server.validate_values([1, 2])

    def test_update_cells_dry_run_does_not_call_google(self) -> None:
        sheets_service = MagicMock()
        original_service = server.SHEETS_SERVICE
        server.SHEETS_SERVICE = sheets_service
        try:
            with (
                patch.dict(os.environ, {'GOOGLE_ALLOWED_SPREADSHEET_IDS': ''}, clear=False),
                patch.object(server, 'audit_mutation') as audit,
            ):
                result = server.update_cells('book', 'Sheet1!A1:B1', '[[1, 2]]', dry_run=True)
        finally:
            server.SHEETS_SERVICE = original_service
        self.assertEqual(result['status'], 'dry-run')
        self.assertEqual(result['cells'], 2)
        audit.assert_called_once()
        sheets_service.spreadsheets.assert_not_called()

    def test_insert_rows_dry_run_does_not_call_google(self) -> None:
        sheets_service = MagicMock()
        original_service = server.SHEETS_SERVICE
        server.SHEETS_SERVICE = sheets_service
        try:
            with (
                patch.dict(os.environ, {'GOOGLE_ALLOWED_SPREADSHEET_IDS': ''}, clear=False),
                patch.object(server, 'audit_mutation') as audit,
            ):
                result = server.insert_rows('book', 101, 5, row_count=2, dry_run=True)
        finally:
            server.SHEETS_SERVICE = original_service
        self.assertEqual(result['status'], 'dry-run')
        self.assertEqual(result['rowCount'], 2)
        audit.assert_called_once()
        sheets_service.spreadsheets.assert_not_called()


if __name__ == '__main__':
    unittest.main()
