import os
import json
import sqlite3
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# Helper to load .env without external dependencies
def load_dotenv():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DB_PATH = os.getenv("OUTPUT_DB_PATH", os.path.join(BASE_DIR, "google_maps_raw.db"))
IMAGES_DIR = os.getenv("IMAGES_DIR", os.path.join(BASE_DIR, "downloaded_images"))
MAX_WORKERS = int(os.getenv("MAX_IMAGE_WORKERS", "25"))

def clean_high_res_url(url):
    if not url:
        return ""
    if "=" in url:
        return url.rsplit("=", 1)[0] + "=s0"
    return url + "=s0"

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
            # Fallback to standard url without =s0 if =s0 is rejected for some rare images
            base_url = url.rsplit("=", 1)[0] + "=w1600-h1200"
            r2 = session.get(base_url, headers=headers, timeout=20)
            if r2.status_code == 200:
                with open(save_path, "wb") as f:
                    f.write(r2.content)
                return True, "downloaded_fallback"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)

def download_all_images(cafe_id_filter=None):
    if not os.path.exists(OUTPUT_DB_PATH):
        print(f"Error: Output database not found at '{OUTPUT_DB_PATH}'. Please run the crawler first.")
        return

    if not os.path.exists(IMAGES_DIR):
        os.makedirs(IMAGES_DIR, exist_ok=True)

    conn = sqlite3.connect(OUTPUT_DB_PATH)
    cur = conn.cursor()

    if cafe_id_filter:
        query = "SELECT cafe_id, original_name, image_urls, high_res_image_urls FROM google_places_raw WHERE cafe_id = ? AND status = 'success'"
        rows = cur.execute(query, (cafe_id_filter,)).fetchall()
    else:
        query = "SELECT cafe_id, original_name, image_urls, high_res_image_urls FROM google_places_raw WHERE status = 'success'"
        rows = cur.execute(query).fetchall()
    conn.close()

    tasks = []
    total_images = 0

    for cid, name, raw_urls_json, high_res_json in rows:
        urls_json = high_res_json or raw_urls_json
        if not urls_json:
            continue
        try:
            urls = json.loads(urls_json)
        except Exception:
            continue

        cafe_dir = os.path.join(IMAGES_DIR, str(cid))
        os.makedirs(cafe_dir, exist_ok=True)

        for idx, u in enumerate(urls, 1):
            if not u:
                continue
            clean_url = clean_high_res_url(u)
            save_path = os.path.join(cafe_dir, f"photo_{idx}.jpg")
            tasks.append((clean_url, save_path, cid, idx))
            total_images += 1

    print(f"Total images queue size: {total_images}")
    if total_images == 0:
        print("No images found to download.")
        return

    print(f"Starting downloads with {MAX_WORKERS} workers...")
    completed = 0
    success = 0
    already_done = 0

    # Thread-local session for reuse
    session = requests.Session()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_task = {
            executor.submit(download_single_image, session, t[0], t[1]): t for t in tasks
        }
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            try:
                ok, msg = future.result()
                if ok:
                    success += 1
                    if msg == "already_exists":
                        already_done += 1
                completed += 1
                if completed % 100 == 0 or completed == total_images:
                    print(f"Progress: {completed}/{total_images} (Downloaded: {success}, Existing: {already_done})")
            except Exception as e:
                pass

    print(f"\nImage downloading complete! {success}/{total_images} images successfully stored in '{IMAGES_DIR}'.")

if __name__ == "__main__":
    import sys
    filter_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    download_all_images(cafe_id_filter=filter_id)
