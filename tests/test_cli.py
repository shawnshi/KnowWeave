import unittest
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

if __name__ == '__main__':
    unittest.main()
