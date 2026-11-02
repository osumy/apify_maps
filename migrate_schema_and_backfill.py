"""
Schema Migration & Backfill Script
===================================
Adds new venue & review attributes to google_maps.db and backfills them
directly from existing raw extraction payloads in google_maps_raw.jsonl.
"""

import os
import json
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "google_maps.db")
JSONL_PATH = os.path.join(DATA_DIR, "google_maps_raw.jsonl")

NEW_PLACE_COLUMNS = [
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

NEW_REVIEW_COLUMNS = [
    ("review_url", "TEXT"),
    ("reviewer_url", "TEXT"),
    ("review_context", "TEXT"),
    ("context_order_type", "TEXT"),
    ("context_meal_type", "TEXT"),
    ("context_price_per_person", "TEXT"),
    ("context_noise_level", "TEXT"),
    ("text_translated", "TEXT"),
    ("translated_language", "TEXT"),
    ("detailed_room_rating", "REAL"),
    ("detailed_location_rating", "REAL"),
]

def migrate_and_backfill(db_path=DB_PATH, jsonl_path=JSONL_PATH):
    print(f"Opening database: {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. Apply ALTER TABLE additions for places
    existing_place_cols = set(r[1] for r in cur.execute("PRAGMA table_info(places)").fetchall())
    for col_name, col_type in NEW_PLACE_COLUMNS:
        if col_name not in existing_place_cols:
            print(f"Adding column '{col_name}' ({col_type}) to places...")
            cur.execute(f"ALTER TABLE places ADD COLUMN {col_name} {col_type}")

    # 2. Apply ALTER TABLE additions for place_reviews
    existing_rev_cols = set(r[1] for r in cur.execute("PRAGMA table_info(place_reviews)").fetchall())
    for col_name, col_type in NEW_REVIEW_COLUMNS:
        if col_name not in existing_rev_cols:
            print(f"Adding column '{col_name}' ({col_type}) to place_reviews...")
            cur.execute(f"ALTER TABLE place_reviews ADD COLUMN {col_name} {col_type}")

    conn.commit()

    if not os.path.exists(jsonl_path):
        print(f"Raw JSONL archive not found at {jsonl_path}. Schema updated without backfill.")
        conn.close()
        return

    print(f"Reading raw entries from {jsonl_path} for backfill...")
    places_updated = 0
    reviews_updated = 0

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("status") != "success" or not entry.get("data"):
                continue

            cid = entry["cafe_id"]
            data = entry["data"]

            # Parse reviews distribution
            rev_dist = data.get("reviewsDistribution") or {}
            rev_dist_str = json.dumps(rev_dist, ensure_ascii=False) if rev_dist else None
            star1 = rev_dist.get("oneStar")
            star2 = rev_dist.get("twoStar")
            star3 = rev_dist.get("threeStar")
            star4 = rev_dist.get("fourStar")
            star5 = rev_dist.get("fiveStar")

            # Parse contacts & socials
            emails = json.dumps(data.get("emails", []), ensure_ascii=False) if data.get("emails") else None
            phones = json.dumps(data.get("phones", []), ensure_ascii=False) if data.get("phones") else None
            instagrams = json.dumps(data.get("instagrams", []), ensure_ascii=False) if data.get("instagrams") else None
            facebooks = json.dumps(data.get("facebooks", []), ensure_ascii=False) if data.get("facebooks") else None
            twitters = json.dumps(data.get("twitters", []), ensure_ascii=False) if data.get("twitters") else None
            
            # Combine social profile lists if any
            soc_profiles = {}
            for sp_key in ["instagramProfiles", "facebookProfiles", "twitterProfiles", "youtubeProfiles", "tiktokProfiles"]:
                if data.get(sp_key):
                    soc_profiles[sp_key] = data[sp_key]
            soc_profiles_str = json.dumps(soc_profiles, ensure_ascii=False) if soc_profiles else None

            # Update place
            cur.execute("""
                UPDATE places SET
                    neighborhood = ?,
                    located_in = ?,
                    floor = ?,
                    menu = ?,
                    owner_description = ?,
                    plus_code = ?,
                    kgmid = ?,
                    reviews_distribution = ?,
                    reviews_distribution_1_star = ?,
                    reviews_distribution_2_star = ?,
                    reviews_distribution_3_star = ?,
                    reviews_distribution_4_star = ?,
                    reviews_distribution_5_star = ?,
                    emails = ?,
                    phones = ?,
                    instagrams = ?,
                    facebooks = ?,
                    twitters = ?,
                    social_profiles = ?
                WHERE cafe_id = ?
            """, (
                data.get("neighborhood"),
                data.get("locatedIn"),
                data.get("floor"),
                data.get("menu"),
                data.get("ownerDescription"),
                data.get("plusCode"),
                data.get("kgmid"),
                rev_dist_str,
                star1, star2, star3, star4, star5,
                emails, phones, instagrams, facebooks, twitters, soc_profiles_str,
                cid
            ))
            places_updated += cur.rowcount

            # Update reviews for this place
            for r in data.get("reviews", []):
                rid = r.get("reviewId")
                if not rid:
                    continue

                ctx = r.get("reviewContext") or {}
                ctx_str = json.dumps(ctx, ensure_ascii=False) if ctx else None
                order_type = ctx.get("نوع سفارش") or ctx.get("Dine in") or ctx.get("Takeout")
                meal_type = ctx.get("وعده غذایی") or ctx.get("Meal")
                price_pp = ctx.get("قیمت برای هر نفر") or ctx.get("Price per person")
                noise = ctx.get("سطح سروصدا") or ctx.get("Noise level")

                det_rating = r.get("reviewDetailedRating") or {}
                room_rating = det_rating.get("اتاق") or det_rating.get("Room")
                loc_rating = det_rating.get("مکان") or det_rating.get("Location")

                cur.execute("""
                    UPDATE place_reviews SET
                        review_url = ?,
                        reviewer_url = ?,
                        review_context = ?,
                        context_order_type = ?,
                        context_meal_type = ?,
                        context_price_per_person = ?,
                        context_noise_level = ?,
                        text_translated = ?,
                        translated_language = ?,
                        detailed_room_rating = ?,
                        detailed_location_rating = ?
                    WHERE review_id = ?
                """, (
                    r.get("reviewUrl"),
                    r.get("reviewerUrl"),
                    ctx_str,
                    order_type,
                    meal_type,
                    price_pp,
                    noise,
                    r.get("textTranslated"),
                    r.get("translatedLanguage"),
                    room_rating,
                    loc_rating,
                    rid
                ))
                reviews_updated += cur.rowcount

    conn.commit()
    conn.close()
    print(f"Migration complete! Enriched {places_updated} places and {reviews_updated} reviews in {db_path}.")

if __name__ == "__main__":
    migrate_and_backfill()
