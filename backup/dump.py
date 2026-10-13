"""Makes a full copy of the Agenndar database as one SQL file.

GitHub runs this every night (.github/workflows/backup.yml). The file it
writes can rebuild the whole database; backup/README.md shows how. It uses
a read-only Turso token, so making a backup can never change any data.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from turso_http import TursoHTTPConnection  # noqa: E402

PAGE = 500  # rows fetched per request, so big tables don't time out


def sql_value(value):
    """One value written the way SQL expects it."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return "X'" + bytes(value).hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def copy_rows(conn, out, table):
    """Writes an INSERT line for every row of one table; returns the count."""
    count = 0
    while True:
        rows = conn.execute(
            'SELECT * FROM "%s" ORDER BY rowid LIMIT %d OFFSET %d'
            % (table, PAGE, count)).fetchall()
        for row in rows:
            values = ",".join(sql_value(v) for v in row)
            out.write('INSERT INTO "%s" VALUES(%s);\n' % (table, values))
        count += len(rows)
        if len(rows) < PAGE:
            return count


def write_dump(conn, out):
    """Writes every table's design and rows to the open file `out`, then
    the indexes. Returns {table name: number of rows}."""
    objects = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE 'libsql_%' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 "
        "ELSE 2 END, name").fetchall()
    counts = {}
    out.write("PRAGMA foreign_keys=OFF;\nBEGIN TRANSACTION;\n")
    for kind, name, sql in objects:
        out.write(sql + ";\n")
        if kind == "table":
            counts[name] = copy_rows(conn, out, name)
    # The counters behind AUTOINCREMENT ids, so new ids carry on from the
    # same place and a deleted task's id is never handed out again.
    if any(sql and "AUTOINCREMENT" in sql.upper() for _, _, sql in objects):
        out.write("DELETE FROM sqlite_sequence;\n")
        copy_rows(conn, out, "sqlite_sequence")
    out.write("COMMIT;\n")
    return counts


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "agenndar-backup.sql"
    conn = TursoHTTPConnection(os.environ["TURSO_DATABASE_URL"],
                               os.environ["TURSO_BACKUP_TOKEN"])
    with open(path, "w", encoding="utf-8") as out:
        counts = write_dump(conn, out)
    if "users" not in counts:
        sys.exit("No users table found - this backup doesn't look right.")
    print("Backup written: %d tables, %d rows"
          % (len(counts), sum(counts.values())))


if __name__ == "__main__":
    main()
