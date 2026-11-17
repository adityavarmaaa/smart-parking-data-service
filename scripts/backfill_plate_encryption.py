
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)

from sqlalchemy import text
from app.database.connection import engine
from app.security import encrypt_text, decrypt_text, plate_hmac

TABLES = [
    {
        "name": "vehicles",
        "keys": ["id"],
        "fields": [
            ("plate_number", "plate_number_ciphertext", "plate_number_hmac"),
        ],
    },
    {
        "name": "parking_events",
        "keys": ["id"],
        "fields": [
            ("plate_number", "plate_number_ciphertext", "plate_number_hmac"),
            (
                "observed_plate_number",
                "observed_plate_number_ciphertext",
                "observed_plate_number_hmac",
            ),
        ],
    },
    {
        "name": "parking_sessions",
        "keys": ["id"],
        "fields": [
            ("plate_number", "plate_number_ciphertext", "plate_number_hmac"),
        ],
    },
    {
        "name": "parking_slot_state",
        "keys": ["camera_id", "slot_id"],
        "fields": [
            ("plate_number", "plate_number_ciphertext", "plate_number_hmac"),
        ],
    },
]


def run_backfill(apply_changes=False):
    total_values = 0
    total_rows = 0

    with engine.begin() as conn:
        for table_info in TABLES:
            table = table_info["name"]
            keys = table_info["keys"]
            fields = table_info["fields"]

            columns = list(keys)
            for plain, cipher, fingerprint in fields:
                columns.extend([plain, cipher, fingerprint])

            columns = list(dict.fromkeys(columns))

            select_sql = text(
                f"SELECT {', '.join(columns)} FROM public.{table}"
            )
            rows = conn.execute(select_sql).mappings().all()

            table_values = 0
            table_rows = 0

            for row in rows:
                updates = {}
                row_has_update = False

                for plain, cipher, fingerprint in fields:
                    original = row[plain]

                    if original is None or not str(original).strip():
                        continue

                    total_values += 1
                    table_values += 1

                    expected_hmac = plate_hmac(original)
                    existing_cipher = row[cipher]
                    existing_hmac = row[fingerprint]

                    if existing_cipher:
                        if decrypt_text(existing_cipher) != original:
                            raise RuntimeError(
                                f"Encryption verification failed in {table}.{cipher}"
                            )

                    if existing_hmac:
                        if existing_hmac != expected_hmac:
                            raise RuntimeError(
                                f"HMAC verification failed in {table}.{fingerprint}"
                            )

                    if not existing_cipher:
                        updates[cipher] = encrypt_text(original)
                        row_has_update = True

                    if not existing_hmac:
                        updates[fingerprint] = expected_hmac
                        row_has_update = True

                if row_has_update:
                    table_rows += 1

                    if apply_changes:
                        where_parts = [
                            f"{key} = :key_{key}" for key in keys
                        ]
                        params = {
                            f"key_{key}": row[key] for key in keys
                        }

                        set_parts = []
                        for column, value in updates.items():
                            set_parts.append(f"{column} = :{column}")
                            params[column] = value

                        update_sql = text(
                            f"UPDATE public.{table} "
                            f"SET {', '.join(set_parts)} "
                            f"WHERE {' AND '.join(where_parts)}"
                        )

                        conn.execute(update_sql, params)

            total_rows += table_rows

            print(
                f"{table}: plaintext values checked={table_values}, "
                f"rows needing updates={table_rows}"
            )

        print()
        print("Total plaintext values checked:", total_values)
        print("Rows needing updates:", total_rows)

        if not apply_changes:
            print("DRY RUN ONLY — no database changes made.")
        else:
            print("Backfill updates applied inside transaction.")

            for table_info in TABLES:
                table = table_info["name"]
                fields = table_info["fields"]
                keys = table_info["keys"]

                columns = list(keys)
                for plain, cipher, fingerprint in fields:
                    columns.extend([plain, cipher, fingerprint])

                columns = list(dict.fromkeys(columns))
                rows = conn.execute(
                    text(
                        f"SELECT {', '.join(columns)} "
                        f"FROM public.{table}"
                    )
                ).mappings().all()

                for row in rows:
                    for plain, cipher, fingerprint in fields:
                        original = row[plain]

                        if original is None or not str(original).strip():
                            continue

                        if not row[cipher] or not row[fingerprint]:
                            raise RuntimeError(
                                f"Missing encrypted data after backfill: {table}"
                            )

                        if decrypt_text(row[cipher]) != original:
                            raise RuntimeError(
                                f"Decryption verification failed: {table}"
                            )

                        if row[fingerprint] != plate_hmac(original):
                            raise RuntimeError(
                                f"HMAC verification failed: {table}"
                            )

            print("POST-BACKFILL VERIFICATION: PASS")
            print("Original plaintext columns were not cleared.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply encryption backfill to the database",
    )
    args = parser.parse_args()

    run_backfill(apply_changes=args.apply)
