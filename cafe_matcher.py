import math
import re
from dataclasses import dataclass
from typing import Optional, List, Tuple
from difflib import SequenceMatcher
from urllib.parse import urlparse

STOP_WORDS = {
    'کافه', 'رستوران', 'کافی', 'شاپ', 'قهوه', 'بار', 'لانژ', 'فست', 'فود',
    'cafe', 'café', 'restaurant', 'coffee', 'shop', 'bar', 'lounge', 'fast', 'food', 'bakery'
}

PERSIAN_TO_LATIN = {
    'آ': 'a', 'ا': 'a', 'ب': 'b', 'پ': 'p', 'ت': 't', 'ث': 's',
    'ج': 'j', 'چ': 'ch', 'ح': 'h', 'خ': 'kh', 'د': 'd', 'ذ': 'z',
    'ر': 'r', 'ز': 'z', 'ژ': 'zh', 'س': 's', 'ش': 'sh', 'ص': 's',
    'ض': 'z', 'ط': 't', 'ظ': 'z', 'ع': 'a', 'غ': 'gh', 'ف': 'f',
    'ق': 'gh', 'ک': 'k', 'گ': 'g', 'ل': 'l', 'م': 'm', 'ن': 'n',
    'و': 'o', 'ه': 'h', 'ی': 'i', 'ي': 'i', 'ك': 'k', 'ة': 'h',
    'ء': '', 'ئ': 'i', 'ؤ': 'o'
}

COMMON_TRANSLATIONS = {
    'مستر': 'mr', 'دوباره': 'dobiro', 'ناین': 'nine', 'بل': 'bell',
    'بیک': 'bake', 'پرسنت': 'percent', 'آیبیک': 'ibake', 'آواکو': 'avako',
    'بناک': 'bonak', 'آویژه': 'avizhe', 'سلوبون': 'celebon', 'گستو': 'gusto',
    'اگومان': 'agoman', 'کارزین': 'karzin', 'عباس': 'abbas', 'اقا': 'agha',
    'میلاد': 'milad', 'دنیز': 'deniz', 'روژه': 'rojeh', 'ویش': 'wish',
    'لیار': 'leymer', 'دیگر': 'andorra'
}

MAJOR_IRANIAN_CITIES = {
    'تهران', 'اصفهان', 'مشهد', 'شیراز', 'تبریز', 'اهواز', 'کرج', 'قم',
    'رشت', 'کرمانشاه', 'ارومیه', 'زاهدان', 'یزد', 'قزوین', 'خرمشهر', 'آبادان',
    'ساری', 'گرگان', 'همدان', 'بندرعباس', 'بوشهر', 'زنجان', 'سنندج'
}

@dataclass
class MatchResult:
    is_match: bool
    status: str                  # 'matched', 'needs_review', 'rejected'
    composite_score: float
    distance_meters: float
    distance_score: float
    name_score: float
    phone_matched: bool
    web_matched: bool
    rejection_reason: Optional[str] = None

def haversine_distance(lat1, lon1, lat2, lon2) -> float:
    """Calculates haversine distance in meters."""
    if None in (lat1, lon1, lat2, lon2):
        return float('inf')
    try:
        lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
    except (ValueError, TypeError):
        return float('inf')

    R = 6371000.0  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0)**2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return R * c

def compute_distance_score(dist_meters: float, max_cutoff: float = 250.0) -> float:
    """
    Computes a continuous distance score between 0.0 and 1.0.
    <= 30m: 1.0
    30m - 100m: decays from 1.0 to 0.8
    100m - 200m: decays from 0.8 to 0.4
    200m - 250m: decays from 0.4 to 0.1
    > 250m: 0.0 (Hard cutoff)
    """
    if dist_meters is None or dist_meters < 0:
        return 0.0
    if dist_meters <= 30.0:
        return 1.0
    if dist_meters <= 100.0:
        return 1.0 - 0.2 * ((dist_meters - 30.0) / 70.0)
    if dist_meters <= 200.0:
        return 0.8 - 0.4 * ((dist_meters - 100.0) / 100.0)
    if dist_meters <= max_cutoff:
        return 0.4 - 0.3 * ((dist_meters - 200.0) / (max_cutoff - 200.0))
    return 0.0

def normalize_text(text: str) -> str:
    """Normalize Persian/Arabic characters, numbers, and remove diacritics."""
    if not text:
        return ""
    t = text.lower()
    # Normalize Arabic/Persian letters
    t = t.replace('ي', 'ی').replace('ك', 'ک').replace('ة', 'ه').replace('ۀ', 'ه')
    t = t.replace('آ', 'ا').replace('أ', 'ا').replace('إ', 'ا').replace('ؤ', 'و').replace('ئ', 'ی')
    # Remove diacritics
    t = re.sub(r'[\u064B-\u065F\u0670]', '', t)
    # Digits mapping
    digits_map = {
        '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4', '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9',
        '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4', '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9'
    }
    for k, v in digits_map.items():
        t = t.replace(k, v)
    # Replace punctuation and special characters with space
    t = re.sub(r'[\(\)\[\]\{\}\-_,\.\+:;/\&\\\|\'\"`!?]', ' ', t)
    return ' '.join(t.split())

