import os
import sys
import tempfile
import sqlite3
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from run_crawler import resolve_data_path, get_db_connection, get_pending_cafes, init_output_db

class TestCrawlerPaths(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.source_dir = os.path.join(self.temp_dir, "kaggle_dataset_dir")
        os.makedirs(self.source_dir, exist_ok=True)
        self.source_db = os.path.join(self.source_dir, "cafes_cafe_shard00.db")
        
        # Create dummy source database
        conn = sqlite3.connect(self.source_db)
        conn.execute("CREATE TABLE cafes_cafe (id INT, name TEXT, latitude REAL, longitude REAL, city TEXT, address TEXT)")
        conn.execute("INSERT INTO cafes_cafe VALUES (101, 'Test Cafe Tehran', 35.72, 51.41, 'Tehran', 'Valiasr St')")
        conn.commit()
        conn.close()

        self.output_db = os.path.join(self.temp_dir, "google_maps.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_resolve_data_path_with_directory(self):
        resolved = resolve_data_path(self.source_dir)
        self.assertTrue(resolved.endswith(".db"))
        self.assertTrue(os.path.isfile(resolved))
        self.assertEqual(os.path.abspath(resolved), os.path.abspath(self.source_db))

    def test_get_db_connection_with_directory(self):
        conn = get_db_connection(self.source_dir, read_only=True)
        row = conn.execute("SELECT * FROM cafes_cafe WHERE id=101").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Test Cafe Tehran")
        conn.close()

    def test_get_pending_cafes_with_directory_source(self):
        # Passing directory path directly to get_pending_cafes should automatically resolve the db file inside
        pending = get_pending_cafes(source_db_path=self.source_dir, output_db_path=self.output_db)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["cafe_id"], 101)
        self.assertEqual(pending[0]["name"], "Test Cafe Tehran")

if __name__ == "__main__":
    unittest.main()
