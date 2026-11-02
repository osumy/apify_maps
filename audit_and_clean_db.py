"""
Database Audit & Cleansing Script
=================================
Scans existing places in google_maps.db and evaluates them against
the strict matching engine (distance decay <= 250m, name similarity, threshold 0.80).
Can flag or remove false matches and their associated images/reviews.
"""

import os
import sys
import re
import sqlite3
import argparse
from cafe_matcher import evaluate_cafe_match, haversine_distance

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
SOURCE_DB_PATH = os.getenv("SOURCE_DB_PATH", r"c:\Users\amira\Desktop\crawler\cafes_cafe_shard00.db")

def audit_database(output_db=OUTPUT_DB_PATH, source_db=SOURCE_DB_PATH, threshold=0.80, apply_changes=False):
    if not os.path.exists(output_db):
        print(f"Error: Output database not found at {output_db}")
        return

    # Load source cafes info if available
    source_cafes = {}
    if os.path.exists(source_db):
        s_conn = sqlite3.connect(source_db)
        s_conn.row_factory = sqlite3.Row
        s_cur = s_conn.cursor()
        for r in s_cur.execute("SELECT id, name, latitude, longitude, city, address, phone, instagram, website FROM cafes_cafe"):
            source_cafes[r["id"]] = dict(r)
        s_conn.close()
        print(f"Loaded {len(source_cafes)} cafes from source DB.")

    conn = sqlite3.connect(output_db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    url_coords_pattern = re.compile(r'/@([0-9.]+),([0-9.]+),')

    rows = cur.execute("""
        SELECT cafe_id, original_name, title, latitude, longitude, search_url,
               address, city, phone, website, status, name_match_score
        FROM places
        WHERE status = 'success'
    """).fetchall()

    total_checked = len(rows)
    passed_count = 0
    rejected_count = 0
    over_250m = 0
    over_1km = 0
    to_reject_ids = []

    print(f"\nAuditing {total_checked} 'success' venues with threshold={threshold}...\n")

    for r in rows:
        cid = r["cafe_id"]
        src = source_cafes.get(cid)

        # Fallback to search_url coords if source DB not available
        if not src:
            m = url_coords_pattern.search(r["search_url"] or "")
            src_lat = float(m.group(1)) if m else None
            src_lng = float(m.group(2)) if m else None
            src = {
                "id": cid,
                "name": r["original_name"],
                "latitude": src_lat,
                "longitude": src_lng,
                "city": None,
                "phone": None,
                "instagram": None,
                "website": None
            }

        cand = {
            "title": r["title"],
            "location": {"lat": r["latitude"], "lng": r["longitude"]},
            "address": r["address"],
            "city": r["city"],
            "phone": r["phone"],
            "website": r["website"]
        }

        res = evaluate_cafe_match(src, cand, threshold=threshold)

        if res.distance_meters > 250:
            over_250m += 1
        if res.distance_meters > 1000:
            over_1km += 1

        if res.is_match:
            passed_count += 1
        else:
            rejected_count += 1
            to_reject_ids.append((cid, r["original_name"], r["title"], res.distance_meters, res.composite_score, res.rejection_reason))

    print("=" * 60)
    print("AUDIT SUMMARY:")
    print("=" * 60)
    print(f"Total Places Evaluated:     {total_checked}")
    print(f"Valid Matches (Strict):     {passed_count} ({passed_count/max(1, total_checked)*100:.1f}%)")
    print(f"False Matches Flagged:      {rejected_count} ({rejected_count/max(1, total_checked)*100:.1f}%)")
    print(f"Places > 250m away:         {over_250m} ({over_250m/max(1, total_checked)*100:.1f}%)")
    print(f"Places > 1000m away:        {over_1km} ({over_1km/max(1, total_checked)*100:.1f}%)")
    print("=" * 60)

    print("\nSample of Flagged False Matches (Top 15):")
    for item in to_reject_ids[:15]:
        cid, orig, title, dist, score, reason = item
        print(f"ID {cid:5d} | Dist: {dist:7.1f}m | Score: {score:.2f} | '{orig}' vs '{title}' | Reason: {reason}")

    if apply_changes and to_reject_ids:
        print(f"\n[APPLYING CHANGES] Updating {len(to_reject_ids)} false matches...")
        ids_tuple = [x[0] for x in to_reject_ids]
        cur.executemany("""
            UPDATE places 
            SET status = 'false_match_rejected'
            WHERE cafe_id = ?
        """, [(cid,) for cid in ids_tuple])

        # Delete bad images and reviews so they are not served
        cur.executemany("DELETE FROM place_images WHERE cafe_id = ?", [(cid,) for cid in ids_tuple])
        cur.executemany("DELETE FROM place_reviews WHERE cafe_id = ?", [(cid,) for cid in ids_tuple])
        cur.executemany("DELETE FROM place_attributes WHERE cafe_id = ?", [(cid,) for cid in ids_tuple])
        cur.executemany("DELETE FROM place_opening_hours WHERE cafe_id = ?", [(cid,) for cid in ids_tuple])
        cur.executemany("DELETE FROM place_popular_times WHERE cafe_id = ?", [(cid,) for cid in ids_tuple])

        conn.commit()
        print(f"Successfully cleaned {len(to_reject_ids)} false matches from active database.")
    elif not apply_changes:
        print("\nDry run completed. Run with --apply to commit these cleanups.")

    conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit and clean false cafe matches in google_maps.db")
    parser.add_argument("--threshold", type=float, default=0.80, help="Strict composite score threshold (default 0.80)")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup changes to the database")
    args = parser.parse_args()

    audit_database(threshold=args.threshold, apply_changes=args.apply)
