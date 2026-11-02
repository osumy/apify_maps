"""
High-Resolution Photo Downloader for Google Maps
=================================================
Downloads uncompressed camera-master WebP (=s0-rw) photos directly from place_images table.
Supports filtering by owner photos, tracking download progress in database, multi-threading,
and frequent periodic database commits for reliable resumability on Google Colab / Kaggle.
"""

import os
import sys
import glob
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
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DB_PATH = os.getenv("OUTPUT_DB_PATH", os.path.join(DATA_DIR, "google_maps.db"))
IMAGES_DIR = os.getenv("IMAGES_DIR", os.path.join(DATA_DIR, "downloaded_images"))
MAX_WORKERS = int(os.getenv("MAX_IMAGE_WORKERS", "25"))
DEFAULT_ONLY_OWNER = os.getenv("DOWNLOAD_ONLY_OWNER", "true").lower() in ("true", "1", "yes")

def resolve_data_path(file_path):
    """
    Smartly resolves a file path.
    If given a directory, discovers database files inside.
    Bare filenames without directories fall back to data/ or BASE_DIR.
    """
    if not file_path:
        return file_path
    if os.path.isdir(file_path):
        cands = (
            glob.glob(os.path.join(file_path, "**/*google_maps*.db"), recursive=True) +
            glob.glob(os.path.join(file_path, "**/*.db"), recursive=True) +
            glob.glob(os.path.join(file_path, "**/*.sqlite*"), recursive=True)
        )
        if cands:
            return cands[0]
        return file_path
    if os.path.exists(file_path):
        return file_path
    parent_dir = os.path.dirname(file_path)
    if not parent_dir:
        data_cand = os.path.join(DATA_DIR, os.path.basename(file_path))
        if os.path.exists(data_cand):
            return data_cand
        root_cand = os.path.join(BASE_DIR, os.path.basename(file_path))
        if os.path.exists(root_cand):
            return root_cand
    return file_path

def get_db_connection(db_path, timeout=60.0, read_only=False):
    """
    Returns an optimized sqlite3 connection.
    Configures busy timeout and safe journal mode for both local SSDs and network/FUSE drives (e.g. Google Drive).
    Handles read-only filesystems and directory paths gracefully.
    """
    db_path = resolve_data_path(db_path)
    if os.path.isdir(db_path):
        cands = (
            glob.glob(os.path.join(db_path, "**/*google_maps*.db"), recursive=True) +
            glob.glob(os.path.join(db_path, "**/*.db"), recursive=True) +
            glob.glob(os.path.join(db_path, "**/*.sqlite*"), recursive=True)
        )
        if cands:
            db_path = cands[0]

    is_kaggle_input = "/kaggle/input" in db_path.replace("\\", "/")
    is_ro = read_only or is_kaggle_input

    if is_ro:
        abs_path = os.path.abspath(db_path).replace("\\", "/")
        uri = f"file:{abs_path}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=timeout)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout = 60000;")
                conn.execute("PRAGMA synchronous = NORMAL;")
            except Exception:
                pass
            return conn
        except Exception:
            pass

    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 60000;")
        is_fuse_or_drive = "drive" in db_path.lower() or "/content/drive" in db_path
        if is_fuse_or_drive:
            conn.execute("PRAGMA journal_mode = TRUNCATE;")
        else:
            conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
    except Exception:
        pass
    return conn

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
            # Fallback to standard high-res url without =s0-rw if =s0-rw is rejected for rare images
            base_url = url.rsplit("=", 1)[0] + "=w1600-h1200-rw"
            r2 = session.get(base_url, headers=headers, timeout=20)
            if r2.status_code == 200:
                with open(save_path, "wb") as f:
                    f.write(r2.content)
                return True, "downloaded_fallback"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)

