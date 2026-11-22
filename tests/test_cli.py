import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vector_lake import cli_app


class TestCLI(unittest.TestCase):
    @patch('vector_lake.tools.doctor_vector_lake')
    def test_doctor_command(self, mock_doctor):
        mock_doctor.return_value = "Healthy"
        with patch('sys.argv', ['cli.py', 'doctor']):
            result = cli_app.main()
        self.assertEqual(result, 0)
        mock_doctor.assert_called_once()

    @patch('vector_lake.tools.lint_vector_lake')
    def test_lint_command(self, mock_lint):
        mock_lint.return_value = "Lint Passed"
        with patch('sys.argv', ['cli.py', 'lint']):
            result = cli_app.main()
        self.assertEqual(result, 0)
        mock_lint.assert_called_once_with(False)

    @patch('vector_lake.tools.search_vector_lake')
    def test_search_command(self, mock_search):
        mock_search.return_value = "Search Results"
        with patch('sys.argv', ['cli.py', 'search', 'test_query', '--top_k', '3', '--domain', 'Medical_IT']):
            result = cli_app.main()
        self.assertEqual(result, 0)
        mock_search.assert_called_once_with(
            'test_query', 3, domain='Medical_IT', cluster='', include_history=False, mode='page',
            allow_external_embedding=False, profile='auto', use_chunks=False,
        )

    @patch('vector_lake.tools.prepare_query_context')
    def test_query_command(self, mock_query):
        mock_query.return_value = "Query Completed"
        with patch('sys.argv', ['cli.py', 'query', 'test_question', '--dry-run', '--domain', 'Medical_IT']):
            result = cli_app.main()
        self.assertEqual(result, 0)
        mock_query.assert_called_once_with(
            'test_question', True, domain='Medical_IT', cluster=None,
            allow_external_embedding=False, profile='auto', use_chunks=False,
        )

    @patch('vector_lake.tools.gc_vector_lake')
    def test_gc_command_plan_file_and_force_preview(self, mock_gc):
        with tempfile.TemporaryDirectory() as td:
            plan_path = Path(td) / "gc_plan.txt"
            plan_payload = {"scope": {"domain": "Medical_IT"}, "target_keys": ["Concept_A"]}
            plan_path.write_text("Preview summary\nExpected plan: " + json.dumps(plan_payload), encoding="utf-8")
            mock_gc.return_value = "[APPLIED]"
            with patch('sys.argv', ['cli.py', 'gc', '--days', '14', '--domain', 'Medical_IT', '--cluster', 'Core', '--plan-file', str(plan_path), '--apply']):
                result = cli_app.main()
            self.assertEqual(result, 0)
            mock_gc.assert_called_once_with(
                days=14, dry_run=False, force=False, domain='Medical_IT', cluster='Core', expected_plan=plan_payload,
            )

        mock_gc.reset_mock()
        auto_plan = {"scope": {"domain": "General"}, "target_keys": ["Concept_B"]}
        mock_gc.side_effect = ["Expected plan: " + json.dumps(auto_plan), "[APPLIED]"]
        with patch('sys.argv', ['cli.py', 'gc', '--domain', 'General', '--apply', '--force']):
            result = cli_app.main()
        self.assertEqual(result, 0)
        self.assertEqual(mock_gc.call_count, 2)
        self.assertEqual(mock_gc.call_args_list[0].kwargs, {'days': 30, 'dry_run': True, 'domain': 'General', 'cluster': ''})
        self.assertEqual(mock_gc.call_args_list[1].kwargs, {
            'days': 30, 'dry_run': False, 'force': True, 'domain': 'General', 'cluster': '', 'expected_plan': auto_plan,
        })

    @patch('vector_lake.tools.outbox_recovery')
    def test_outbox_command_list_retry_and_clear(self, mock_outbox):
        mock_outbox.return_value = "[DRY RUN] ok"
        with patch('sys.argv', ['cli.py', 'outbox']):
            self.assertEqual(cli_app.main(), 0)
        mock_outbox.assert_called_with(retry=False, clear_failed=False, filename='', dry_run=True)

        with patch('sys.argv', ['cli.py', 'outbox', '--retry', '--filename', 'Concept_A.md', '--apply']):
            self.assertEqual(cli_app.main(), 0)
        mock_outbox.assert_called_with(retry=True, clear_failed=False, filename='Concept_A.md', dry_run=False)

        with patch('sys.argv', ['cli.py', 'outbox', '--clear-failed', '--apply']):
            self.assertEqual(cli_app.main(), 0)
        mock_outbox.assert_called_with(retry=False, clear_failed=True, filename='', dry_run=False)

    @patch('vector_lake.tools.delete_source')
    def test_delete_command_dry_run_and_apply(self, mock_delete):
        mock_delete.return_value = "[DRY RUN]"
        with patch('sys.argv', ['cli.py', 'delete', 'raw/notes.md']):
            self.assertEqual(cli_app.main(), 0)
        mock_delete.assert_called_once_with('raw/notes.md', dry_run=True)

        mock_delete.reset_mock()
        with patch('sys.argv', ['cli.py', 'delete', 'raw/notes.md', '--apply']):
            self.assertEqual(cli_app.main(), 0)
        mock_delete.assert_called_once_with('raw/notes.md', dry_run=False)

    @patch('vector_lake.operational_status.render_status', return_value='healthy')
    @patch('vector_lake.operational_status.collect_status', return_value={'database': {'state': 'healthy'}})
    def test_ops_status_command(self, mock_collect, _mock_render):
        with patch('sys.argv', ['cli.py', 'ops-status', '--db', 'dummy.db', '--budget-seconds', '3.5']):
            self.assertEqual(cli_app.main(), 0)
        mock_collect.assert_called_once_with('dummy.db', meta_dir=None, budget_seconds=3.5)

    @patch('vector_lake.tools.expire_ingest_tasks', return_value='expired')
    @patch('vector_lake.tools.list_ingest_tasks', return_value='tasks')
    def test_ingest_tasks_command(self, mock_list, mock_expire):
        with patch('sys.argv', ['cli.py', 'ingest-tasks', '--limit', '15']):
            self.assertEqual(cli_app.main(), 0)
        mock_list.assert_called_once_with(limit=15, include_queued=True)

        with patch('sys.argv', ['cli.py', 'ingest-tasks', '--expire-stale', '--max-age-seconds', '120']):
            self.assertEqual(cli_app.main(), 0)
        mock_expire.assert_called_once_with(120)

    @patch('vector_lake.tools.repair_timeline_projection', return_value='repaired')
    @patch('vector_lake.tools.backup_retention_report', return_value='backups')
    def test_timeline_repair_and_backup_retention_commands(self, mock_backup, mock_timeline):
        with patch('sys.argv', ['cli.py', 'timeline-repair', '--apply']):
            self.assertEqual(cli_app.main(), 0)
        mock_timeline.assert_called_once_with(dry_run=False)

        with patch('sys.argv', ['cli.py', 'backup-retention', '--keep', '3', '--max-bytes', '1024', '--apply']):
            self.assertEqual(cli_app.main(), 0)
        mock_backup.assert_called_once_with(keep=3, max_bytes=1024, dry_run=False)


if __name__ == '__main__':
    unittest.main()
