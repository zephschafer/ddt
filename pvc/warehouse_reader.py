"""
Fast warehouse querying via DuckDB.

For catalog: local  — reads Parquet files from warehouse/{namespace}/{table}/data/*.parquet
For catalog: gcp    — downloads Parquet blobs from GCS via google-cloud-storage,
                      registers them as Arrow tables in DuckDB, then rewrites
                      namespace.table references to the registered names.

Returns at most 500 rows per query.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_MAX_ROWS = 500


def _project_config() -> dict:
    import yaml
    from .project import find_project_root
    cfg_file = find_project_root() / "project.yml"
    return yaml.safe_load(cfg_file.read_text()) if cfg_file.exists() else {}


def _catalog() -> str:
    return _project_config().get("catalog", "local")


def _warehouse() -> Path:
    from .project import find_project_root
    return find_project_root() / "warehouse"


def _gcs_bucket() -> str:
    return _project_config().get("gcp", {}).get("warehouse_bucket", "")


def _iter_gcs_tables(bucket_name: str) -> list[tuple[str, str]]:
    """List all namespace/table pairs that have data in the GCS warehouse bucket."""
    from google.cloud import storage as gcs
    client = gcs.Client()
    blobs = client.list_blobs(bucket_name)
    seen: set[tuple[str, str]] = set()
    for blob in blobs:
        parts = blob.name.split("/")
        if len(parts) >= 4 and parts[2] == "data" and parts[3].endswith(".parquet"):
            seen.add((parts[0], parts[1]))
    return sorted(seen)


def _load_gcs_table(bucket_name: str, namespace: str, table: str):
    """Download all Parquet blobs for a GCS table and return a single PyArrow table."""
    import io
    import pyarrow as pa
    import pyarrow.parquet as pq
    from google.cloud import storage as gcs

    client = gcs.Client()
    bucket = client.bucket(bucket_name)
    prefix = f"{namespace}/{table}/data/"
    blobs = [b for b in bucket.list_blobs(prefix=prefix) if b.name.endswith(".parquet")]
    if not blobs:
        return None
    tables = [pq.read_table(io.BytesIO(b.download_as_bytes())) for b in blobs]
    return pa.concat_tables(tables) if len(tables) > 1 else tables[0]


def _gcs_table_key(namespace: str, table: str) -> str:
    """DuckDB-safe registered name for a GCS table."""
    return f"_gcs_{namespace}_{table}"


def list_tables() -> list[dict[str, Any]]:
    """Return all tables in the warehouse with column schemas and row counts."""
    import duckdb

    catalog = _catalog()
    results = []

    if catalog == "gcp":
        bucket = _gcs_bucket()
        if not bucket:
            return results
        conn = duckdb.connect()
        for namespace, table in _iter_gcs_tables(bucket):
            arrow_table = _load_gcs_table(bucket, namespace, table)
            if arrow_table is None:
                continue
            key = _gcs_table_key(namespace, table)
            try:
                conn.register(key, arrow_table)
                row_count = conn.execute(f"SELECT COUNT(*) FROM {key}").fetchone()[0]
                cols = conn.execute(f"DESCRIBE SELECT * FROM {key} LIMIT 0").fetchall()
                columns = [{"name": c[0], "type": c[1]} for c in cols]
            except Exception as e:
                row_count = -1
                columns = [{"error": str(e)}]
            results.append({
                "namespace": namespace,
                "table": table,
                "full_name": f"{namespace}.{table}",
                "row_count": row_count,
                "columns": columns,
            })
        conn.close()
        return results

    # local catalog
    warehouse = _warehouse()
    if not warehouse.exists():
        return results

    for ns_dir in sorted(warehouse.iterdir()):
        if not ns_dir.is_dir():
            continue
        for table_dir in sorted(ns_dir.iterdir()):
            if not table_dir.is_dir():
                continue
            data_dir = table_dir / "data"
            parquet_files = list(data_dir.glob("*.parquet")) if data_dir.exists() else []
            if not parquet_files:
                continue

            glob = str(data_dir / "*.parquet")
            try:
                conn = duckdb.connect()
                info = conn.execute(f"SELECT COUNT(*) as n FROM read_parquet('{glob}')").fetchone()
                row_count = info[0] if info else 0
                cols = conn.execute(f"DESCRIBE SELECT * FROM read_parquet('{glob}') LIMIT 0").fetchall()
                columns = [{"name": c[0], "type": c[1]} for c in cols]
                conn.close()
            except Exception as e:
                row_count = -1
                columns = [{"error": str(e)}]

            results.append({
                "namespace": ns_dir.name,
                "table": table_dir.name,
                "full_name": f"{ns_dir.name}.{table_dir.name}",
                "row_count": row_count,
                "columns": columns,
            })

    return results


def query(sql: str) -> list[dict[str, Any]]:
    """
    Run a SQL query against the warehouse.

    Table references use the form  namespace.table  — e.g.
        SELECT * FROM portland_permits.permits_loader LIMIT 10

    The server rewrites these to DuckDB read_parquet() calls (local) or
    registered Arrow tables (GCS) automatically.
    Returns at most 500 rows.
    """
    import duckdb
    import re

    catalog = _catalog()
    conn = duckdb.connect()
    resolved = sql

    if catalog == "gcp":
        bucket = _gcs_bucket()
        for namespace, table in _iter_gcs_tables(bucket):
            pattern = rf"\b{re.escape(namespace)}\.{re.escape(table)}\b"
            if re.search(pattern, resolved):
                arrow_table = _load_gcs_table(bucket, namespace, table)
                if arrow_table is not None:
                    key = _gcs_table_key(namespace, table)
                    conn.register(key, arrow_table)
                    resolved = re.sub(pattern, key, resolved)
    else:
        warehouse = _warehouse()
        if warehouse.exists():
            for ns_dir in warehouse.iterdir():
                if not ns_dir.is_dir():
                    continue
                for table_dir in ns_dir.iterdir():
                    if not table_dir.is_dir():
                        continue
                    data_dir = table_dir / "data"
                    if not data_dir.exists() or not list(data_dir.glob("*.parquet")):
                        continue
                    pattern = rf"\b{re.escape(ns_dir.name)}\.{re.escape(table_dir.name)}\b"
                    glob = str(data_dir / "*.parquet")
                    resolved = re.sub(pattern, f"read_parquet('{glob}')", resolved)

    if "limit" not in resolved.lower():
        resolved = f"SELECT * FROM ({resolved}) _q LIMIT {_MAX_ROWS}"

    rows = conn.execute(resolved).fetchall()
    cols = [d[0] for d in conn.description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]