def clean_tokens(text: str) -> List[str]:
    norm = normalize_text(text)
    tokens = norm.split()
    filtered = [w for w in tokens if w not in STOP_WORDS]
    return filtered if filtered else tokens

def to_phonetic_latin(tokens: List[str]) -> List[str]:
    latin_tokens = []
    for tok in tokens:
        if tok in COMMON_TRANSLATIONS:
            latin_tokens.append(COMMON_TRANSLATIONS[tok])
            continue
        if all(ord(c) < 128 for c in tok):
            latin_tokens.append(tok)
            continue
        res = ''.join(PERSIAN_TO_LATIN.get(c, c) for c in tok)
        res = re.sub(r'[^a-z0-9]', '', res)
        # Collapse repeated consecutive chars (e.g. bb -> b)
        res = re.sub(r'(.)\1+', r'\1', res)
        if res:
            latin_tokens.append(res)
    return latin_tokens

def compute_name_similarity(name1: str, name2: str) -> float:
    """
    Computes a multi-faceted similarity score between two names:
    1. Direct Persian normalized string ratio
    2. Phonetic Latin transliteration ratio
    3. Token overlap / containment
    """
    if not name1 or not name2:
        return 0.0

    tok1 = clean_tokens(name1)
    tok2 = clean_tokens(name2)

    # 1. Direct Persian text ratio
    s1 = ' '.join(tok1)
    s2 = ' '.join(tok2)
    direct_ratio = SequenceMatcher(None, s1, s2).ratio()

    # 2. Phonetic Latin ratio
    lat1 = to_phonetic_latin(tok1)
    lat2 = to_phonetic_latin(tok2)
    lat_s1 = ''.join(lat1)
    lat_s2 = ''.join(lat2)
    phonetic_ratio = SequenceMatcher(None, lat_s1, lat_s2).ratio() if (lat_s1 and lat_s2) else 0.0

    # 3. Token containment
    set1, set2 = set(lat1), set(lat2)
    if set1 and set2:
        overlap = len(set1 & set2) / min(len(set1), len(set2))
        # If smaller name is completely inside the larger name
        containment = 1.0 if (set1.issubset(set2) or set2.issubset(set1)) else overlap
    else:
        containment = 0.0

    return round(max(direct_ratio, phonetic_ratio, containment * 0.9), 2)

def normalize_phone(phone: str) -> Optional[str]:
    """Normalize phone to national 10-digit number."""
    if not phone:
        return None
    digits = re.sub(r'\D', '', str(phone))
    if digits.startswith('98') and len(digits) > 10:
        digits = '0' + digits[2:]
    elif not digits.startswith('0') and len(digits) == 10:
        digits = '0' + digits
    return digits if len(digits) >= 8 else None

def check_phone_match(phone1: Optional[str], phone2: Optional[str]) -> bool:
    p1 = normalize_phone(phone1)
    p2 = normalize_phone(phone2)
    if p1 and p2:
        return p1 == p2 or p1[-8:] == p2[-8:]
    return False

def extract_web_handle(url: Optional[str]) -> Optional[str]:
    """Extracts username or domain signature from instagram/web URL."""
    if not url:
        return None
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        netloc = parsed.netloc.lower()
        path = parsed.path.strip("/").lower()
        if "instagram.com" in netloc:
            handle = path.split("/")[0] if path else ""
            return handle.split("?")[0] if handle else None
        # for standard websites, return core domain (without www or tld)
        parts = netloc.replace("www.", "").split(".")
        return parts[0] if parts else None
    except Exception:
        return None

def check_web_match(url1: Optional[str], url2: Optional[str]) -> bool:
    h1 = extract_web_handle(url1)
    h2 = extract_web_handle(url2)
    if h1 and h2 and len(h1) >= 3 and len(h2) >= 3:
        return h1 == h2 or h1 in h2 or h2 in h1
    return False

def check_city_mismatch(source_city: Optional[str], place_address: Optional[str], place_city: Optional[str]) -> bool:
    """Detect if the returned address is definitively in another province/city."""
    if not source_city:
        return False
    src_norm = normalize_text(source_city)
    addr_norm = normalize_text(f"{place_address or ''} {place_city or ''}")

    for city in MAJOR_IRANIAN_CITIES:
        if city != src_norm and city in addr_norm and src_norm not in addr_norm:
            return True
    return False

