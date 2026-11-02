import os
import re
import sys
import glob
import json
import time
import sqlite3
import urllib.parse
from difflib import SequenceMatcher
from apify_client import ApifyClient
from key_manager import ApifyKeyManager, AllKeysExhaustedError
from cafe_matcher import (
    haversine_distance,
    compute_name_similarity,
    evaluate_cafe_match,
    select_best_candidate,
    normalize_text,
    MatchResult
)

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
DATA_DIR = os.path.join(BASE_DIR, "data")
SOURCE_DB_PATH = os.getenv("SOURCE_DB_PATH", os.path.join(DATA_DIR, "cafes_cafe_shard00.db"))
OUTPUT_DB_PATH = os.getenv("OUTPUT_DB_PATH", os.path.join(DATA_DIR, "google_maps.db"))
RAW_JSONL_PATH = os.getenv("RAW_JSONL_PATH", os.path.join(DATA_DIR, "google_maps_raw.jsonl"))

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
            glob.glob(os.path.join(file_path, "**/*cafes_cafe*.db"), recursive=True) +
            glob.glob(os.path.join(file_path, "**/*.db"), recursive=True) +
            glob.glob(os.path.join(file_path, "**/*.sqlite*"), recursive=True)
        )
        if cands:
            return cands[0]
        return file_path
    if os.path.exists(file_path):
        return file_path
    
    # Only fallback to data/ and BASE_DIR if a bare filename was provided without parent directory
    parent_dir = os.path.dirname(file_path)
    if not parent_dir:
        data_cand = os.path.join(DATA_DIR, os.path.basename(file_path))
        if os.path.exists(data_cand):
            return data_cand
        root_cand = os.path.join(BASE_DIR, os.path.basename(file_path))
        if os.path.exists(root_cand):
            return root_cand
    return file_path

