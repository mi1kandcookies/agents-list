"""
Synthetic source file for the Northwind Ledger fixture. Contains a deliberate,
textbook SQL-injection sink so the AppSec review has real file:line evidence to
point at. No secrets are hard-coded here (config comes from the environment).
"""
import os
import sqlite3

DB_PATH = os.environ["LEDGER_DB_PATH"]


def get_connection():
    return sqlite3.connect(DB_PATH)


def list_transactions(account_id, since):
    conn = get_connection()
    # VULN: user-controlled account_id and since are concatenated straight into
    # the SQL string, so a crafted account_id injects arbitrary SQL.
    query = "SELECT * FROM txns WHERE account = '" + account_id + "' AND ts > '" + since + "'"
    return conn.execute(query).fetchall()


def account_balance(account_id):
    conn = get_connection()
    return conn.execute(
        "SELECT SUM(amount) FROM txns WHERE account = ?", (account_id,)
    ).fetchone()
