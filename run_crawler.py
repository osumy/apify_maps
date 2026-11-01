import os
import re
import sys
import json
import time
import sqlite3
import urllib.parse
from difflib import SequenceMatcher
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_DB_PATH = os.getenv("SOURCE_DB_PATH", r"c:\Users\amira\Desktop\crawler\cafes_cafe_shard00.db")
OUTPUT_DB_PATH = os.getenv("OUTPUT_DB_PATH", os.path.join(BASE_DIR, "google_maps.db"))
RAW_JSONL_PATH = os.getenv("RAW_JSONL_PATH", os.path.join(BASE_DIR, "google_maps_raw.jsonl"))

# Scraper Settings
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))
MAX_IMAGES_PER_PLACE = int(os.getenv("MAX_IMAGES_PER_PLACE", "50"))
MAX_REVIEWS_PER_PLACE = int(os.getenv("MAX_REVIEWS_PER_PLACE", "15"))
ZOOM_LEVEL = int(os.getenv("ZOOM_LEVEL", "17"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
SCRAPE_IMAGE_AUTHORS = os.getenv("SCRAPE_IMAGE_AUTHORS", "true").lower() in ("true", "1", "yes")

_default_key_manager = None

def get_default_key_manager(strategy=None, tokens=None):
    """Returns or lazily initializes the default ApifyKeyManager."""
    global _default_key_manager
    if _default_key_manager is None or tokens is not None:
        chosen_strategy = strategy or os.getenv("KEY_ROTATION_STRATEGY", "sequential")
        _default_key_manager = ApifyKeyManager(tokens=tokens, strategy=chosen_strategy)
    return _default_key_manager

def set_api_tokens(tokens, strategy="sequential"):
    """Explicitly configure the API tokens for notebooks or custom scripts."""
    global _default_key_manager
    _default_key_manager = ApifyKeyManager(tokens=tokens, strategy=strategy)
    return _default_key_manager

class _KeyManagerProxy:
    """Proxy object so `from run_crawler import key_manager` works dynamically."""
    def __getattr__(self, name):
        return getattr(get_default_key_manager(), name)

key_manager = _KeyManagerProxy()

def get_db_connection(db_path, timeout=60.0):
    """
    Returns an optimized sqlite3 connection.
    Configures busy timeout and safe journal mode for both local SSDs and network/FUSE drives (e.g. Google Drive).
    """
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 60000;")
        # Avoid WAL mode on network/FUSE filesystems (like Google Drive) to prevent locking / I/O errors
        is_fuse_or_drive = "drive" in db_path.lower() or "/content/drive" in db_path
        if is_fuse_or_drive:
            conn.execute("PRAGMA journal_mode = TRUNCATE;")
        else:
            conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
    except Exception:
        pass
    return conn

def normalize_text(text):
    if not text:
        return ""
    digits = {'۰':'0', '۱':'1', '۲':'2', '۳':'3', '۴':'4', '۵':'5', '۶':'6', '۷':'7', '۸':'8', '۹':'9',
              '٠':'0', '١':'1', '٢':'2', '٣':'3', '٤':'4', '٥':'5', '٦':'6', '٧':'7', '٨':'8', '٩':'9',
              'ي':'ی', 'ك':'ک', 'ة':'ه', 'ۀ':'ه'}
    t = text.lower()
    for k, v in digits.items():
        t = t.replace(k, v)
    t = re.sub(r'[\(\)\[\]\{\}\-_,\.\+]', ' ', t)
    stop_words = {'کافه', 'رستوران', 'cafe', 'restaurant', 'coffee', 'قهوه', 'فست', 'فود', 'fast', 'food'}
    tokens = [w for w in t.split() if w not in stop_words]
    return ' '.join(tokens) if tokens else t

def compute_similarity(s1, s2):
    n1 = normalize_text(s1)
    n2 = normalize_text(s2)
    if not n1 or not n2:
        return 0.5
    tokens1 = set(n1.split())
    tokens2 = set(n2.split())
    if tokens1 & tokens2:
        jaccard = len(tokens1 & tokens2) / len(tokens1 | tokens2)
        seq = SequenceMatcher(None, n1, n2).ratio()
        return round(max(jaccard, seq), 2)
    return round(SequenceMatcher(None, n1, n2).ratio(), 2)

def to_high_res_url(img_url):
    """
    Replaces Google image sizing modifier (e.g. =w1920-h1080-k-no) with =s0-rw 
    to retrieve the maximum resolution camera-master image in WebP format.
    """
    if not img_url:
        return ""
    if "=" in img_url:
        return img_url.rsplit("=", 1)[0] + "=s0-rw"
    return img_url + "=s0-rw"

def init_output_db(output_db_path=None):
    db_path = output_db_path or os.getenv("OUTPUT_DB_PATH", OUTPUT_DB_PATH)
    conn = get_db_connection(db_path)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS places (
        cafe_id INTEGER PRIMARY KEY,
        place_id TEXT UNIQUE,
        cid TEXT,
        fid TEXT,
        original_name TEXT,
        title TEXT,
        category_name TEXT,
        categories TEXT,
        address TEXT,
        street TEXT,
        city TEXT,
        state TEXT,
        country_code TEXT,
        postal_code TEXT,
        latitude REAL,
        longitude REAL,
        phone TEXT,
        phone_unformatted TEXT,
        website TEXT,
        total_score REAL,
        reviews_count INTEGER,
        images_count INTEGER,
        price_level TEXT,
        is_claimed BOOLEAN,
        is_permanently_closed BOOLEAN,
        is_temporarily_closed BOOLEAN,
        search_url TEXT,
        google_maps_url TEXT,
        status TEXT,
        name_match_score REAL,
        scraped_at TEXT
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS place_images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cafe_id INTEGER,
        source_type TEXT,          -- 'cover', 'gallery', 'review'
        is_owner_photo BOOLEAN,    -- 1 if uploaded by owner / official cover
        author_name TEXT,
        author_url TEXT,
        review_id TEXT,
        image_url TEXT,
        high_res_url TEXT,
        local_path TEXT,
        download_status TEXT DEFAULT 'pending',
        downloaded_at TEXT,
        FOREIGN KEY (cafe_id) REFERENCES places(cafe_id)
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS place_reviews (
        review_id TEXT PRIMARY KEY,
        cafe_id INTEGER,
        reviewer_id TEXT,
        reviewer_name TEXT,
        reviewer_photo_url TEXT,
        reviewer_reviews_count INTEGER,
        is_local_guide BOOLEAN,
        stars INTEGER,
        publish_date TEXT,
        publish_relative TEXT,
        likes_count INTEGER,
        text TEXT,
        original_language TEXT,
        detailed_food_rating REAL,
        detailed_service_rating REAL,
        detailed_atmosphere_rating REAL,
        response_from_owner_text TEXT,
        response_from_owner_date TEXT,
        FOREIGN KEY (cafe_id) REFERENCES places(cafe_id)
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS place_attributes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cafe_id INTEGER,
        group_name TEXT,
        attribute_name TEXT,
        value BOOLEAN DEFAULT 1,
        UNIQUE (cafe_id, group_name, attribute_name),
        FOREIGN KEY (cafe_id) REFERENCES places(cafe_id)
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS place_opening_hours (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cafe_id INTEGER,
        day TEXT,
        hours TEXT,
        UNIQUE (cafe_id, day),
        FOREIGN KEY (cafe_id) REFERENCES places(cafe_id)
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS place_popular_times (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cafe_id INTEGER,
        day TEXT,
        hour INTEGER,
        occupancy_percent INTEGER,
        UNIQUE (cafe_id, day, hour),
        FOREIGN KEY (cafe_id) REFERENCES places(cafe_id)
    )
    ''')

    # Indices
    cur.execute('CREATE INDEX IF NOT EXISTS idx_images_cafe ON place_images(cafe_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_images_owner ON place_images(is_owner_photo)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_reviews_cafe ON place_reviews(cafe_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_reviews_stars ON place_reviews(stars)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_attrs_cafe ON place_attributes(cafe_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_hours_cafe ON place_opening_hours(cafe_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_places_status ON places(status)')
    conn.commit()
    conn.close()

def build_search_url(name, lat, lng, zoom=ZOOM_LEVEL):
    clean_name = name.strip()
    encoded_name = urllib.parse.quote_plus(clean_name)
    return f"https://www.google.com/maps/search/{encoded_name}/@{lat},{lng},{zoom}z"

def get_pending_cafes(source_db_path=None, output_db_path=None, limit=None, zoom=None):
    source_db_path = source_db_path or os.getenv("SOURCE_DB_PATH", SOURCE_DB_PATH)
    output_db_path = output_db_path or os.getenv("OUTPUT_DB_PATH", OUTPUT_DB_PATH)
    zoom = int(zoom if zoom is not None else os.getenv("ZOOM_LEVEL", ZOOM_LEVEL))

    init_output_db(output_db_path)
    out_conn = get_db_connection(output_db_path)
    out_cur = out_conn.cursor()
    # Cafes already processed (either success or not_found)
    existing_ids = set(row[0] for row in out_cur.execute(
        "SELECT cafe_id FROM places"
    ).fetchall())
    out_conn.close()

    if not os.path.exists(source_db_path):
        raise FileNotFoundError(
            f"Source database not found at '{source_db_path}'. "
            "Please check the path or set SOURCE_DB_PATH in your .env or notebook."
        )

    src_conn = get_db_connection(source_db_path)
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
        search_url = build_search_url(name, lat, lng, zoom=zoom)
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

def save_batch_results(batch_cafes, scraped_items, output_db_path=None, raw_jsonl_path=None):
    output_db_path = output_db_path or os.getenv("OUTPUT_DB_PATH", OUTPUT_DB_PATH)
    raw_jsonl_path = raw_jsonl_path or os.getenv("RAW_JSONL_PATH", RAW_JSONL_PATH)

    init_output_db(output_db_path)
    conn = get_db_connection(output_db_path)
    cur = conn.cursor()

    url_to_cafe = {c["search_url"]: c for c in batch_cafes}
    matched_cafe_ids = set()

    # Append-only raw JSON Lines archive
    os.makedirs(os.path.dirname(os.path.abspath(raw_jsonl_path)), exist_ok=True)
    jsonl_file = open(raw_jsonl_path, "a", encoding="utf-8")

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

        # 1. Archive raw JSON payload into JSONL file
        archive_entry = {
            "cafe_id": cid,
            "original_name": cafe["name"],
            "searched_lat": cafe["latitude"],
            "searched_lng": cafe["longitude"],
            "search_url": cafe["search_url"],
            "status": "success",
            "scraped_at": item.get("scrapedAt") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "data": item
        }
        jsonl_file.write(json.dumps(archive_entry, ensure_ascii=False) + "\n")

        # 2. Extract into clean relational tables
        loc = item.get("location") or {}
        scraped_lat = loc.get("lat")
        scraped_lng = loc.get("lng")
        title = item.get("title") or cafe["name"]
        match_score = compute_similarity(cafe["name"], title)

        cur.execute('''
            INSERT OR REPLACE INTO places (
                cafe_id, place_id, cid, fid, original_name, title, category_name, categories,
                address, street, city, state, country_code, postal_code,
                latitude, longitude, phone, phone_unformatted, website,
                total_score, reviews_count, images_count, price_level,
                is_claimed, is_permanently_closed, is_temporarily_closed,
                search_url, google_maps_url, status, name_match_score, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            cid,
            item.get("placeId"),
            item.get("cid"),
            item.get("fid"),
            cafe["name"],
            title,
            item.get("categoryName"),
            json.dumps(item.get("categories", []), ensure_ascii=False),
            item.get("address"),
            item.get("street"),
            item.get("city"),
            item.get("state"),
            item.get("countryCode"),
            item.get("postalCode"),
            scraped_lat,
            scraped_lng,
            item.get("phone"),
            item.get("phoneUnformatted"),
            item.get("website"),
            item.get("totalScore"),
            item.get("reviewsCount"),
            item.get("imagesCount"),
            item.get("price"),
            1 if item.get("claimThisBusiness") else 0,
            1 if item.get("permanentlyClosed") else 0,
            1 if item.get("temporarilyClosed") else 0,
            cafe["search_url"],
            item.get("url"),
            "success",
            match_score,
            item.get("scrapedAt") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ))

        seen_image_urls = set()

        # Cover Image
        cover_url = item.get("imageUrl")
        if cover_url:
            seen_image_urls.add(cover_url)
            cur.execute('''
                INSERT INTO place_images (cafe_id, source_type, is_owner_photo, author_name, image_url, high_res_url)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (cid, "cover", 1, title, cover_url, to_high_res_url(cover_url)))

        # Rich images (from scrapeImageAuthors: true) or fallback imageUrls
        rich_images = item.get("images", [])
        if rich_images and isinstance(rich_images, list) and isinstance(rich_images[0], dict):
            for img_obj in rich_images:
                u = img_obj.get("imageUrl")
                if not u or u in seen_image_urls:
                    continue
                seen_image_urls.add(u)
                author = img_obj.get("authorName", "")
                author_url = img_obj.get("authorUrl", "")
                is_owner = 1 if (author and (normalize_text(author) == normalize_text(title))) else 0
                cur.execute('''
                    INSERT INTO place_images (cafe_id, source_type, is_owner_photo, author_name, author_url, image_url, high_res_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (cid, "gallery", is_owner, author, author_url, u, to_high_res_url(u)))
        else:
            gallery_urls = item.get("imageUrls", [])
            for idx, u in enumerate(gallery_urls):
                if not u or u in seen_image_urls:
                    continue
                seen_image_urls.add(u)
                is_owner = 1 if idx < 2 else 0
                cur.execute('''
                    INSERT INTO place_images (cafe_id, source_type, is_owner_photo, image_url, high_res_url)
                    VALUES (?, ?, ?, ?, ?)
                ''', (cid, "gallery", is_owner, u, to_high_res_url(u)))

        # Reviews & Review Images
        for r in item.get("reviews", []):
            rid = r.get("reviewId")
            if not rid:
                continue
            det_rating = r.get("reviewDetailedRating") or {}
            cur.execute('''
                INSERT OR IGNORE INTO place_reviews (
                    review_id, cafe_id, reviewer_id, reviewer_name, reviewer_photo_url,
                    reviewer_reviews_count, is_local_guide BOOLEAN, stars, publish_date,
                    publish_relative, likes_count, text, original_language,
                    detailed_food_rating, detailed_service_rating, detailed_atmosphere_rating,
                    response_from_owner_text, response_from_owner_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                rid, cid, r.get("reviewerId"), r.get("name"), r.get("reviewerPhotoUrl"),
                r.get("reviewerNumberOfReviews"), 1 if r.get("isLocalGuide") else 0,
                r.get("stars"), r.get("publishedAtDate"), r.get("publishAt"),
                r.get("likesCount") or 0, r.get("text"), r.get("originalLanguage"),
                det_rating.get("غذا") or det_rating.get("Food"),
                det_rating.get("خدمات") or det_rating.get("Service"),
                det_rating.get("محيط") or det_rating.get("Atmosphere"),
                r.get("responseFromOwnerText"), r.get("responseFromOwnerDate")
            ))

            for ru in r.get("reviewImageUrls", []):
                if ru and ru not in seen_image_urls:
                    seen_image_urls.add(ru)
                    cur.execute('''
                        INSERT INTO place_images (cafe_id, source_type, is_owner_photo, author_name, review_id, image_url, high_res_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (cid, "review", 0, r.get("name"), rid, ru, to_high_res_url(ru)))

        # Attributes
        add_info = item.get("additionalInfo") or {}
        for grp, items in add_info.items():
            if isinstance(items, list):
                for itm in items:
                    if isinstance(itm, dict):
                        for attr_name, val in itm.items():
                            cur.execute('''
                                INSERT OR IGNORE INTO place_attributes (cafe_id, group_name, attribute_name, value)
                                VALUES (?, ?, ?, ?)
                            ''', (cid, grp, attr_name, 1 if val else 0))

        # Opening Hours
        for h in item.get("openingHours") or []:
            if isinstance(h, dict) and "day" in h:
                cur.execute('''
                    INSERT OR IGNORE INTO place_opening_hours (cafe_id, day, hours)
                    VALUES (?, ?, ?)
                ''', (cid, h.get("day"), h.get("hours")))

        # Popular Times
        for day, hourly in (item.get("popularTimesHistogram") or {}).items():
            if isinstance(hourly, list):
                for h_item in hourly:
                    if isinstance(h_item, dict):
                        cur.execute('''
                            INSERT OR IGNORE INTO place_popular_times (cafe_id, day, hour, occupancy_percent)
                            VALUES (?, ?, ?, ?)
                        ''', (cid, day, h_item.get("hour"), h_item.get("occupancyPercent")))

    # Record unmatched cafes as not_found so we don't re-crawl repeatedly
    for c in batch_cafes:
        cid = c["cafe_id"]
        if cid not in matched_cafe_ids:
            cur.execute('''
                INSERT OR IGNORE INTO places (
                    cafe_id, original_name, latitude, longitude, search_url, status, scraped_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (cid, c["name"], c["latitude"], c["longitude"], c["search_url"], "not_found",
                  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))

            archive_entry = {
                "cafe_id": cid,
                "original_name": c["name"],
                "searched_lat": c["latitude"],
                "searched_lng": c["longitude"],
                "search_url": c["search_url"],
                "status": "not_found",
                "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "data": None
            }
            jsonl_file.write(json.dumps(archive_entry, ensure_ascii=False) + "\n")

    jsonl_file.close()
    conn.commit()
    conn.close()
    return len(matched_cafe_ids), len(batch_cafes) - len(matched_cafe_ids)

def run_scraper(
    batch_size=None,
    max_total=None,
    source_db_path=None,
    output_db_path=None,
    raw_jsonl_path=None,
    max_images=None,
    max_reviews=None,
    zoom_level=None,
    key_manager_instance=None,
    scrape_image_authors=None,
    progress_bar=True
):
    """
    Main scraper execution engine.
    Supports modular arguments, live progress tracking, auto-rotation across multiple keys,
    and resumability.
    """
    source_db_path = source_db_path or os.getenv("SOURCE_DB_PATH", SOURCE_DB_PATH)
    output_db_path = output_db_path or os.getenv("OUTPUT_DB_PATH", OUTPUT_DB_PATH)
    raw_jsonl_path = raw_jsonl_path or os.getenv("RAW_JSONL_PATH", RAW_JSONL_PATH)
    batch_size = int(batch_size or os.getenv("BATCH_SIZE", "50"))
    max_images = int(max_images if max_images is not None else os.getenv("MAX_IMAGES_PER_PLACE", "50"))
    max_reviews = int(max_reviews if max_reviews is not None else os.getenv("MAX_REVIEWS_PER_PLACE", "15"))
    zoom_level = int(zoom_level if zoom_level is not None else os.getenv("ZOOM_LEVEL", "17"))
    if scrape_image_authors is None:
        scrape_image_authors = os.getenv("SCRAPE_IMAGE_AUTHORS", "true").lower() in ("true", "1", "yes")

    km = key_manager_instance or get_default_key_manager()
    init_output_db(output_db_path)

    print("\n[Apify Pool Initialization]")
    km.print_summary()

    all_pending = get_pending_cafes(source_db_path=source_db_path, output_db_path=output_db_path, limit=max_total, zoom=zoom_level)
    total_count = len(all_pending)
    print(f"Total pending cafes to crawl: {total_count}")

    if total_count == 0:
        print("All cafes in the source database have already been scraped!")
        return {
            "processed": 0,
            "matched": 0,
            "not_found": 0,
            "total_pending": 0,
            "stopped_reason": "already_up_to_date"
        }

    processed = 0
    total_matched = 0
    total_not_found = 0
    total_batches = (total_count + batch_size - 1) // batch_size

    pbar = None
    if progress_bar:
        try:
            from tqdm.auto import tqdm
            pbar = tqdm(total=total_count, desc="Crawling Cafes", unit="cafe")
        except ImportError:
            pbar = None

    for i in range(0, total_count, batch_size):
        batch = all_pending[i:i + batch_size]
        batch_num = i // batch_size + 1
        print(f"\n==========================================")
        print(f"Starting batch {batch_num}/{total_batches} ({len(batch)} cafes: '{batch[0]['name']}' ... '{batch[-1]['name']}')")
        print(f"==========================================")

        start_urls = [{"url": c["search_url"]} for c in batch]
        
        run_input = {
            "startUrls": start_urls,
            "maxCrawledPlacesPerSearch": 1,
            "maxImages": max_images,
            "maxReviews": max_reviews,
            "scrapeImageAuthors": scrape_image_authors,
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

                items = km.execute_with_retry(_execute_crawl)
                print(f"Retrieved {len(items)} places from Apify.")

                matched, not_found = save_batch_results(
                    batch, items, output_db_path=output_db_path, raw_jsonl_path=raw_jsonl_path
                )
                total_matched += matched
                total_not_found += not_found
                processed += len(batch)
                
                if pbar:
                    pbar.update(len(batch))
                    pbar.set_postfix({"matched": total_matched, "not_found": total_not_found})

                print(f"Batch {batch_num} saved: {matched} matched, {not_found} not found.")
                print(f"Overall Progress: {processed}/{total_count} processed ({total_matched} success, {total_not_found} not found).")
                success = True
                break
            except AllKeysExhaustedError as e:
                print(f"\n[FATAL] Key Pool Exhaustion: {e}")
                print("All available Apify keys have been exhausted. Stopping crawl gracefully.")
                if pbar:
                    pbar.close()
                return {
                    "processed": processed,
                    "matched": total_matched,
                    "not_found": total_not_found,
                    "total_pending": total_count,
                    "stopped_reason": "keys_exhausted"
                }
            except Exception as e:
                print(f"Error on batch {batch_num} attempt {attempt}: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(5)
                else:
                    print(f"Failed to process batch {batch_num} after {MAX_RETRIES} attempts.")

        if not success:
            print("Stopping crawl due to consecutive errors.")
            break

    if pbar:
        pbar.close()

    print(f"\nCrawl complete! Processed: {processed}/{total_count} (Total Success: {total_matched}, Not found: {total_not_found})")
    return {
        "processed": processed,
        "matched": total_matched,
        "not_found": total_not_found,
        "total_pending": total_count,
        "stopped_reason": "completed" if processed == total_count else "stopped_early"
    }

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run_scraper(max_total=limit)
