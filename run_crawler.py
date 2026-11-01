import os
import re
import sys
import json
import time
import sqlite3
import urllib.parse
from apify_client import ApifyClient
from key_manager import ApifyKeyManager, AllKeysExhaustedError

# Helper to load .env without external dependencies
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

# Key Manager & Pool Initialization
KEY_ROTATION_STRATEGY = os.getenv("KEY_ROTATION_STRATEGY", "sequential")
key_manager = ApifyKeyManager(strategy=KEY_ROTATION_STRATEGY)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_DB_PATH = os.getenv("SOURCE_DB_PATH", r"c:\Users\amira\Desktop\crawler\cafes_cafe_shard00.db")
OUTPUT_DB_PATH = os.getenv("OUTPUT_DB_PATH", os.path.join(BASE_DIR, "google_maps_raw.db"))

# Scraper Settings
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))
MAX_IMAGES_PER_PLACE = int(os.getenv("MAX_IMAGES_PER_PLACE", "50"))
MAX_REVIEWS_PER_PLACE = int(os.getenv("MAX_REVIEWS_PER_PLACE", "15"))
ZOOM_LEVEL = int(os.getenv("ZOOM_LEVEL", "17"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))

def init_output_db():
    conn = sqlite3.connect(OUTPUT_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS google_places_raw (
            cafe_id INTEGER PRIMARY KEY,
            original_name TEXT,
            searched_lat REAL,
            searched_lng REAL,
            search_url TEXT,
            place_id TEXT,
            title TEXT,
            category TEXT,
            address TEXT,
            phone TEXT,
            rating REAL,
            reviews_count INTEGER,
            images_count INTEGER,
            image_urls TEXT,
            high_res_image_urls TEXT,
            scraped_reviews TEXT,
            raw_data TEXT,
            status TEXT,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def build_search_url(name, lat, lng, zoom=ZOOM_LEVEL):
    clean_name = name.strip()
    encoded_name = urllib.parse.quote_plus(clean_name)
    return f"https://www.google.com/maps/search/{encoded_name}/@{lat},{lng},{zoom}z"

def to_high_res_url(img_url):
    """
    Replaces Google image sizing modifier (e.g. =w1920-h1080-k-no) with =s0 
    to retrieve the uncompressed, maximum resolution source image.
    """
    if not img_url:
        return ""
    if "=" in img_url:
        return img_url.rsplit("=", 1)[0] + "=s0"
    return img_url + "=s0"

def get_pending_cafes(limit=None):
    init_output_db()
    out_conn = sqlite3.connect(OUTPUT_DB_PATH)
    out_cur = out_conn.cursor()
    # Cafes already processed (either success or not_found)
    existing_ids = set(row[0] for row in out_cur.execute(
        "SELECT cafe_id FROM google_places_raw"
    ).fetchall())
    out_conn.close()

    if not os.path.exists(SOURCE_DB_PATH):
        raise FileNotFoundError(
            f"Source database not found at '{SOURCE_DB_PATH}'. "
            "Please check the path or set SOURCE_DB_PATH in your .env file."
        )

    src_conn = sqlite3.connect(SOURCE_DB_PATH)
    src_conn.row_factory = sqlite3.Row
    src_cur = src_conn.cursor()
    
    rows = src_cur.execute("SELECT id, name, latitude, longitude, city, address FROM cafes_cafe").fetchall()
    src_conn.close()

    pending = []
    for r in rows:
        cid = r["id"]
        if cid in existing_ids:
            continue
        lat = r["latitude"]
        lng = r["longitude"]
        name = r["name"]
        if not lat or not lng:
            continue
        search_url = build_search_url(name, lat, lng)
        pending.append({
            "cafe_id": cid,
            "name": name,
            "latitude": lat,
            "longitude": lng,
            "city": r["city"],
            "search_url": search_url
        })
        if limit and len(pending) >= limit:
            break

    return pending

def save_batch_results(batch_cafes, scraped_items):
    conn = sqlite3.connect(OUTPUT_DB_PATH)
    cur = conn.cursor()

    url_to_cafe = {c["search_url"]: c for c in batch_cafes}
    matched_cafe_ids = set()

    for item in scraped_items:
        raw_search_url = item.get("searchPageUrl", "")
        clean_url = raw_search_url.split("?")[0]
        
        cafe = url_to_cafe.get(clean_url)
        
        # Fallback match by exact coordinate proximity if URL had encoding variation
        if not cafe:
            loc = item.get("location") or {}
            item_lat = loc.get("lat")
            item_lng = loc.get("lng")
            if item_lat and item_lng:
                best_cafe = None
                min_dist = float('inf')
                for c in batch_cafes:
                    if c["cafe_id"] in matched_cafe_ids:
                        continue
                    d = ((c["latitude"] - item_lat)**2 + (c["longitude"] - item_lng)**2)**0.5
                    if d < min_dist:
                        min_dist = d
                        best_cafe = c
                if best_cafe and min_dist < 0.008:
                    cafe = best_cafe

        if not cafe:
            continue

        cid = cafe["cafe_id"]
        matched_cafe_ids.add(cid)

        raw_img_urls = item.get("imageUrls", [])
        high_res_urls = [to_high_res_url(u) for u in raw_img_urls]
        reviews = item.get("reviews", [])

        cur.execute('''
            INSERT OR REPLACE INTO google_places_raw (
                cafe_id, original_name, searched_lat, searched_lng, search_url,
                place_id, title, category, address, phone, rating, reviews_count,
                images_count, image_urls, high_res_image_urls, scraped_reviews,
                raw_data, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            cid,
            cafe["name"],
            cafe["latitude"],
            cafe["longitude"],
            cafe["search_url"],
            item.get("placeId"),
            item.get("title"),
            item.get("categoryName"),
            item.get("address"),
            item.get("phone"),
            item.get("totalScore"),
            item.get("reviewsCount"),
            item.get("imagesCount"),
            json.dumps(raw_img_urls, ensure_ascii=False),
            json.dumps(high_res_urls, ensure_ascii=False),
            json.dumps(reviews, ensure_ascii=False),
            json.dumps(item, ensure_ascii=False),
            "success"
        ))

    # Record unmatched cafes as not_found so we don't re-crawl repeatedly
    for c in batch_cafes:
        cid = c["cafe_id"]
        if cid not in matched_cafe_ids:
            cur.execute('''
                INSERT OR IGNORE INTO google_places_raw (
                    cafe_id, original_name, searched_lat, searched_lng, search_url, status
                ) VALUES (?, ?, ?, ?, ?, ?)
            ''', (cid, c["name"], c["latitude"], c["longitude"], c["search_url"], "not_found"))

    conn.commit()
    conn.close()
    return len(matched_cafe_ids), len(batch_cafes) - len(matched_cafe_ids)

def run_scraper(batch_size=BATCH_SIZE, max_total=None):
    init_output_db()

    # Display Apify Key Pool Status & Balances before starting
    print("\n[Apify Pool Initialization]")
    key_manager.print_summary()

    all_pending = get_pending_cafes(limit=max_total)
    total_count = len(all_pending)
    print(f"Total pending cafes to crawl: {total_count}")

    if total_count == 0:
        print("All cafes in the source database have already been scraped!")
        return

    processed = 0
    total_matched = 0
    total_not_found = 0

    for i in range(0, total_count, batch_size):
        batch = all_pending[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total_count + batch_size - 1) // batch_size
        print(f"\n==========================================")
        print(f"Starting batch {batch_num}/{total_batches} ({len(batch)} cafes: '{batch[0]['name']}' ... '{batch[-1]['name']}')")
        print(f"==========================================")

        start_urls = [{"url": c["search_url"]} for c in batch]
        
        run_input = {
            "startUrls": start_urls,
            "maxCrawledPlacesPerSearch": 1,
            "maxImages": MAX_IMAGES_PER_PLACE,
            "maxReviews": MAX_REVIEWS_PER_PLACE,
            "language": "fa"
        }

        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                def _execute_crawl(client):
                    print(f"Submitting batch {batch_num} to Apify (Attempt {attempt})...")
                    run = client.actor("compass/crawler-google-places").call(run_input=run_input)
                    run_dict = run.model_dump() if hasattr(run, "model_dump") else run
                    dataset_id = run_dict.get("default_dataset_id") if isinstance(run_dict, dict) else getattr(run, "default_dataset_id", None)
                    run_id = run_dict.get("id") if isinstance(run_dict, dict) else getattr(run, "id", None)
                    print(f"Run {run_id} finished. Dataset: {dataset_id}")
                    
                    items = client.dataset(dataset_id).list_items().items
                    return items

                items = key_manager.execute_with_retry(_execute_crawl)
                print(f"Retrieved {len(items)} places from Apify.")

                matched, not_found = save_batch_results(batch, items)
                total_matched += matched
                total_not_found += not_found
                processed += len(batch)
                print(f"Batch {batch_num} saved: {matched} matched, {not_found} not found.")
                print(f"Overall Progress: {processed}/{total_count} processed ({total_matched} success, {total_not_found} not found).")
                success = True
                break
            except AllKeysExhaustedError as e:
                print(f"\n[FATAL] Key Pool Exhaustion: {e}")
                print("All available Apify keys have been exhausted. Stopping crawl gracefully.")
                return
            except Exception as e:
                print(f"Error on batch {batch_num} attempt {attempt}: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(5)
                else:
                    print(f"Failed to process batch {batch_num} after {MAX_RETRIES} attempts.")

        if not success:
            print("Stopping crawl due to consecutive errors.")
            break

    print(f"\nCrawl complete! Processed: {processed}/{total_count} (Total Success: {total_matched}, Not found: {total_not_found})")

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run_scraper(max_total=limit)
