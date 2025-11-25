# Copyright (C) 2022 Dremio Corporation
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest
import agate
from unittest.mock import Mock, patch, MagicMock
from dbt.adapters.dremio.api.cursor import DremioCursor
from dbt.adapters.dremio.api.rest.client import DremioRestClient
from dbt.adapters.dremio.api.parameters import Parameters
from dbt.adapters.dremio.api.authentication import DremioAuthentication


class TestDremioSubquery:
    """Test DREMIO_SUBQUERY substitution feature."""

    @pytest.fixture
    def mock_rest_client(self):
        """Create a mock DremioRestClient."""
        return Mock(spec=DremioRestClient)

    @pytest.fixture
    def cursor(self, mock_rest_client):
        """Create a DremioCursor instance with mocked rest client."""
        return DremioCursor(mock_rest_client)

    def test_format_query_result_integer(self, cursor):
        """Test formatting integer values."""
        result = cursor._format_query_result(42, "INTEGER")
        assert result == "42"
        assert result[0] != "'"  # Should not be quoted

    def test_format_query_result_bigint(self, cursor):
        """Test formatting bigint values."""
        result = cursor._format_query_result(123456789, "BIGINT")
        assert result == "123456789"
        assert result[0] != "'"  # Should not be quoted

    def test_format_query_result_decimal(self, cursor):
        """Test formatting decimal values."""
        result = cursor._format_query_result(123.45, "DECIMAL")
        assert result == "123.45"
        assert result[0] != "'"  # Should not be quoted

    def test_format_query_result_float(self, cursor):
        """Test formatting float values."""
        result = cursor._format_query_result(3.14, "FLOAT")
        assert result == "3.14"
        assert result[0] != "'"  # Should not be quoted

    def test_format_query_result_string(self, cursor):
        """Test formatting string values with proper quoting."""
        result = cursor._format_query_result("test", "VARCHAR")
        assert result == "'test'"
        assert result[0] == "'"  # Should be quoted

    def test_format_query_result_string_escaping(self, cursor):
        """Test escaping of single quotes in strings."""
        result = cursor._format_query_result("test'value", "VARCHAR")
        assert result == "'test''value'"
        assert "''" in result  # Single quote should be escaped

    def test_format_query_result_char(self, cursor):
        """Test formatting char values."""
        result = cursor._format_query_result("a", "CHAR")
        assert result == "'a'"
        assert result[0] == "'"  # Should be quoted

    def test_format_query_result_date(self, cursor):
        """Test formatting date values."""
        result = cursor._format_query_result("2024-01-15", "DATE")
        assert result == "'2024-01-15'"
        assert result[0] == "'"  # Should be quoted

    def test_format_query_result_timestamp(self, cursor):
        """Test formatting timestamp values."""
        result = cursor._format_query_result("2024-01-15 10:30:00", "TIMESTAMP")
        assert result == "'2024-01-15 10:30:00'"
        assert result[0] == "'"  # Should be quoted

    def test_format_query_result_null(self, cursor):
        """Test formatting NULL values."""
        result = cursor._format_query_result(None, "INTEGER")
        assert result == "NULL"
        assert result[0] != "'"  # Should not be quoted

    def test_format_query_result_boolean(self, cursor):
        """Test formatting boolean values."""
        result = cursor._format_query_result(True, "BOOLEAN")
        assert result.upper() == "TRUE"
        result = cursor._format_query_result(False, "BOOLEAN")
        assert result.upper() == "FALSE"

    def test_format_result_list(self, cursor):
        """Test formatting list of values for IN() clause."""
        values = [1, 2, 3]
        result = cursor._format_result_list(values, "INTEGER")
        assert "1" in result
        assert "2" in result
        assert "3" in result
        assert "," in result
        # Should not be quoted
        assert result[0] != "'"

    def test_format_result_list_strings(self, cursor):
        """Test formatting list of string values for IN() clause."""
        values = ["active", "pending", "completed"]
        result = cursor._format_result_list(values, "VARCHAR")
        assert "'active'" in result
        assert "'pending'" in result
        assert "'completed'" in result
        assert "," in result

    def test_multiline_subquery_pattern_detection(self, cursor):
        """Test that multi-line DREMIO_SUBQUERY patterns are detected."""
        sql = """
        SELECT * FROM table 
        WHERE status IN (
          /* DREMIO_SUBQUERY: 
             SELECT DISTINCT status 
             FROM other_table 
             WHERE active = true 
          */
        )
        """
        import re
        pattern = r'/\*\s*DREMIO_SUBQUERY:\s*(.*?)\s*\*/'
        matches = re.findall(pattern, sql, flags=re.DOTALL)
        assert len(matches) == 1
        assert "SELECT DISTINCT status" in matches[0]

    def test_multiple_subquery_patterns_detection(self, cursor):
        """Test that multiple DREMIO_SUBQUERY patterns are detected."""
        sql = """
        SELECT * FROM table 
        WHERE id >= /* DREMIO_SUBQUERY: SELECT 10 */
        AND status IN (/* DREMIO_SUBQUERY: SELECT 'active' */)
        """
        import re
        pattern = r'/\*\s*DREMIO_SUBQUERY:\s*(.*?)\s*\*/'
        matches = re.findall(pattern, sql, flags=re.DOTALL)
        assert len(matches) == 2

    @patch('dbt.adapters.dremio.api.cursor.DremioCursor')
    def test_single_value_integer_substitution(self, mock_cursor_class, cursor, mock_rest_client):
        """Test single integer value substitution."""
        # Create a mock subquery cursor
        mock_subquery_cursor = Mock(spec=DremioCursor)
        mock_table = Mock(spec=agate.Table)
        mock_table.rows = [(42,)]
        mock_subquery_cursor.table = mock_table
        mock_subquery_cursor.job_results.return_value = {
            "rowCount": 1,
            "schema": [{"name": "value", "type": {"name": "INTEGER"}}],
            "rows": [{"value": 42}]
        }
        
        # Make DremioCursor constructor return our mock
        mock_cursor_class.return_value = mock_subquery_cursor
        
        sql = "SELECT * FROM table WHERE id >= /* DREMIO_SUBQUERY: SELECT 42 */"
        result = cursor._substitute_dremio_subqueries(sql)
        
        assert "42" in result
        assert "/* DREMIO_SUBQUERY:" not in result
        mock_subquery_cursor.execute.assert_called_once()
        mock_subquery_cursor.close.assert_called_once()

    @patch('dbt.adapters.dremio.api.cursor.DremioCursor')
    def test_single_value_string_substitution(self, mock_cursor_class, cursor, mock_rest_client):
        """Test single string value substitution with proper quoting."""
        mock_subquery_cursor = Mock(spec=DremioCursor)
        mock_table = Mock(spec=agate.Table)
        mock_table.rows = [("test_value",)]
        mock_subquery_cursor.table = mock_table
        mock_subquery_cursor.job_results.return_value = {
            "rowCount": 1,
            "schema": [{"name": "value", "type": {"name": "VARCHAR"}}],
            "rows": [{"value": "test_value"}]
        }
        
        mock_cursor_class.return_value = mock_subquery_cursor
        
        sql = "SELECT * FROM table WHERE name = /* DREMIO_SUBQUERY: SELECT 'test_value' */"
        result = cursor._substitute_dremio_subqueries(sql)
        
        assert "'test_value'" in result
        assert "/* DREMIO_SUBQUERY:" not in result

    @patch('dbt.adapters.dremio.api.cursor.DremioCursor')
    def test_multiple_values_in_clause(self, mock_cursor_class, cursor, mock_rest_client):
        """Test multiple values substitution for IN() clause."""
        mock_subquery_cursor = Mock(spec=DremioCursor)
        mock_table = Mock(spec=agate.Table)
        mock_table.rows = [(1,), (2,), (3,)]
        mock_subquery_cursor.table = mock_table
        mock_subquery_cursor.job_results.return_value = {
            "rowCount": 3,
            "schema": [{"name": "id", "type": {"name": "INTEGER"}}],
            "rows": [{"id": 1}, {"id": 2}, {"id": 3}]
        }
        
        mock_cursor_class.return_value = mock_subquery_cursor
        
        sql = "SELECT * FROM table WHERE id IN (/* DREMIO_SUBQUERY: SELECT id FROM other_table */)"
        result = cursor._substitute_dremio_subqueries(sql)
        
        # Check that all values are present
        assert "1" in result
        assert "2" in result
        assert "3" in result
        assert "," in result
        assert "/* DREMIO_SUBQUERY:" not in result

    @patch('dbt.adapters.dremio.api.cursor.DremioCursor')
    def test_null_value_substitution(self, mock_cursor_class, cursor, mock_rest_client):
        """Test NULL value substitution."""
        mock_subquery_cursor = Mock(spec=DremioCursor)
        mock_table = Mock(spec=agate.Table)
        mock_table.rows = [(None,)]
        mock_subquery_cursor.table = mock_table
        mock_subquery_cursor.job_results.return_value = {
            "rowCount": 1,
            "schema": [{"name": "value", "type": {"name": "INTEGER"}}],
            "rows": [{"value": None}]
        }
        
        mock_cursor_class.return_value = mock_subquery_cursor
        
        sql = "SELECT * FROM table WHERE value = /* DREMIO_SUBQUERY: SELECT NULL */"
        result = cursor._substitute_dremio_subqueries(sql)
        
        assert "NULL" in result
        assert "'NULL'" not in result  # Should not be quoted

    @patch('dbt.adapters.dremio.api.cursor.DremioCursor')
    def test_empty_result_set_error(self, mock_cursor_class, cursor, mock_rest_client):
        """Test that empty result set raises an error."""
        mock_subquery_cursor = Mock(spec=DremioCursor)
        mock_table = Mock(spec=agate.Table)
        mock_table.rows = []
        mock_subquery_cursor.table = mock_table
        mock_subquery_cursor.job_results.return_value = {
            "rowCount": 0,
            "schema": [{"name": "value", "type": {"name": "INTEGER"}}],
            "rows": []
        }
        
        mock_cursor_class.return_value = mock_subquery_cursor
        
        sql = "SELECT * FROM table WHERE id = /* DREMIO_SUBQUERY: SELECT value FROM empty_table */"
        
        with pytest.raises(Exception) as exc_info:
            cursor._substitute_dremio_subqueries(sql)
        assert "no rows" in str(exc_info.value).lower() or "no results" in str(exc_info.value).lower()

    @patch('dbt.adapters.dremio.api.cursor.DremioCursor')
    def test_multiple_columns_warning(self, mock_cursor_class, cursor, mock_rest_client):
        """Test that multiple columns triggers a warning."""
        mock_subquery_cursor = Mock(spec=DremioCursor)
        mock_table = Mock(spec=agate.Table)
        mock_table.rows = [(1, "test")]
        mock_subquery_cursor.table = mock_table
        mock_subquery_cursor.job_results.return_value = {
            "rowCount": 1,
            "schema": [
                {"name": "id", "type": {"name": "INTEGER"}},
                {"name": "name", "type": {"name": "VARCHAR"}}
            ],
            "rows": [{"id": 1, "name": "test"}]
        }
        
        mock_cursor_class.return_value = mock_subquery_cursor
        
        sql = "SELECT * FROM table WHERE id = /* DREMIO_SUBQUERY: SELECT id, name FROM table */"
        
        with patch('dbt.adapters.dremio.api.cursor.logger') as mock_logger:
            result = cursor._substitute_dremio_subqueries(sql)
            # Should log a warning about multiple columns
            mock_logger.warning.assert_called()
            assert "columns" in str(mock_logger.warning.call_args).lower()
        
        # Should still use first column
        assert "1" in result

    @patch('dbt.adapters.dremio.api.cursor.DremioCursor')
    def test_subquery_recursion_prevention(self, mock_cursor_class, cursor, mock_rest_client):
        """Test that sub-queries don't process DREMIO_SUBQUERY patterns (no recursion)."""
        mock_subquery_cursor = Mock(spec=DremioCursor)
        mock_table = Mock(spec=agate.Table)
        mock_table.rows = [(42,)]
        mock_subquery_cursor.table = mock_table
        mock_subquery_cursor.job_results.return_value = {
            "rowCount": 1,
            "schema": [{"name": "value", "type": {"name": "INTEGER"}}],
            "rows": [{"value": 42}]
        }
        
        mock_cursor_class.return_value = mock_subquery_cursor
        
        sql = "SELECT * FROM table WHERE id = /* DREMIO_SUBQUERY: SELECT 42 */"
        cursor._substitute_dremio_subqueries(sql)
        
        # Verify that execute was called with skip_subquery_substitution=True
        call_args = mock_subquery_cursor.execute.call_args
        assert call_args[1]['skip_subquery_substitution'] is True

    def test_type_formatting_coverage(self, cursor):
        """Test that all supported types are formatted correctly."""
        test_cases = [
            ("INTEGER", 42, False),  # (type, value, should_be_quoted)
            ("BIGINT", 123456789, False),
            ("SMALLINT", 10, False),
            ("TINYINT", 5, False),
            ("DECIMAL", 123.45, False),
            ("NUMERIC", 99.99, False),
            ("FLOAT", 3.14, False),
            ("DOUBLE", 2.718, False),
            ("REAL", 1.5, False),
            ("VARCHAR", "test", True),
            ("CHAR", "a", True),
            ("CHARACTER", "b", True),
            ("TEXT", "text", True),
            ("STRING", "string", True),
            ("DATE", "2024-01-15", True),
            ("TIME", "10:30:00", True),
            ("TIMESTAMP", "2024-01-15 10:30:00", True),
            ("DATETIME", "2024-01-15 10:30:00", True),
            ("BOOLEAN", True, False),
            ("BIT", False, False),
        ]
        
        for data_type, value, should_be_quoted in test_cases:
            result = cursor._format_query_result(value, data_type)
            if should_be_quoted:
                assert result[0] == "'", f"{data_type} should be quoted, got: {result}"
            else:
                assert result[0] != "'", f"{data_type} should not be quoted, got: {result}"
