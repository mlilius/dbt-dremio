# DREMIO_SUBQUERY Feature

The `DREMIO_SUBQUERY` feature allows you to dynamically substitute query results into your SQL statements before they are executed. This enables you to use literal values from subqueries anywhere in your SQL, making it particularly powerful for:

- **Incremental materializations**: Use literal values in predicates for Iceberg table partition pruning
- **Dynamic filtering**: Filter data based on values from other tables or the current model
- **Configuration-driven queries**: Use values from configuration tables to control query behavior
- **Date range processing**: Get date ranges from control tables for time-based filtering

## Overview

`DREMIO_SUBQUERY` uses a special SQL comment syntax that gets replaced with the actual results of executing that subquery. The substitution happens automatically before the SQL is sent to Dremio, enabling dynamic predicate values that can optimize query performance through partition pruning.

## Syntax

```sql
/* DREMIO_SUBQUERY: <your_subquery_here> */
```

The subquery must return a single column. If it returns multiple rows, the values will be formatted as a comma-separated list suitable for `IN()` clauses.

## Usage Examples

### Using in WHERE Clauses

You can use `DREMIO_SUBQUERY` directly in your model's WHERE clause:

```sql
SELECT 
    id,
    name,
    created_date,
    status
FROM {{ ref('source_table') }}
WHERE 
    created_date >= /* DREMIO_SUBQUERY: SELECT MAX(last_processed_date) FROM {{ ref('processing_log') }} */
    AND status IN (/* DREMIO_SUBQUERY: SELECT DISTINCT status FROM {{ ref('status_lookup') }} WHERE is_active = true */)
```

### Single Value Substitution

For single values, the result is substituted directly. Useful for date ranges, thresholds, or lookup values:

```sql
-- Get the latest partition date
WHERE partition_date = /* DREMIO_SUBQUERY: SELECT MAX(partition_date) FROM {{ this }} */

-- Use a threshold value from a config table
WHERE amount > /* DREMIO_SUBQUERY: SELECT threshold_value FROM {{ ref('config_table') }} WHERE config_key = 'min_amount' */

-- Get a specific date from another table
WHERE event_date >= /* DREMIO_SUBQUERY: SELECT cutoff_date FROM {{ ref('date_config') }} WHERE environment = 'production' */
```

### Multiple Values for IN() Clauses

When the subquery returns multiple rows, they are automatically formatted for `IN()` clauses:

```sql
-- Filter by multiple status values
WHERE status IN (/* DREMIO_SUBQUERY: SELECT DISTINCT status FROM {{ ref('valid_statuses') }} WHERE is_active = true */)

-- Filter by multiple IDs
WHERE customer_id IN (/* DREMIO_SUBQUERY: SELECT customer_id FROM {{ ref('premium_customers') }} */)

-- Filter by multiple date partitions
WHERE DATE_TRUNC('month', transaction_date) IN (
    /* DREMIO_SUBQUERY: 
       SELECT DISTINCT DATE_TRUNC('month', transaction_date) 
       FROM {{ ref('recent_transactions') }}
       WHERE transaction_date >= CURRENT_DATE - INTERVAL '3' MONTH
    */
)
```

### Using in JOIN Conditions

You can use `DREMIO_SUBQUERY` in JOIN conditions:

```sql
SELECT 
    t.*,
    d.description
FROM {{ ref('transactions') }} t
LEFT JOIN {{ ref('descriptions') }} d
    ON d.category_id = t.category_id
    AND d.region IN (/* DREMIO_SUBQUERY: SELECT DISTINCT region FROM {{ ref('active_regions') }} */)
```

### Using in CASE Statements

Use `DREMIO_SUBQUERY` to get dynamic values in CASE statements:

```sql
SELECT 
    id,
    amount,
    CASE 
        WHEN amount > /* DREMIO_SUBQUERY: SELECT high_value_threshold FROM {{ ref('config') }} */ 
        THEN 'High'
        WHEN amount > /* DREMIO_SUBQUERY: SELECT medium_value_threshold FROM {{ ref('config') }} */
        THEN 'Medium'
        ELSE 'Low'
    END AS value_category
FROM {{ ref('transactions') }}
```

### Combining Multiple DREMIO_SUBQUERY Calls

You can use multiple `DREMIO_SUBQUERY` calls in the same query:

```sql
SELECT *
FROM {{ ref('events') }}
WHERE 
    event_date BETWEEN 
        /* DREMIO_SUBQUERY: SELECT MIN(event_date) FROM {{ ref('date_range') }} */ 
        AND 
        /* DREMIO_SUBQUERY: SELECT MAX(event_date) FROM {{ ref('date_range') }} */
    AND region IN (/* DREMIO_SUBQUERY: SELECT region FROM {{ ref('target_regions') }} */)
    AND status IN (/* DREMIO_SUBQUERY: SELECT status FROM {{ ref('valid_statuses') }} */)
```

### Using Jinja References

Jinja templating is processed before `DREMIO_SUBQUERY` substitution, so you can use dbt references and macros:

```sql
-- Reference the current model
WHERE last_updated > /* DREMIO_SUBQUERY: SELECT MAX(last_updated) FROM {{ this }} */

-- Use ref() macro
WHERE customer_id IN (/* DREMIO_SUBQUERY: SELECT customer_id FROM {{ ref('customer_list') }} */)

-- Use variables
WHERE environment = /* DREMIO_SUBQUERY: SELECT env_name FROM {{ ref('env_config') }} WHERE env_id = {{ var('target_env_id') }} */
```

### Using in Incremental Predicates

