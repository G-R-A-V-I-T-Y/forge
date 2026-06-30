import sqlite3
import os

db_path = 'data/forge.db'
try:
    c = sqlite3.connect(db_path)
    
    # Get schema
    schema = c.execute("PRAGMA table_info(trades)").fetchall()
    print("Trades table schema:")
    for col in schema:
        print(f"  {col}")
    
    print()
    
    # Get trade count
    t = c.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
    print(f'{t} trades recorded')
    
    # Get all trades with their columns
    print("\nAll trades:")
    c.row_factory = sqlite3.Row
    trades = c.execute('SELECT * FROM trades ORDER BY rowid').fetchall()
    for trade in trades:
        print(f"  {dict(trade)}")
    
    c.close()
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
