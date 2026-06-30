import sqlite3
import os

db_path = 'data/forge.db'
try:
    c = sqlite3.connect(db_path)
    
    # Get table names
    tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print(f"Tables in database: {tables}")
    
    # Try to get trade count
    try:
        t = c.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
        print(f'{t} trades recorded')
        
        # Get all trades
        trades = c.execute('SELECT * FROM trades ORDER BY timestamp').fetchall()
        print(f"All trades:")
        for trade in trades:
            print(f"  {trade}")
    except Exception as e:
        print(f"Error querying trades: {e}")
    
    c.close()
except Exception as e:
    print(f"Error: {e}")