Use `DREMIO_SUBQUERY` in incremental predicates to dynamically filter based on existing data. This is particularly useful for Iceberg table partition pruning:

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['id'],
    incremental_predicates=[
        "DATE_TRUNC('day', DBT_INTERNAL_DEST.event_date) IN (/* DREMIO_SUBQUERY: SELECT DISTINCT DATE_TRUNC('day', event_date) FROM DBT_INTERNAL_SOURCE */)"
    ]
) }}

SELECT * FROM {{ ref('source_table') }}
```

## MERGE Statement Context

When used in incremental merge statements, `DREMIO_SUBQUERY` automatically resolves the `DBT_INTERNAL_SOURCE` and `DBT_INTERNAL_DEST` aliases to their actual table names:

- `DBT_INTERNAL_SOURCE` → The intermediate/temp table with new data
- `DBT_INTERNAL_DEST` → The destination table with existing data

This allows you to reference these tables in your subqueries even though they are aliases in the MERGE statement.

## Type-Aware Formatting

The feature automatically formats results based on their SQL data types:

- **Numbers** (INTEGER, BIGINT, DECIMAL, FLOAT, etc.): Unquoted
- **Strings** (VARCHAR, CHAR, TEXT): Quoted with proper escaping
- **Dates/Timestamps**: Quoted as strings
- **NULL values**: Returned as literal `NULL`
- **Booleans**: Unquoted uppercase (TRUE/FALSE)

## Empty Result Handling

If a subquery returns no rows, `DREMIO_SUBQUERY` will return `NULL`. This is useful when there are no new records to process:

```sql
-- If subquery returns no rows, this becomes:
WHERE date IN (NULL)
```

## Best Practices

1. **Use in Incremental Predicates**: This feature is most useful for incremental materializations where you need dynamic partition pruning.

2. **Query the Right Table**: 
   - Use `DBT_INTERNAL_SOURCE` to query the temp table with new data
   - Use `DBT_INTERNAL_DEST` to query the destination table with existing data

3. **Single Column**: Ensure your subquery returns only one column. If multiple columns are returned, only the first column will be used (with a warning logged).

4. **Performance**: The subquery is executed before the main query, so keep subqueries simple and efficient.

5. **Error Handling**: If a subquery fails, the entire query will fail with a clear error message indicating which `DREMIO_SUBQUERY` failed.

## Limitations

- Subqueries cannot contain nested `DREMIO_SUBQUERY` patterns (recursion is prevented)
- Only the first column of multi-column results is used
- The feature is specific to dbt-dremio and will be ignored by other adapters

## Debugging

To see the final SQL after substitution, run dbt with the `--debug` flag:

```bash
dbt run --select your_model --debug
```

Look for log messages containing "DREMIO_SUBQUERY" to see:
- Pattern detection
- MERGE context extraction
- Subquery execution
- Final substituted SQL

## Complete Examples

### Example 1: Filtering by Dynamic Date Range

Use `DREMIO_SUBQUERY` to get date ranges from a control table:

```sql
SELECT 
    transaction_id,
    customer_id,
    amount,
    transaction_date
FROM {{ ref('transactions') }}
WHERE 
    transaction_date >= /* DREMIO_SUBQUERY: SELECT start_date FROM {{ ref('date_control') }} WHERE process_name = 'daily_processing' */
    AND transaction_date <= /* DREMIO_SUBQUERY: SELECT end_date FROM {{ ref('date_control') }} WHERE process_name = 'daily_processing' */
```

### Example 2: Multi-Table Filtering

Combine multiple `DREMIO_SUBQUERY` calls to filter from different sources:

```sql
SELECT 
    o.order_id,
    o.customer_id,
    o.order_date,
    o.total_amount
FROM {{ ref('orders') }} o
WHERE 
    o.order_date >= /* DREMIO_SUBQUERY: SELECT MAX(processed_date) FROM {{ ref('processing_log') }} WHERE table_name = 'orders' */
    AND o.customer_id IN (/* DREMIO_SUBQUERY: SELECT customer_id FROM {{ ref('active_customers') }} WHERE status = 'active' */)
    AND o.region IN (/* DREMIO_SUBQUERY: SELECT region_code FROM {{ ref('target_regions') }} */)
```

### Example 3: Conditional Processing Based on Config

Use `DREMIO_SUBQUERY` to get configuration values:

```sql
SELECT 
    id,
    name,
    value,
    CASE 
        WHEN value > /* DREMIO_SUBQUERY: SELECT high_threshold FROM {{ ref('threshold_config') }} WHERE metric_type = 'value' */
        THEN 'High'
        ELSE 'Normal'
    END AS value_category
FROM {{ ref('metrics') }}
WHERE 
    metric_date >= /* DREMIO_SUBQUERY: SELECT processing_start_date FROM {{ ref('processing_config') }} */
```

### Example 4: Incremental Model with Partition Pruning

This example shows how to use `DREMIO_SUBQUERY` in incremental predicates for efficient partition pruning:

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['session_id', 'event_type'],
    on_schema_change='sync_all_columns',
    partition_by=['DAY(event_timestamp)'],
    incremental_predicates=[
        "DATE_TRUNC('day', DBT_INTERNAL_DEST.event_timestamp) IN (/* DREMIO_SUBQUERY: SELECT DISTINCT DATE_TRUNC('day', event_timestamp) FROM DBT_INTERNAL_SOURCE */)"
    ]
) }}

SELECT
    session_id,
    event_type,
    event_timestamp,
    user_id,
    event_data
FROM {{ ref('raw_events') }}
```

This will automatically filter the merge operation to only process rows where the date exists in the new data, enabling efficient partition pruning on Iceberg tables.