# Scraper Settings
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))
MAX_IMAGES_PER_PLACE = int(os.getenv("MAX_IMAGES_PER_PLACE", "50"))
MAX_REVIEWS_PER_PLACE = int(os.getenv("MAX_REVIEWS_PER_PLACE", "15"))
ZOOM_LEVEL = int(os.getenv("ZOOM_LEVEL", "17"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
SCRAPE_IMAGE_AUTHORS = os.getenv("SCRAPE_IMAGE_AUTHORS", "true").lower() in ("true", "1", "yes")
MAX_PLACES_PER_SEARCH = int(os.getenv("MAX_PLACES_PER_SEARCH", "5"))
MATCH_THRESHOLD = float(os.getenv("MATCH_THRESHOLD", "0.80"))

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

def get_db_connection(db_path, timeout=60.0, read_only=False):
    """
    Returns an optimized sqlite3 connection.
    Configures busy timeout and safe journal mode for both local SSDs and network/FUSE drives (e.g. Google Drive).
    Handles read-only filesystems (e.g. Kaggle /kaggle/input/) and directory paths gracefully.
    """
    db_path = resolve_data_path(db_path)
    if os.path.isdir(db_path):
        cands = (
            glob.glob(os.path.join(db_path, "**/*cafes_cafe*.db"), recursive=True) +
            glob.glob(os.path.join(db_path, "**/*.db"), recursive=True) +
            glob.glob(os.path.join(db_path, "**/*.sqlite*"), recursive=True)
        )
        if cands:
            db_path = cands[0]
        else:
            contents = os.listdir(db_path) if os.path.exists(db_path) else []
            raise FileNotFoundError(
                f"Database path '{db_path}' is a directory, but no .db or .sqlite file was found inside. Contents: {contents}"
            )

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
            # Fallback if read-only mount prevents locking or WAL indexing:
            # Copy to high-speed writable storage (/kaggle/working or system temp dir)
            try:
                import shutil
                import tempfile
                tmp_dir = "/kaggle/working" if os.path.exists("/kaggle/working") else tempfile.gettempdir()
                cached_path = os.path.join(tmp_dir, os.path.basename(db_path))
                if not os.path.exists(cached_path) or os.path.getsize(cached_path) != os.path.getsize(db_path):
                    shutil.copy2(db_path, cached_path)
                conn = sqlite3.connect(cached_path, timeout=timeout)
                conn.row_factory = sqlite3.Row
                try:
                    conn.execute("PRAGMA busy_timeout = 60000;")
                except Exception:
                    pass
                return conn
            except Exception:
                conn = sqlite3.connect(db_path, timeout=timeout)
                conn.row_factory = sqlite3.Row
                return conn

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
    return compute_name_similarity(s1, s2)

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
        distance_meters REAL,
        neighborhood TEXT,
        located_in TEXT,
        floor TEXT,
        menu TEXT,
        owner_description TEXT,
        plus_code TEXT,
        kgmid TEXT,
        reviews_distribution TEXT,
        reviews_distribution_1_star INTEGER,
        reviews_distribution_2_star INTEGER,
        reviews_distribution_3_star INTEGER,
        reviews_distribution_4_star INTEGER,
        reviews_distribution_5_star INTEGER,
        emails TEXT,
        phones TEXT,
        instagrams TEXT,
        facebooks TEXT,
        twitters TEXT,
        social_profiles TEXT,
        scraped_at TEXT
    )
    ''')

    # Migration alter statements for backwards compatibility
    new_place_cols = [
        ("distance_meters", "REAL"),
        ("neighborhood", "TEXT"),
        ("located_in", "TEXT"),
        ("floor", "TEXT"),
        ("menu", "TEXT"),
        ("owner_description", "TEXT"),
        ("plus_code", "TEXT"),
        ("kgmid", "TEXT"),
        ("reviews_distribution", "TEXT"),
        ("reviews_distribution_1_star", "INTEGER"),
        ("reviews_distribution_2_star", "INTEGER"),
        ("reviews_distribution_3_star", "INTEGER"),
        ("reviews_distribution_4_star", "INTEGER"),
        ("reviews_distribution_5_star", "INTEGER"),
        ("emails", "TEXT"),
        ("phones", "TEXT"),
        ("instagrams", "TEXT"),
        ("facebooks", "TEXT"),
        ("twitters", "TEXT"),
        ("social_profiles", "TEXT"),
    ]
    for col, col_type in new_place_cols:
        try:
            cur.execute(f"ALTER TABLE places ADD COLUMN {col} {col_type}")
        except Exception:
            pass

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
        detailed_room_rating REAL,
        detailed_location_rating REAL,
        response_from_owner_text TEXT,
        response_from_owner_date TEXT,
        review_url TEXT,
        reviewer_url TEXT,
        review_context TEXT,
        context_order_type TEXT,
        context_meal_type TEXT,
        context_price_per_person TEXT,
        context_noise_level TEXT,
        text_translated TEXT,
        translated_language TEXT,
        FOREIGN KEY (cafe_id) REFERENCES places(cafe_id)
    )
    ''')

    new_review_cols = [
        ("detailed_room_rating", "REAL"),
        ("detailed_location_rating", "REAL"),
        ("review_url", "TEXT"),
        ("reviewer_url", "TEXT"),
        ("review_context", "TEXT"),
        ("context_order_type", "TEXT"),
        ("context_meal_type", "TEXT"),
        ("context_price_per_person", "TEXT"),
        ("context_noise_level", "TEXT"),
        ("text_translated", "TEXT"),
        ("translated_language", "TEXT"),
    ]
    for col, col_type in new_review_cols:
        try:
            cur.execute(f"ALTER TABLE place_reviews ADD COLUMN {col} {col_type}")
        except Exception:
            pass

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
    source_db_path = resolve_data_path(source_db_path or os.getenv("SOURCE_DB_PATH", SOURCE_DB_PATH))
    output_db_path = resolve_data_path(output_db_path or os.getenv("OUTPUT_DB_PATH", OUTPUT_DB_PATH))
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
            "Please place your cafes database (e.g. cafes_cafe_shard00.db) in the 'data/' folder or set SOURCE_DB_PATH."
        )

    src_conn = get_db_connection(source_db_path, read_only=True)
    src_cur = src_conn.cursor()
    tables = [r[0] for r in src_cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    target_table = "cafes_cafe" if "cafes_cafe" in tables else (tables[0] if tables else "cafes_cafe")
    col_names = [col[1] for col in src_cur.execute(f"PRAGMA table_info({target_table})").fetchall()]
    select_cols = [c for c in ["id", "name", "latitude", "longitude", "city", "address"] if c in col_names]
    for extra in ["phone", "instagram", "website"]:
        if extra in col_names:
            select_cols.append(extra)
    rows = src_cur.execute(f"SELECT {', '.join(select_cols)} FROM {target_table}").fetchall()
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
            "address": r["address"],
            "phone": r["phone"] if "phone" in r.keys() else None,
            "instagram": r["instagram"] if "instagram" in r.keys() else None,
            "website": r["website"] if "website" in r.keys() else None,
            "search_url": search_url
        })
        if limit and len(pending) >= limit:
            break

    return pending

