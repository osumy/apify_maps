import os
import json
import sqlite3
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
import io
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NEW_DB_PATH = os.path.join(BASE_DIR, "google_maps.db")
OLD_DB_PATH = os.path.join(BASE_DIR, "google_maps_raw.db")
DB_PATH = NEW_DB_PATH if os.path.exists(NEW_DB_PATH) else OLD_DB_PATH

def get_100_urls():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # Check if place_images table exists
    has_place_images = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='place_images'").fetchone()
    if has_place_images:
        rows = cur.execute("SELECT high_res_url, image_url FROM place_images LIMIT 200").fetchall()
        conn.close()
        urls = []
        seen = set()
        for hr, raw in rows:
            u = hr or raw
            if u and ("googleusercontent.com" in u or "ggpht.com" in u):
                base = u.rsplit("=", 1)[0] if "=" in u else u
                if base not in seen:
                    seen.add(base)
                    urls.append(base)
                    if len(urls) >= 100:
                        return urls
        return urls

    rows = cur.execute("SELECT image_urls, high_res_image_urls FROM google_places_raw").fetchall()
    conn.close()

    urls = []
    seen = set()
    for r in rows:
        raw = r[1] or r[0]
        if raw:
            try:
                arr = json.loads(raw)
                for u in arr:
                    if u and ("googleusercontent.com" in u or "ggpht.com" in u):
                        base = u.rsplit("=", 1)[0] if "=" in u else u
                        if base not in seen:
                            seen.add(base)
                            urls.append(base)
                            if len(urls) >= 100:
                                return urls
            except Exception:
                pass
    return urls

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

VARIANTS = {
    "s0": "=s0",
    "s0-rw": "=s0-rw",
    "s0-rw-q90": "=s0-rw-q90",
    "s0-rw-l90": "=s0-rw-l90"
}

def fetch_image_variants(session, base_url, index):
    result = {
        "index": index,
        "base_url": base_url,
        "variants": {},
        "success": True,
        "error": None
    }
    for key, suffix in VARIANTS.items():
        url = base_url + suffix
        try:
            resp = session.get(url, headers=HEADERS, timeout=25)
            if resp.status_code == 200:
                content_len = len(resp.content)
                ctype = resp.headers.get("Content-Type", "")
                
                # Verify format & resolution
                try:
                    img = Image.open(io.BytesIO(resp.content))
                    fmt = img.format
                    width, height = img.size
                except Exception as e:
                    fmt = "UNKNOWN"
                    width, height = (0, 0)
                
                result["variants"][key] = {
                    "status": resp.status_code,
                    "bytes": content_len,
                    "content_type": ctype,
                    "format": fmt,
                    "resolution": [width, height]
                }
            else:
                result["variants"][key] = {
                    "status": resp.status_code,
                    "bytes": 0,
                    "error": f"HTTP {resp.status_code}"
                }
                result["success"] = False
        except Exception as e:
            result["variants"][key] = {
                "status": 0,
                "bytes": 0,
                "error": str(e)
            }
            result["success"] = False
            
    return result

def main():
    urls = get_100_urls()
    print(f"Extracted {len(urls)} unique base image URLs.")
    
    results = []
    start_time = time.time()
    
    # Run with thread pool
    max_workers = 15
    print(f"Starting benchmark on 100 images with {max_workers} worker threads...")
    
    session = requests.Session()
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_image_variants, session, url, i+1): i for i, url in enumerate(urls)}
        completed = 0
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            completed += 1
            if completed % 10 == 0 or completed == len(urls):
                print(f"Progress: {completed}/{len(urls)} images processed...")

    # Sort results by index
    results.sort(key=lambda x: x["index"])
    
    # Save raw results
    output_json = os.path.join(BASE_DIR, "benchmark_results.json")
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
        
    duration = time.time() - start_time
    print(f"\nBenchmark completed in {duration:.1f} seconds!")
    
    # Analyze results
    valid_results = [r for r in results if r["success"] and all(k in r["variants"] and r["variants"][k]["bytes"] > 0 for k in VARIANTS)]
    print(f"\nSuccessfully downloaded all 4 variants for {len(valid_results)}/{len(urls)} images.")
    
    if not valid_results:
        print("No valid results to analyze.")
        return

    totals = {k: sum(r["variants"][k]["bytes"] for r in valid_results) for k in VARIANTS}
    
    print("\n" + "="*70)
    print("TOTAL VOLUMES (for valid images):")
    print(f"JPEG (=s0)         : {totals['s0'] / (1024*1024):.2f} MB (100.0%)")
    print(f"WebP (=s0-rw)      : {totals['s0-rw'] / (1024*1024):.2f} MB ({totals['s0-rw']/totals['s0']*100:.1f}%) | Savings: {(1 - totals['s0-rw']/totals['s0'])*100:.1f}%")
    print(f"WebP (=s0-rw-q90)  : {totals['s0-rw-q90'] / (1024*1024):.2f} MB ({totals['s0-rw-q90']/totals['s0']*100:.1f}%) | Savings: {(1 - totals['s0-rw-q90']/totals['s0'])*100:.1f}%")
    print(f"WebP (=s0-rw-l90)  : {totals['s0-rw-l90'] / (1024*1024):.2f} MB ({totals['s0-rw-l90']/totals['s0']*100:.1f}%) | Difference: {(totals['s0-rw-l90']/totals['s0'] - 1)*100:+.1f}%")
    print("="*70)
    
    # Check if s0-rw and s0-rw-q90 are byte-identical across all images
    identical_count = sum(1 for r in valid_results if r["variants"]["s0-rw"]["bytes"] == r["variants"]["s0-rw-q90"]["bytes"])
    print(f"Images where '=s0-rw' and '=s0-rw-q90' are EXACTLY identical in bytes: {identical_count}/{len(valid_results)}")

    # Check resolutions
    res_match_count = sum(1 for r in valid_results if r["variants"]["s0"]["resolution"] == r["variants"]["s0-rw"]["resolution"] == r["variants"]["s0-rw-l90"]["resolution"])
    print(f"Images where resolution is 100% preserved identically: {res_match_count}/{len(valid_results)}")

if __name__ == "__main__":
    main()
