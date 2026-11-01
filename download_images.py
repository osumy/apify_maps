"""
High-Resolution Photo Downloader for Google Maps
=================================================
Downloads uncompressed camera-master WebP (=s0-rw) photos directly from place_images table.
Supports filtering by owner photos, tracking download progress in database, and multi-threading.
"""

import os
import sys
import sqlite3
import argparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

def load_dotenv():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DB_PATH = os.getenv("OUTPUT_DB_PATH", os.path.join(BASE_DIR, "google_maps.db"))
IMAGES_DIR = os.getenv("IMAGES_DIR", os.path.join(BASE_DIR, "downloaded_images"))
MAX_WORKERS = int(os.getenv("MAX_IMAGE_WORKERS", "25"))
DEFAULT_ONLY_OWNER = os.getenv("DOWNLOAD_ONLY_OWNER", "true").lower() in ("true", "1", "yes")

def clean_high_res_url(url):
    if not url:
        return ""
    if "=" in url:
        return url.rsplit("=", 1)[0] + "=s0-rw"
    return url + "=s0-rw"

def download_single_image(session, url, save_path):
    if os.path.exists(save_path) and os.path.getsize(save_path) > 1024:
        return True, "already_exists"
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = session.get(url, headers=headers, timeout=20)
        if resp.status_code == 200 and len(resp.content) > 100:
            with open(save_path, "wb") as f:
                f.write(resp.content)
            return True, "downloaded"
        elif resp.status_code == 400:
            # Fallback to standard url without =s0-rw if =s0-rw is rejected for rare images
            base_url = url.rsplit("=", 1)[0] + "=w1600-h1200-rw"
            r2 = session.get(base_url, headers=headers, timeout=20)
            if r2.status_code == 200:
                with open(save_path, "wb") as f:
                    f.write(r2.content)
                return True, "downloaded_fallback"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)

def download_images(cafe_id_filter=None, only_owner=DEFAULT_ONLY_OWNER, limit=None):
    if not os.path.exists(OUTPUT_DB_PATH):
        print(f"Error: Database not found at '{OUTPUT_DB_PATH}'. Please run crawler or migration first.")
        return

    os.makedirs(IMAGES_DIR, exist_ok=True)

    conn = sqlite3.connect(OUTPUT_DB_PATH)
    cur = conn.cursor()

    conditions = ["download_status != 'downloaded'"]
    params = []

    if cafe_id_filter:
        conditions.append("cafe_id = ?")
        params.append(cafe_id_filter)

    if only_owner:
        conditions.append("(is_owner_photo = 1 OR source_type = 'cover')")

    where_clause = " WHERE " + " AND ".join(conditions)
    query = f"SELECT id, cafe_id, source_type, high_res_url, image_url FROM place_images {where_clause}"
    if limit:
        query += f" LIMIT {int(limit)}"

    rows = cur.execute(query, params).fetchall()
    conn.close()

    total_images = len(rows)
    print(f"Found {total_images} images to download (Filters: only_owner={only_owner}, cafe_id={cafe_id_filter})")
    if total_images == 0:
        print("No pending images found.")
        return

    tasks = []
    for img_id, cid, src_type, high_res, raw_url in rows:
        url = clean_high_res_url(high_res) if high_res else clean_high_res_url(raw_url)
        if not url:
            continue
        cafe_dir = os.path.join(IMAGES_DIR, str(cid))
        os.makedirs(cafe_dir, exist_ok=True)
        filename = f"{src_type}_{img_id}.webp"
        save_path = os.path.join(cafe_dir, filename)
        tasks.append((img_id, cid, url, save_path))

    print(f"Starting downloads with {MAX_WORKERS} workers...")
    completed = 0
    success = 0
    already_done = 0
    downloaded_records = []

    session = requests.Session()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_task = {
            executor.submit(download_single_image, session, t[2], t[3]): t for t in tasks
        }
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            img_id, cid, url, save_path = task
            try:
                ok, msg = future.result()
                if ok:
                    success += 1
                    if msg == "already_exists":
                        already_done += 1
                    downloaded_records.append((save_path, img_id))
                completed += 1
                if completed % 100 == 0 or completed == len(tasks):
                    print(f"Progress: {completed}/{len(tasks)} (Success: {success}, Existing: {already_done})")
            except Exception:
                pass

    # Batch update download status in database
    if downloaded_records:
        conn = sqlite3.connect(OUTPUT_DB_PATH)
        cur = conn.cursor()
        cur.executemany('''
            UPDATE place_images 
            SET download_status = 'downloaded', local_path = ?, downloaded_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', downloaded_records)
        conn.commit()
        conn.close()

    print(f"\nDownload finished! {success}/{len(tasks)} stored in '{IMAGES_DIR}'. DB statuses updated.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Google Maps High-Res Photo Downloader")
    parser.add_argument("--cafe-id", type=int, help="Filter by specific cafe ID")
    parser.add_argument("--all", action="store_true", help="Download all images including customer review photos")
    parser.add_argument("--limit", type=int, help="Limit number of images to download")
    args = parser.parse_args()

    download_images(
        cafe_id_filter=args.cafe_id,
        only_owner=not args.all,
        limit=args.limit
    )