def save_batch_results(batch_cafes, scraped_items, output_db_path=None, raw_jsonl_path=None, threshold=MATCH_THRESHOLD):
    output_db_path = resolve_data_path(output_db_path or os.getenv("OUTPUT_DB_PATH", OUTPUT_DB_PATH))
    raw_jsonl_path = resolve_data_path(raw_jsonl_path or os.getenv("RAW_JSONL_PATH", RAW_JSONL_PATH))

    init_output_db(output_db_path)
    conn = get_db_connection(output_db_path)
    cur = conn.cursor()

    # Group scraped items by search_url
    url_to_candidates = {c["search_url"]: [] for c in batch_cafes}
    unmapped_items = []

    for item in scraped_items:
        raw_search_url = item.get("searchPageUrl", "")
        clean_url = raw_search_url.split("?")[0]
        if clean_url in url_to_candidates:
            url_to_candidates[clean_url].append(item)
        else:
            unmapped_items.append(item)

    # For any unmapped items (due to URL variations), assign to closest batch cafe within 250m
    for item in unmapped_items:
        loc = item.get("location") or {}
        item_lat = loc.get("lat")
        item_lng = loc.get("lng")
        if item_lat and item_lng:
            best_c = None
            min_d = float('inf')
            for c in batch_cafes:
                d = haversine_distance(c["latitude"], c["longitude"], item_lat, item_lng)
                if d < min_d and d <= 250.0:
                    min_d = d
                    best_c = c
            if best_c:
                url_to_candidates[best_c["search_url"]].append(item)

    matched_cafe_ids = set()

    # Append-only raw JSON Lines archive
    os.makedirs(os.path.dirname(os.path.abspath(raw_jsonl_path)), exist_ok=True)
    jsonl_file = open(raw_jsonl_path, "a", encoding="utf-8")

    for cafe in batch_cafes:
        cid = cafe["cafe_id"]
        candidates = url_to_candidates.get(cafe["search_url"], [])

        best_item, match_res = select_best_candidate(cafe, candidates, threshold=threshold)

        if best_item and match_res and match_res.is_match:
            matched_cafe_ids.add(cid)

            # 1. Archive raw JSON payload into JSONL file
            archive_entry = {
                "cafe_id": cid,
                "original_name": cafe["name"],
                "searched_lat": cafe["latitude"],
                "searched_lng": cafe["longitude"],
                "search_url": cafe["search_url"],
                "status": "success",
                "composite_score": match_res.composite_score,
                "distance_meters": round(match_res.distance_meters, 1),
                "name_score": match_res.name_score,
                "phone_matched": match_res.phone_matched,
                "web_matched": match_res.web_matched,
                "candidates_evaluated": len(candidates),
                "scraped_at": best_item.get("scrapedAt") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "data": best_item
            }
            jsonl_file.write(json.dumps(archive_entry, ensure_ascii=False) + "\n")

            # 2. Extract into clean relational tables
            loc = best_item.get("location") or {}
            scraped_lat = loc.get("lat")
            scraped_lng = loc.get("lng")
            title = best_item.get("title") or cafe["name"]

            # Parse reviews distribution
            rev_dist = best_item.get("reviewsDistribution") or {}
            rev_dist_str = json.dumps(rev_dist, ensure_ascii=False) if rev_dist else None
            star1 = rev_dist.get("oneStar")
            star2 = rev_dist.get("twoStar")
            star3 = rev_dist.get("threeStar")
            star4 = rev_dist.get("fourStar")
            star5 = rev_dist.get("fiveStar")

            # Parse contacts & socials
            emails = json.dumps(best_item.get("emails", []), ensure_ascii=False) if best_item.get("emails") else None
            phones = json.dumps(best_item.get("phones", []), ensure_ascii=False) if best_item.get("phones") else None
            instagrams = json.dumps(best_item.get("instagrams", []), ensure_ascii=False) if best_item.get("instagrams") else None
            facebooks = json.dumps(best_item.get("facebooks", []), ensure_ascii=False) if best_item.get("facebooks") else None
            twitters = json.dumps(best_item.get("twitters", []), ensure_ascii=False) if best_item.get("twitters") else None

            soc_profiles = {}
            for sp_key in ["instagramProfiles", "facebookProfiles", "twitterProfiles", "youtubeProfiles", "tiktokProfiles"]:
                if best_item.get(sp_key):
                    soc_profiles[sp_key] = best_item[sp_key]
            soc_profiles_str = json.dumps(soc_profiles, ensure_ascii=False) if soc_profiles else None

            cur.execute('''
                INSERT OR REPLACE INTO places (
                    cafe_id, place_id, cid, fid, original_name, title, category_name, categories,
                    address, street, city, state, country_code, postal_code,
                    latitude, longitude, phone, phone_unformatted, website,
                    total_score, reviews_count, images_count, price_level,
                    is_claimed, is_permanently_closed, is_temporarily_closed,
                    search_url, google_maps_url, status, name_match_score, distance_meters,
                    neighborhood, located_in, floor, menu, owner_description,
                    plus_code, kgmid, reviews_distribution,
                    reviews_distribution_1_star, reviews_distribution_2_star,
                    reviews_distribution_3_star, reviews_distribution_4_star,
                    reviews_distribution_5_star, emails, phones, instagrams,
                    facebooks, twitters, social_profiles, scraped_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                cid,
                best_item.get("placeId"),
                best_item.get("cid"),
                best_item.get("fid"),
                cafe["name"],
                title,
                best_item.get("categoryName"),
                json.dumps(best_item.get("categories", []), ensure_ascii=False),
                best_item.get("address"),
                best_item.get("street"),
                best_item.get("city"),
                best_item.get("state"),
                best_item.get("countryCode"),
                best_item.get("postalCode"),
                scraped_lat,
                scraped_lng,
                best_item.get("phone"),
                best_item.get("phoneUnformatted"),
                best_item.get("website"),
                best_item.get("totalScore"),
                best_item.get("reviewsCount"),
                best_item.get("imagesCount"),
                best_item.get("price"),
                1 if best_item.get("claimThisBusiness") else 0,
                1 if best_item.get("permanentlyClosed") else 0,
                1 if best_item.get("temporarilyClosed") else 0,
                cafe["search_url"],
                best_item.get("url"),
                "success",
                match_res.composite_score,
                round(match_res.distance_meters, 1),
                best_item.get("neighborhood"),
                best_item.get("locatedIn"),
                best_item.get("floor"),
                best_item.get("menu"),
                best_item.get("ownerDescription"),
                best_item.get("plusCode"),
                best_item.get("kgmid"),
                rev_dist_str,
                star1, star2, star3, star4, star5,
                emails, phones, instagrams, facebooks, twitters, soc_profiles_str,
                best_item.get("scrapedAt") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            ))

            seen_image_urls = set()

            # Cover Image
            cover_url = best_item.get("imageUrl")
            if cover_url:
                seen_image_urls.add(cover_url)
                cur.execute('''
                    INSERT INTO place_images (cafe_id, source_type, is_owner_photo, author_name, image_url, high_res_url)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (cid, "cover", 1, title, cover_url, to_high_res_url(cover_url)))

            # Rich images (from scrapeImageAuthors: true) or fallback imageUrls
            rich_images = best_item.get("images", [])
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
                gallery_urls = best_item.get("imageUrls", [])
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
            for r in best_item.get("reviews", []):
                rid = r.get("reviewId")
                if not rid:
                    continue
                det_rating = r.get("reviewDetailedRating") or {}
                ctx = r.get("reviewContext") or {}
                ctx_str = json.dumps(ctx, ensure_ascii=False) if ctx else None
                order_type = ctx.get("نوع سفارش") or ctx.get("Dine in") or ctx.get("Takeout")
                meal_type = ctx.get("وعده غذایی") or ctx.get("Meal")
                price_pp = ctx.get("قیمت برای هر نفر") or ctx.get("Price per person")
                noise = ctx.get("سطح سروصدا") or ctx.get("Noise level")

                cur.execute('''
                    INSERT OR REPLACE INTO place_reviews (
                        review_id, cafe_id, reviewer_id, reviewer_name, reviewer_photo_url,
                        reviewer_reviews_count, is_local_guide, stars, publish_date,
                        publish_relative, likes_count, text, original_language,
                        detailed_food_rating, detailed_service_rating, detailed_atmosphere_rating,
                        detailed_room_rating, detailed_location_rating,
                        response_from_owner_text, response_from_owner_date,
                        review_url, reviewer_url, review_context,
                        context_order_type, context_meal_type, context_price_per_person,
                        context_noise_level, text_translated, translated_language
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    rid, cid, r.get("reviewerId"), r.get("name"), r.get("reviewerPhotoUrl"),
                    r.get("reviewerNumberOfReviews"), 1 if r.get("isLocalGuide") else 0,
                    r.get("stars"), r.get("publishedAtDate"), r.get("publishAt"),
                    r.get("likesCount") or 0, r.get("text"), r.get("originalLanguage"),
                    det_rating.get("غذا") or det_rating.get("Food"),
                    det_rating.get("خدمات") or det_rating.get("Service"),
                    det_rating.get("محيط") or det_rating.get("Atmosphere"),
                    det_rating.get("اتاق") or det_rating.get("Room"),
                    det_rating.get("مکان") or det_rating.get("Location"),
                    r.get("responseFromOwnerText"), r.get("responseFromOwnerDate"),
                    r.get("reviewUrl"), r.get("reviewerUrl"), ctx_str,
                    order_type, meal_type, price_pp, noise,
                    r.get("textTranslated"), r.get("translatedLanguage")
                ))

                for ru in r.get("reviewImageUrls", []):
                    if ru and ru not in seen_image_urls:
                        seen_image_urls.add(ru)
                        cur.execute('''
                            INSERT INTO place_images (cafe_id, source_type, is_owner_photo, author_name, review_id, image_url, high_res_url)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (cid, "review", 0, r.get("name"), rid, ru, to_high_res_url(ru)))

            # Attributes
            add_info = best_item.get("additionalInfo") or {}
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
            for h in best_item.get("openingHours") or []:
                if isinstance(h, dict) and "day" in h:
                    cur.execute('''
                        INSERT OR IGNORE INTO place_opening_hours (cafe_id, day, hours)
                        VALUES (?, ?, ?)
                    ''', (cid, h.get("day"), h.get("hours")))

            # Popular Times
            for day, hourly in (best_item.get("popularTimesHistogram") or {}).items():
                if isinstance(hourly, list):
                    for h_item in hourly:
                        if isinstance(h_item, dict):
                            cur.execute('''
                                INSERT OR IGNORE INTO place_popular_times (cafe_id, day, hour, occupancy_percent)
                                VALUES (?, ?, ?, ?)
                            ''', (cid, day, h_item.get("hour"), h_item.get("occupancyPercent")))
        else:
            # NOT FOUND / REJECTED
            reason = match_res.rejection_reason if match_res else "No candidates returned by Google Maps"
            comp_score = match_res.composite_score if match_res else 0.0
            dist_m = round(match_res.distance_meters, 1) if (match_res and match_res.distance_meters != float('inf')) else None

            cur.execute('''
                INSERT OR REPLACE INTO places (
                    cafe_id, original_name, latitude, longitude, search_url, status, name_match_score, distance_meters, scraped_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (cid, cafe["name"], cafe["latitude"], cafe["longitude"], cafe["search_url"], "not_found",
                  comp_score, dist_m, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))

            archive_entry = {
                "cafe_id": cid,
                "original_name": cafe["name"],
                "searched_lat": cafe["latitude"],
                "searched_lng": cafe["longitude"],
                "search_url": cafe["search_url"],
                "status": "not_found",
                "rejection_reason": reason,
                "best_score": comp_score,
                "distance_meters": dist_m,
                "candidates_evaluated": len(candidates),
                "candidate_titles": [c.get("title") for c in candidates],
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
    max_places_per_search=None,
    match_threshold=None,
    key_manager_instance=None,
    scrape_image_authors=None,
    scrape_contacts=None,
    scrape_social_media_profiles=None,
    progress_bar=True
):
    """
    Main scraper execution engine.
    Supports modular arguments, live progress tracking, auto-rotation across multiple keys,
    strict matching, and resumability.
    """
    source_db_path = resolve_data_path(source_db_path or os.getenv("SOURCE_DB_PATH", SOURCE_DB_PATH))
    output_db_path = resolve_data_path(output_db_path or os.getenv("OUTPUT_DB_PATH", OUTPUT_DB_PATH))
    raw_jsonl_path = resolve_data_path(raw_jsonl_path or os.getenv("RAW_JSONL_PATH", RAW_JSONL_PATH))
    batch_size = int(batch_size or os.getenv("BATCH_SIZE", "50"))
    max_images = int(max_images if max_images is not None else os.getenv("MAX_IMAGES_PER_PLACE", "50"))
    max_reviews = int(max_reviews if max_reviews is not None else os.getenv("MAX_REVIEWS_PER_PLACE", "15"))
    zoom_level = int(zoom_level if zoom_level is not None else os.getenv("ZOOM_LEVEL", "17"))
    max_places_per_search = int(max_places_per_search if max_places_per_search is not None else os.getenv("MAX_PLACES_PER_SEARCH", "5"))
    match_threshold = float(match_threshold if match_threshold is not None else os.getenv("MATCH_THRESHOLD", "0.80"))
    if scrape_image_authors is None:
        scrape_image_authors = os.getenv("SCRAPE_IMAGE_AUTHORS", "true").lower() in ("true", "1", "yes")
    if scrape_contacts is None:
        scrape_contacts = os.getenv("SCRAPE_CONTACTS", "false").lower() in ("true", "1", "yes")
    if scrape_social_media_profiles is None:
        scrape_social_media_profiles = os.getenv("SCRAPE_SOCIAL_MEDIA_PROFILES", "false").lower() in ("true", "1", "yes")

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
            "maxCrawledPlacesPerSearch": max_places_per_search,
            "maxImages": max_images,
            "maxReviews": max_reviews,
            "scrapeImageAuthors": scrape_image_authors,
            "language": "fa"
        }
        if scrape_contacts:
            run_input["scrapeContacts"] = True
        if scrape_social_media_profiles:
            run_input["scrapeSocialMediaProfiles"] = {
                "facebooks": True,
                "instagrams": True,
                "twitters": True,
                "youtubes": False,
                "tiktoks": False
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
                    batch, items, output_db_path=output_db_path, raw_jsonl_path=raw_jsonl_path, threshold=match_threshold
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
    import argparse
    parser = argparse.ArgumentParser(description="Google Maps Scraper")
    parser.add_argument("limit", nargs="?", type=int, default=None, help="Max places to process")
    parser.add_argument("--source-db", type=str, default=None, help="Path to source SQLite database")
    parser.add_argument("--output-db", type=str, default=None, help="Path to output SQLite database")
    parser.add_argument("--jsonl-path", type=str, default=None, help="Path to output JSONL file")
    parser.add_argument("--batch-size", type=int, default=None, help="Batch size")
    parser.add_argument("--scrape-contacts", action="store_true", help="Enable company contacts enrichment")
    parser.add_argument("--scrape-socials", action="store_true", help="Enable social media profiles enrichment")
    args = parser.parse_args()

    run_scraper(
        max_total=args.limit,
        source_db_path=args.source_db,
        output_db_path=args.output_db,
        raw_jsonl_path=args.jsonl_path,
        batch_size=args.batch_size,
        scrape_contacts=args.scrape_contacts or None,
        scrape_social_media_profiles=args.scrape_socials or None
    )
