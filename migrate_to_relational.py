"""
Database Migration Script: Dual Storage Architecture
=====================================================
Migrates monolithic google_maps_raw.db into:
1. google_maps_raw.jsonl: Zero-data-loss raw JSON Lines archive.
2. google_maps.db: Clean, normalized, high-performance relational SQLite database.
"""

import os
import sys
import json
import sqlite3
import shutil
import re
from difflib import SequenceMatcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OLD_DB_PATH = os.path.join(BASE_DIR, "google_maps_raw.db")
BACKUP_DB_PATH = os.path.join(BASE_DIR, "google_maps_raw.db.bak")
RAW_JSONL_PATH = os.path.join(BASE_DIR, "google_maps_raw.jsonl")
NEW_DB_PATH = os.path.join(BASE_DIR, "google_maps.db")

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
    if not img_url:
        return ""
    if "=" in img_url:
        return img_url.rsplit("=", 1)[0] + "=s0"
    return img_url + "=s0"

def create_schema(conn):
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

def migrate():
    if not os.path.exists(OLD_DB_PATH):
        print(f"Old database not found at {OLD_DB_PATH}")
        return

    # 1. Backup old database safely
    if not os.path.exists(BACKUP_DB_PATH):
        print(f"Creating safety backup: {OLD_DB_PATH} -> {BACKUP_DB_PATH}")
        shutil.copy2(OLD_DB_PATH, BACKUP_DB_PATH)
    else:
        print(f"Safety backup already exists at {BACKUP_DB_PATH}")

    old_conn = sqlite3.connect(OLD_DB_PATH)
    old_cur = old_conn.cursor()
    rows = old_cur.execute('''
        SELECT cafe_id, original_name, searched_lat, searched_lng, search_url, raw_data, status, scraped_at
        FROM google_places_raw
    ''').fetchall()
    old_conn.close()

    print(f"Found {len(rows)} total records in old database.")

    # Remove existing new db if recreating
    if os.path.exists(NEW_DB_PATH):
        os.remove(NEW_DB_PATH)

    new_conn = sqlite3.connect(NEW_DB_PATH)
    create_schema(new_conn)
    new_cur = new_conn.cursor()

    jsonl_file = open(RAW_JSONL_PATH, "w", encoding="utf-8")

    stats = {
        "places": 0,
        "not_found": 0,
        "images_cover": 0,
        "images_gallery": 0,
        "images_review": 0,
        "reviews": 0,
        "attributes": 0,
        "opening_hours": 0,
        "popular_times": 0
    }

    for row in rows:
        cid, orig_name, s_lat, s_lng, s_url, raw_data_str, status, scraped_at = row

        raw_obj = json.loads(raw_data_str) if raw_data_str else None

        # 1. Write to raw JSON Lines archive
        archive_entry = {
            "cafe_id": cid,
            "original_name": orig_name,
            "searched_lat": s_lat,
            "searched_lng": s_lng,
            "search_url": s_url,
            "status": status,
            "scraped_at": scraped_at,
            "data": raw_obj
        }
        jsonl_file.write(json.dumps(archive_entry, ensure_ascii=False) + "\n")

        # 2. Insert into clean relational DB
        if status != "success" or not raw_obj:
            stats["not_found"] += 1
            new_cur.execute('''
                INSERT INTO places (cafe_id, original_name, latitude, longitude, search_url, status, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (cid, orig_name, s_lat, s_lng, s_url, status, scraped_at))
            continue

        stats["places"] += 1
        d = raw_obj
        loc = d.get("location") or {}
        scraped_lat = loc.get("lat")
        scraped_lng = loc.get("lng")
        title = d.get("title") or orig_name

        match_score = compute_similarity(orig_name, title)

        new_cur.execute('''
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
            d.get("placeId"),
            d.get("cid"),
            d.get("fid"),
            orig_name,
            title,
            d.get("categoryName"),
            json.dumps(d.get("categories", []), ensure_ascii=False),
            d.get("address"),
            d.get("street"),
            d.get("city"),
            d.get("state"),
            d.get("countryCode"),
            d.get("postalCode"),
            scraped_lat,
            scraped_lng,
            d.get("phone"),
            d.get("phoneUnformatted"),
            d.get("website"),
            d.get("totalScore"),
            d.get("reviewsCount"),
            d.get("imagesCount"),
            d.get("price"),
            1 if d.get("claimThisBusiness") else 0,
            1 if d.get("permanentlyClosed") else 0,
            1 if d.get("temporarilyClosed") else 0,
            s_url,
            d.get("url"),
            "success",
            match_score,
            d.get("scrapedAt") or scraped_at
        ))

        seen_image_urls = set()

        # Cover image
        cover_url = d.get("imageUrl")
        if cover_url:
            seen_image_urls.add(cover_url)
            stats["images_cover"] += 1
            new_cur.execute('''
                INSERT INTO place_images (cafe_id, source_type, is_owner_photo, author_name, image_url, high_res_url)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (cid, "cover", 1, title, cover_url, to_high_res_url(cover_url)))

        # Gallery images
        gallery_urls = d.get("imageUrls", [])
        for idx, u in enumerate(gallery_urls):
            if not u or u in seen_image_urls:
                continue
            seen_image_urls.add(u)
            # Top 2 photos in Google Maps gallery are almost always owner / primary photos
            is_owner = 1 if idx < 2 else 0
            stats["images_gallery"] += 1
            new_cur.execute('''
                INSERT INTO place_images (cafe_id, source_type, is_owner_photo, image_url, high_res_url)
                VALUES (?, ?, ?, ?, ?)
            ''', (cid, "gallery", is_owner, u, to_high_res_url(u)))

        # Reviews & Review images
        reviews = d.get("reviews", [])
        for r in reviews:
            rid = r.get("reviewId")
            if not rid:
                continue
            stats["reviews"] += 1
            det_rating = r.get("reviewDetailedRating") or {}

            new_cur.execute('''
                INSERT OR IGNORE INTO place_reviews (
                    review_id, cafe_id, reviewer_id, reviewer_name, reviewer_photo_url,
                    reviewer_reviews_count, is_local_guide, stars, publish_date,
                    publish_relative, likes_count, text, original_language,
                    detailed_food_rating, detailed_service_rating, detailed_atmosphere_rating,
                    response_from_owner_text, response_from_owner_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                rid,
                cid,
                r.get("reviewerId"),
                r.get("name"),
                r.get("reviewerPhotoUrl"),
                r.get("reviewerNumberOfReviews"),
                1 if r.get("isLocalGuide") else 0,
                r.get("stars"),
                r.get("publishedAtDate"),
                r.get("publishAt"),
                r.get("likesCount") or 0,
                r.get("text"),
                r.get("originalLanguage"),
                det_rating.get("غذا") or det_rating.get("Food"),
                det_rating.get("خدمات") or det_rating.get("Service"),
                det_rating.get("محيط") or det_rating.get("Atmosphere"),
                r.get("responseFromOwnerText"),
                r.get("responseFromOwnerDate")
            ))

            for ru in r.get("reviewImageUrls", []):
                if ru and ru not in seen_image_urls:
                    seen_image_urls.add(ru)
                    stats["images_review"] += 1
                    new_cur.execute('''
                        INSERT INTO place_images (cafe_id, source_type, is_owner_photo, author_name, review_id, image_url, high_res_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (cid, "review", 0, r.get("name"), rid, ru, to_high_res_url(ru)))

        # Attributes (additionalInfo)
        add_info = d.get("additionalInfo") or {}
        for grp, items in add_info.items():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        for attr_name, val in item.items():
                            stats["attributes"] += 1
                            new_cur.execute('''
                                INSERT OR IGNORE INTO place_attributes (cafe_id, group_name, attribute_name, value)
                                VALUES (?, ?, ?, ?)
                            ''', (cid, grp, attr_name, 1 if val else 0))

        # Opening hours
        hours = d.get("openingHours") or []
        for h in hours:
            if isinstance(h, dict) and "day" in h:
                stats["opening_hours"] += 1
                new_cur.execute('''
                    INSERT OR IGNORE INTO place_opening_hours (cafe_id, day, hours)
                    VALUES (?, ?, ?)
                ''', (cid, h.get("day"), h.get("hours")))

        # Popular times
        pop_times = d.get("popularTimesHistogram") or {}
        for day, hourly in pop_times.items():
            if isinstance(hourly, list):
                for h_item in hourly:
                    if isinstance(h_item, dict):
                        stats["popular_times"] += 1
                        new_cur.execute('''
                            INSERT OR IGNORE INTO place_popular_times (cafe_id, day, hour, occupancy_percent)
                            VALUES (?, ?, ?, ?)
                        ''', (cid, day, h_item.get("hour"), h_item.get("occupancyPercent")))

    new_conn.commit()
    new_conn.close()
    jsonl_file.close()

    old_size = os.path.getsize(OLD_DB_PATH) / (1024 * 1024)
    new_size = os.path.getsize(NEW_DB_PATH) / (1024 * 1024)
    jsonl_size = os.path.getsize(RAW_JSONL_PATH) / (1024 * 1024)

    print("\n=======================================================")
    print("MIGRATION COMPLETED SUCCESSFULLY!")
    print("=======================================================")
    print(f"Places migrated:            {stats['places']} success, {stats['not_found']} not_found")
    print(f"Images migrated:            {stats['images_cover']} cover, {stats['images_gallery']} gallery, {stats['images_review']} review (Total: {stats['images_cover'] + stats['images_gallery'] + stats['images_review']})")
    print(f"Reviews migrated:           {stats['reviews']}")
    print(f"Attributes migrated:        {stats['attributes']}")
    print(f"Opening hours migrated:     {stats['opening_hours']}")
    print(f"Popular times migrated:     {stats['popular_times']}")
    print("-------------------------------------------------------")
    print(f"Original DB Size:           {old_size:.2f} MB ({OLD_DB_PATH})")
    print(f"New Clean Relational DB:    {new_size:.2f} MB ({NEW_DB_PATH})")
    print(f"Raw JSONL Archive:          {jsonl_size:.2f} MB ({RAW_JSONL_PATH})")
    print("=======================================================\n")

if __name__ == "__main__":
    migrate()