def _commit_download_records(output_db_path, records):
    """Helper to commit a batch of downloaded image records into SQLite."""
    if not records:
        return
    try:
        conn = get_db_connection(output_db_path)
        cur = conn.cursor()
        cur.executemany('''
            UPDATE place_images 
            SET download_status = 'downloaded', local_path = ?, downloaded_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', records)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Warning: Failed to update download status in database: {e}")

def download_images(
    output_db_path=None,
    images_dir=None,
    cafe_id_filter=None,
    only_owner=None,
    limit=None,
    max_workers=None,
    batch_commit_size=100,
    progress_bar=True
):
    """
    Downloads images with concurrent workers, incremental batch database commits,
    and progress bar tracking.
    """
    output_db_path = resolve_data_path(output_db_path or os.getenv("OUTPUT_DB_PATH", OUTPUT_DB_PATH))
    images_dir = images_dir or os.getenv("IMAGES_DIR", IMAGES_DIR)
    if not os.path.isabs(images_dir) and not images_dir.startswith("data"):
        data_images_cand = os.path.join(DATA_DIR, os.path.basename(images_dir))
        if os.path.exists(data_images_cand) or not os.path.exists(images_dir):
            images_dir = data_images_cand
    max_workers = int(max_workers or os.getenv("MAX_IMAGE_WORKERS", MAX_WORKERS))
    only_owner = DEFAULT_ONLY_OWNER if only_owner is None else bool(only_owner)

    if not os.path.exists(output_db_path):
        print(f"Error: Database not found at '{output_db_path}'. Please run crawler or migration first.")
        return {
            "total": 0,
            "success": 0,
            "already_done": 0,
            "failed": 0,
            "images_dir": images_dir,
            "error": "database_not_found"
        }

    os.makedirs(images_dir, exist_ok=True)

    conn = get_db_connection(output_db_path)
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
    if limit is not None:
        query += f" LIMIT {int(limit)}"

    rows = cur.execute(query, params).fetchall()
    conn.close()

    total_images = len(rows)
    print(f"Found {total_images} images to download (Filters: only_owner={only_owner}, cafe_id={cafe_id_filter})")
    if total_images == 0:
        print("No pending images found.")
        return {
            "total": 0,
            "success": 0,
            "already_done": 0,
            "failed": 0,
            "images_dir": images_dir,
            "status": "up_to_date"
        }

    tasks = []
    for img_id, cid, src_type, high_res, raw_url in rows:
        url = clean_high_res_url(high_res) if high_res else clean_high_res_url(raw_url)
        if not url:
            continue
        cafe_dir = os.path.join(images_dir, str(cid))
        os.makedirs(cafe_dir, exist_ok=True)
        filename = f"{src_type}_{img_id}.webp"
        save_path = os.path.join(cafe_dir, filename)
        tasks.append((img_id, cid, url, save_path))

    print(f"Starting downloads with {max_workers} workers into '{images_dir}'...")
    completed = 0
    success = 0
    already_done = 0
    pending_commits = []

    session = requests.Session()

    pbar = None
    if progress_bar:
        try:
            from tqdm.auto import tqdm
            pbar = tqdm(total=len(tasks), desc="Downloading Photos", unit="img")
        except ImportError:
            pbar = None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
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
                    rel_save_path = os.path.relpath(save_path, BASE_DIR).replace("\\", "/")
                    pending_commits.append((rel_save_path, img_id))
            except Exception:
                pass

            completed += 1

            if pbar:
                pbar.update(1)
                pbar.set_postfix({"ok": success, "exists": already_done})
            elif completed % 100 == 0 or completed == len(tasks):
                print(f"Progress: {completed}/{len(tasks)} (Success: {success}, Existing: {already_done})")

            # Batch commit to database periodically to prevent data loss on unexpected disconnects
            if len(pending_commits) >= batch_commit_size:
                _commit_download_records(output_db_path, pending_commits)
                pending_commits = []

    if pbar:
        pbar.close()

    # Commit any remaining uncommitted records
    if pending_commits:
        _commit_download_records(output_db_path, pending_commits)
        pending_commits = []

    print(f"\nDownload finished! {success}/{len(tasks)} stored in '{images_dir}'. DB statuses updated.")
    return {
        "total": len(tasks),
        "success": success,
        "already_done": already_done,
        "failed": len(tasks) - success,
        "images_dir": images_dir,
        "status": "completed"
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="High-Resolution Photo Downloader for Google Maps")
    parser.add_argument("cafe_id", nargs="?", type=int, default=None, help="Specific cafe ID to download photos for")
    parser.add_argument("--db", type=str, default=None, help="Path to google_maps.db")
    parser.add_argument("--images-dir", type=str, default=None, help="Directory to save downloaded photos")
    parser.add_argument("--limit", type=int, default=None, help="Max images to download")
    parser.add_argument("--workers", type=int, default=None, help="Number of concurrent download threads")
    parser.add_argument("--all-photos", action="store_true", help="Download all photos including visitor reviews")
    parser.add_argument("--batch-commit", type=int, default=100, help="DB commit batch size")

    args = parser.parse_args()
    download_images(
        output_db_path=args.db,
        images_dir=args.images_dir,
        cafe_id_filter=args.cafe_id,
        only_owner=not args.all_photos if args.all_photos else None,
        limit=args.limit,
        max_workers=args.workers,
        batch_commit_size=args.batch_commit
    )
