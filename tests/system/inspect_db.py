from __future__ import annotations

import json
import sqlite3


def decoded(value: str) -> object:
    return json.loads(value)


connection = sqlite3.connect("file:/state/system.db?mode=ro", uri=True, timeout=5)
connection.row_factory = sqlite3.Row
result = {
    "entries": [
        {
            "id": row["id"],
            "kind": row["kind"],
            "data": decoded(row["data_json"]),
            "created_revision": row["created_revision"],
            "updated_revision": row["updated_revision"],
        }
        for row in connection.execute(
            "SELECT id,kind,data_json,created_revision,updated_revision "
            "FROM preference_entries ORDER BY id"
        )
    ],
    "revisions": [
        {
            "revision": row["revision"],
            "original_message": row["original_message"],
            "operations": decoded(row["operations_json"]),
            "summary": row["summary"],
        }
        for row in connection.execute(
            "SELECT revision,original_message,operations_json,summary "
            "FROM preference_revisions ORDER BY revision"
        )
    ],
    "offset": int(
        (
            connection.execute(
                "SELECT value FROM preference_meta WHERE name='telegram_offset'"
            ).fetchone()
            or {"value": 0}
        )["value"]
    ),
    "processed": [
        dict(row)
        for row in connection.execute(
            "SELECT update_id,outcome FROM telegram_processed_updates ORDER BY update_id"
        )
    ],
    "commands": [
        dict(row)
        for row in connection.execute(
            "SELECT update_id,outcome FROM preference_command_log ORDER BY id"
        )
    ],
    "deliveries": [
        dict(row)
        for row in connection.execute(
            "SELECT source,native_id FROM deliveries ORDER BY source,native_id"
        )
    ],
    "decisions": [
        dict(row)
        for row in connection.execute(
            "SELECT source,native_id,decision,stage,reason FROM decisions ORDER BY id"
        )
    ],
}
connection.close()
print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
