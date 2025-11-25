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


import re
import time

import agate

from dbt.adapters.dremio.api.rest.client import DremioRestClient

from dbt.adapters.events.logging import AdapterLogger

logger = AdapterLogger("dremio")


class DremioCursor:
    def __init__(self, rest_client: DremioRestClient):
        self._rest_client = rest_client

        self._closed = False
        self._job_id = None
        self._rowcount = -1
        self._job_results = None
        self._table_results: agate.Table = None
        self._description = None

    @property
    def description(self):
        return self._description

    @description.setter
    def description(self, value):
        self._description = value

    @property
    def closed(self):
        return self._closed

    @closed.setter
    def closed(self, new_closed_value):
        self._closed = new_closed_value

    @property
    def rowcount(self):
        return self._rowcount

    @property
    def table(self) -> agate.Table:
        return self._table_results

    def job_results(self):
        if self.closed:
            raise Exception("CursorClosed")
        if self._job_results is None:
            self._populate_job_results()

        return self._job_results

    def job_cancel(self):
        # cancels current job
        logger.debug(f"Cancelling job {self._job_id}")
        return self._rest_client.job_cancel_api(self._job_id)

    def close(self):
        if self.closed:
            raise Exception("CursorClosed")
        self._initialize()
        self.closed = True

    def execute(self, sql, bindings=None, fetch=False, skip_subquery_substitution=False):
        if self.closed:
            raise Exception("CursorClosed")
        if bindings is None:
            self._initialize()

            # Process DREMIO_SUBQUERY substitutions before executing
            # Skip for sub-queries to avoid infinite recursion
            if not skip_subquery_substitution:
                original_sql = sql
                sql = self._substitute_dremio_subqueries(sql)
                if sql != original_sql:
                    logger.debug("DREMIO_SUBQUERY substitution completed. SQL modified.")
                    # Log the full substituted SQL at debug level for detailed troubleshooting
                    logger.debug("=" * 80)
                    logger.debug("FINAL SQL (after DREMIO_SUBQUERY substitution):")
                    logger.debug("=" * 80)
                    logger.debug(sql)
                    logger.debug("=" * 80)
                    # Verify all patterns were replaced
                    if "/* DREMIO_SUBQUERY:" not in sql:
                        logger.debug("All DREMIO_SUBQUERY patterns successfully replaced")
                    else:
                        logger.warning("Some DREMIO_SUBQUERY patterns remain after substitution")

            json_payload = self._rest_client.sql_endpoint(sql, context=None)

            self._job_id = json_payload["id"]

            self._populate_rowcount()
            if fetch:
                self._populate_job_results()
            self._populate_results_table()

        else:
            raise Exception("Bindings not currently supported.")

    def fetchone(self):
        row = None
        if self._table_results is not None:
            row = self._table_results.rows[0]
        return row

    def fetchall(self):
        logger.debug(f"The fetch result is: {self._table_results.rows}")
        return self._table_results.rows

    def _initialize(self):
        self._job_id = None
        self._rowcount = -1
        self._table_results = None
        self._job_results = None

    def _populate_rowcount(self):
        if self.closed:
            raise Exception("CursorClosed")
        # keep checking job status until status is one of COMPLETE, CANCELLED or FAILED
        # map job results to AdapterResponse
        job_id = self._job_id

        last_job_state = ""
        job_status_response = self._rest_client.job_status(job_id)
        job_status_state = job_status_response["jobState"]

        while True:
            time.sleep(0.2)
            if job_status_state != last_job_state:
                logger.debug(f"Job State = {job_status_state}")

            if job_status_state == "FAILED":
                error_message = job_status_response["errorMessage"]
                raise Exception(f"ERROR: {error_message}")

            if job_status_state == "CANCELLED":
                raise Exception("Job was cancelled")

            if job_status_state == "COMPLETED":
                break
            last_job_state = job_status_state
            job_status_response = self._rest_client.job_status(job_id)
            job_status_state = job_status_response["jobState"]

        # this is done as job status does not return a rowCount if there are no rows affected (even in completed job_state)
        # pyodbc Cursor documentation states "[rowCount] is -1 if no SQL has been executed or if the number of rows is unknown.
        # Note that it is not uncommon for databases to report -1 immediately after a SQL select statement for performance reasons."
        if "rowCount" not in job_status_response:
            rows = -1
            logger.debug("rowCount does not exist in job_status payload")
        else:
            rows = job_status_response["rowCount"]

        self._rowcount = rows

    def _populate_job_results(self, row_limit=500):
        if self._job_results is None:
            combined_job_results = self._rest_client.job_results(
                self._job_id,
                offset=0,
                limit=row_limit,
            )
            total_row_count = combined_job_results["rowCount"]
            current_row_count = len(combined_job_results["rows"])

            if total_row_count > 100000:
                logger.warning(
                    "Fetching more than 100000 records. This may result in slower performance."
                )

            while current_row_count < total_row_count:
                combined_job_results["rows"].extend(
                    self._rest_client.job_results(
                        self._job_id,
                        offset=current_row_count,
                        limit=row_limit,
                    )["rows"]
                )
                current_row_count += row_limit

            self._job_results = combined_job_results

    def _populate_results_table(self):
        if self._job_results is not None:
            tester = agate.TypeTester()
            json_rows = self._job_results["rows"]
            # Used to force agate to use a specific .DataType for some type values that can be misinterpreted as Boolean
            force = {}
            self._table_results = json_rows
            for col in self._job_results["schema"]:
                name = col["name"]
                data_type_str = col["type"]["name"]
                if data_type_str == "BIGINT" or data_type_str == "INTEGER":
                    force[name] = agate.Number()
            if force:
                tester = agate.TypeTester(force=force)
            self._table_results = agate.Table.from_object(
                json_rows, column_types=tester
            )

    def _substitute_dremio_subqueries(self, sql: str) -> str:
        """
        Find and replace all /* DREMIO_SUBQUERY: ... */ patterns with their query results.
        
        Args:
            sql: The SQL string that may contain DREMIO_SUBQUERY patterns
            
        Returns:
            SQL string with DREMIO_SUBQUERY patterns replaced by their results
        """
        # Pattern to match /* DREMIO_SUBQUERY: ... */ (with DOTALL for multi-line)
        pattern = r'/\*\s*DREMIO_SUBQUERY:\s*(.*?)\s*\*/'
        
        # Check if there are any matches first
        matches = list(re.finditer(pattern, sql, flags=re.DOTALL))
        if not matches:
            return sql  # No DREMIO_SUBQUERY patterns found, return as-is
        
        logger.debug(f"Found {len(matches)} DREMIO_SUBQUERY pattern(s) to process")
        
        # Extract MERGE statement context to resolve DBT_INTERNAL_SOURCE and DBT_INTERNAL_DEST aliases
        merge_source_relation = None
        merge_dest_relation = None
        
        # Try to find MERGE statement and extract source/dest relations
        # Pattern: merge into <dest> as DBT_INTERNAL_DEST using <source> as DBT_INTERNAL_SOURCE
        # Handle quoted identifiers and multi-part names with spaces (e.g., "db"."schema"."table")
        # Use non-greedy matching to capture everything up to "as DBT_INTERNAL_DEST/USOURCE"
        merge_pattern = r'merge\s+into\s+(.+?)\s+as\s+DBT_INTERNAL_DEST\s+using\s+(.+?)\s+as\s+DBT_INTERNAL_SOURCE'
        merge_match = re.search(merge_pattern, sql, re.IGNORECASE | re.DOTALL)
        if merge_match:
            merge_dest_relation = merge_match.group(1).strip()
            merge_source_relation = merge_match.group(2).strip()
            logger.debug(f"Found MERGE context: DBT_INTERNAL_DEST={merge_dest_relation}, DBT_INTERNAL_SOURCE={merge_source_relation}")
        
        def replace_subquery(match):
            subquery_sql = match.group(1).strip()
            logger.debug(f"Processing DREMIO_SUBQUERY: {subquery_sql[:200]}...")
            
            # Check if this DREMIO_SUBQUERY is in an IN clause context
            # Look for "IN" keyword before the comment (with optional whitespace and optional opening paren)
            match_start = match.start()
            # Look back up to 100 characters to find "IN" keyword
            context_before = sql[max(0, match_start - 100):match_start]
            # Pattern: word boundary, "IN", optional whitespace, optional "(", then whitespace/comment
            is_in_clause = re.search(r'\bIN\s*\(?\s*(?:/\*|$)', context_before, re.IGNORECASE) is not None
            # Check if there's already an opening parenthesis right before the DREMIO_SUBQUERY
            has_opening_paren = context_before.rstrip().endswith('(')
            
            # Replace MERGE aliases with actual relation names if found
            if merge_source_relation and 'DBT_INTERNAL_SOURCE' in subquery_sql:
                subquery_sql = subquery_sql.replace('DBT_INTERNAL_SOURCE', merge_source_relation)
                logger.debug(f"Replaced DBT_INTERNAL_SOURCE with {merge_source_relation}")
                logger.debug(f"Subquery after alias replacement: {subquery_sql}")
            if merge_dest_relation and 'DBT_INTERNAL_DEST' in subquery_sql:
                subquery_sql = subquery_sql.replace('DBT_INTERNAL_DEST', merge_dest_relation)
                logger.debug(f"Replaced DBT_INTERNAL_DEST with {merge_dest_relation}")
                logger.debug(f"Subquery after alias replacement: {subquery_sql}")
            
            subquery_cursor = None
            try:
                # Execute the sub-query using a new cursor to avoid state conflicts
                # Skip DREMIO_SUBQUERY substitution to prevent infinite recursion
                subquery_cursor = DremioCursor(self._rest_client)
                subquery_cursor.execute(subquery_sql, fetch=True, skip_subquery_substitution=True)
                
                # Get schema information for type detection
                job_results = subquery_cursor.job_results()
                schema = job_results.get("schema", [])
                
                if len(schema) == 0:
                    raise Exception(f"DREMIO_SUBQUERY returned no schema information: {subquery_sql}")
                
                # Get data type from first column
                data_type = schema[0].get("type", {}).get("name", "VARCHAR")
                
                # Get all rows from the table
                if subquery_cursor.table is None:
                    raise Exception(f"DREMIO_SUBQUERY returned no results: {subquery_sql}")
                
                rows = subquery_cursor.table.rows
                num_rows = len(rows)
                num_cols = len(schema)
                
                if num_rows == 0:
                    # Handle empty result set gracefully - return NULL
                    logger.debug(f"DREMIO_SUBQUERY returned no rows: {subquery_sql}")
                    logger.debug("Empty result - returning NULL")
                    return "NULL"
                
                # Warn if multiple columns (use first column)
                if num_cols > 1:
                    logger.warning(
                        f"DREMIO_SUBQUERY returned {num_cols} columns, using first column only: {subquery_sql}"
                    )
                
                # Extract values from first column
                values = []
                for row in rows:
                    if len(row) > 0:
                        values.append(row[0])
                
                if len(values) == 0:
                    raise Exception(f"DREMIO_SUBQUERY returned no values: {subquery_sql}")
                
                # Format based on number of rows
                if num_rows == 1:
                    # Single value substitution
                    formatted_result = self._format_query_result(values[0], data_type)
                    # If used in IN clause and there's no opening paren already, wrap in parentheses
                    if is_in_clause and not has_opening_paren:
                        formatted_result = f"({formatted_result})"
                    logger.debug(f"DREMIO_SUBQUERY result (single value, type={data_type}): {formatted_result}")
                    return formatted_result
                else:
                    # Multiple values for IN() clause
                    formatted_result = self._format_result_list(values, data_type)
                    # Only wrap in parentheses if there's no opening paren already
                    if not has_opening_paren:
                        formatted_result = f"({formatted_result})"
                    logger.debug(f"DREMIO_SUBQUERY result ({num_rows} values, type={data_type}): {formatted_result[:200]}...")
                    logger.debug(f"Full DREMIO_SUBQUERY result: {formatted_result}")
                    return formatted_result
                    
            except Exception as e:
                error_msg = f"Error executing DREMIO_SUBQUERY '{subquery_sql}': {str(e)}"
                logger.error(error_msg)
                raise Exception(error_msg)
            finally:
                # Clean up subquery cursor
                if subquery_cursor is not None:
                    try:
                        subquery_cursor.close()
                    except Exception:
                        pass  # Ignore errors during cleanup
        
        # Find all matches and replace them
        try:
            result_sql = re.sub(pattern, replace_subquery, sql, flags=re.DOTALL)
            if result_sql != sql:
                logger.debug("DREMIO_SUBQUERY substitution successful")
            return result_sql
        except Exception as e:
            logger.error(f"Error during DREMIO_SUBQUERY substitution: {str(e)}")
            # Re-raise to fail the query rather than silently continuing
            raise

    def _format_query_result(self, value, data_type: str) -> str:
        """
        Format a single query result value based on its data type.
        
        Args:
            value: The value to format
            data_type: The SQL data type name (e.g., 'INTEGER', 'VARCHAR', 'DATE')
            
        Returns:
            Formatted string representation suitable for SQL substitution
        """
        # Handle NULL
        if value is None:
            return "NULL"
        
        data_type_upper = data_type.upper()
        
        # Numeric types: unquoted
        if data_type_upper in ('INTEGER', 'BIGINT', 'SMALLINT', 'TINYINT', 
                               'DECIMAL', 'NUMERIC', 'FLOAT', 'DOUBLE', 'REAL'):
            return str(value)
        
        # Boolean: unquoted
        if data_type_upper in ('BOOLEAN', 'BIT'):
            return str(value).upper()
        
        # String types: quoted with proper escaping
        if data_type_upper in ('VARCHAR', 'CHAR', 'CHARACTER', 'TEXT', 'STRING'):
            # Escape single quotes by doubling them
            escaped_value = str(value).replace("'", "''")
            return f"'{escaped_value}'"
        
        # Date/Time types: quoted as strings
        if data_type_upper in ('DATE', 'TIME', 'TIMESTAMP', 'DATETIME'):
            # Format as string and quote
            escaped_value = str(value).replace("'", "''")
            return f"'{escaped_value}'"
        
        # Default: treat as string (quoted)
        escaped_value = str(value).replace("'", "''")
        return f"'{escaped_value}'"

    def _format_result_list(self, values: list, data_type: str) -> str:
        """
        Format a list of query result values for use in IN() clauses.
        
        Args:
            values: List of values to format
            data_type: The SQL data type name
            
        Returns:
            Comma-separated formatted values suitable for IN() clause
        """
        formatted_values = [self._format_query_result(value, data_type) for value in values]
        return ", ".join(formatted_values)