def evaluate_cafe_match(
    source_cafe: dict,
    candidate_place: dict,
    threshold: float = 0.80
) -> MatchResult:
    """
    Evaluates a candidate Google Maps place against the source cafe.
    Applies strict veto gates and composite scoring.
    """
    src_lat = source_cafe.get("latitude")
    src_lng = source_cafe.get("longitude")
    src_name = source_cafe.get("name") or ""
    src_city = source_cafe.get("city") or ""
    src_phone = source_cafe.get("phone")
    src_insta = source_cafe.get("instagram")
    src_web = source_cafe.get("website")

    loc = candidate_place.get("location") or {}
    cand_lat = loc.get("lat")
    cand_lng = loc.get("lng")
    cand_title = candidate_place.get("title") or ""
    cand_address = candidate_place.get("address") or ""
    cand_city = candidate_place.get("city") or ""
    cand_phone = candidate_place.get("phone")
    cand_web = candidate_place.get("website")

    # 1. Geographic Distance
    dist = haversine_distance(src_lat, src_lng, cand_lat, cand_lng)
    
    # 2. Identity Signals
    phone_matched = check_phone_match(src_phone, cand_phone)
    web_matched = check_web_match(src_insta, cand_web) or check_web_match(src_web, cand_web)

    # GATE 1: Hard Veto Checks
    # Distance Cutoff: 250m max, or 350m if phone/web match
    max_allowed_dist = 350.0 if (phone_matched or web_matched) else 250.0
    if dist > max_allowed_dist:
        return MatchResult(
            is_match=False,
            status="rejected",
            composite_score=0.0,
            distance_meters=dist,
            distance_score=0.0,
            name_score=0.0,
            phone_matched=phone_matched,
            web_matched=web_matched,
            rejection_reason=f"Distance {dist:.1f}m exceeds max allowed {max_allowed_dist}m"
        )

    # City Mismatch Veto
    if check_city_mismatch(src_city, cand_address, cand_city):
        return MatchResult(
            is_match=False,
            status="rejected",
            composite_score=0.0,
            distance_meters=dist,
            distance_score=0.0,
            name_score=0.0,
            phone_matched=phone_matched,
            web_matched=web_matched,
            rejection_reason=f"City mismatch between '{src_city}' and returned address"
        )

    # 3. Individual Scores
    dist_score = compute_distance_score(dist, max_cutoff=max_allowed_dist)
    name_score = compute_name_similarity(src_name, cand_title)

    # Also check if cand_title matches instagram handle
    if not name_score or name_score < 0.6:
        src_handle = extract_web_handle(src_insta)
        if src_handle:
            handle_score = compute_name_similarity(src_handle, cand_title)
            name_score = max(name_score, handle_score)

    # GATE 2: Name Floor Veto
    # If name similarity is lower than 0.35, reject even if at exact same location
    if name_score < 0.35 and not (phone_matched or web_matched):
        return MatchResult(
            is_match=False,
            status="rejected",
            composite_score=round(dist_score * 0.5, 2),
            distance_meters=dist,
            distance_score=dist_score,
            name_score=name_score,
            phone_matched=phone_matched,
            web_matched=web_matched,
            rejection_reason=f"Name score {name_score} below minimum floor 0.35"
        )

    # 4. Composite Score Calculation
    # Base: 50% name, 50% distance
    comp_score = (0.50 * name_score) + (0.50 * dist_score)

    # Bonus: Phone match (+0.25) or Web match (+0.15)
    if phone_matched:
        comp_score += 0.25
    if web_matched:
        comp_score += 0.15

    # Cap composite score at 1.0
    comp_score = min(1.0, round(comp_score, 2))

    # GATE 3: Final Acceptance Threshold
    # Meets strict threshold (0.80) and within 200m, or phone matched within allowed radius
    is_accepted = (comp_score >= threshold and dist <= 200.0) or (phone_matched and dist <= max_allowed_dist)

    status = "matched" if is_accepted else ("needs_review" if comp_score >= 0.60 else "rejected")

    return MatchResult(
        is_match=is_accepted,
        status=status,
        composite_score=comp_score,
        distance_meters=dist,
        distance_score=dist_score,
        name_score=name_score,
        phone_matched=phone_matched,
        web_matched=web_matched,
        rejection_reason=None if is_accepted else f"Composite score {comp_score} below threshold {threshold}"
    )

def select_best_candidate(
    source_cafe: dict,
    candidate_places: List[dict],
    threshold: float = 0.80
) -> Tuple[Optional[dict], Optional[MatchResult]]:
    """
    Iterates through all returned candidate places for a search query,
    evaluates each, and selects the candidate with the highest composite score.
    Returns (best_place, best_result). If none match or candidates list is empty,
    returns (None, best_result or None).
    """
    if not candidate_places:
        return None, None

    scored_candidates = []
    for cand in candidate_places:
        res = evaluate_cafe_match(source_cafe, cand, threshold=threshold)
        scored_candidates.append((cand, res))

    # Sort descending by composite score, then by distance ascending
    scored_candidates.sort(key=lambda x: (x[1].composite_score, -x[1].distance_meters), reverse=True)

    best_cand, best_res = scored_candidates[0]

    if best_res.is_match:
        return best_cand, best_res
    else:
        return None, best_res
