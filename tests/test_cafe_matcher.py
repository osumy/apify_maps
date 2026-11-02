import os
import sys
import unittest

# Ensure root directory is on sys.path for direct script execution
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cafe_matcher import (
    haversine_distance,
    compute_distance_score,
    normalize_text,
    compute_name_similarity,
    normalize_phone,
    check_phone_match,
    check_city_mismatch,
    evaluate_cafe_match,
    select_best_candidate,
    MatchResult
)

class TestCafeMatcher(unittest.TestCase):

    def test_haversine_distance(self):
        # Tehran coords distance
        # ~100m north
        dist = haversine_distance(35.7000, 51.4000, 35.7009, 51.4000)
        self.assertTrue(90 <= dist <= 110)

        # Same point
        self.assertEqual(haversine_distance(35.7000, 51.4000, 35.7000, 51.4000), 0.0)

        # None values
        self.assertEqual(haversine_distance(None, 51.4000, 35.7000, 51.4000), float('inf'))

    def test_compute_distance_score(self):
        self.assertEqual(compute_distance_score(15.0), 1.0)
        self.assertTrue(compute_distance_score(65.0) > 0.8)
        self.assertTrue(compute_distance_score(150.0) < 0.8)
        self.assertTrue(compute_distance_score(220.0) < 0.4)
        self.assertEqual(compute_distance_score(260.0), 0.0)
        self.assertEqual(compute_distance_score(1000.0), 0.0)

    def test_name_similarity(self):
        # Direct Persian
        self.assertEqual(compute_name_similarity('کافه ویش', 'کافه ویش'), 1.0)
        self.assertEqual(compute_name_similarity('کافه رستوران 13', 'کافه رستوران ۱۳'), 1.0)

        # Cross-lingual / Phonetic
        self.assertTrue(compute_name_similarity('کافه رستوران عباس آقا', 'Abbasagha Restaurant') >= 0.90)
        self.assertTrue(compute_name_similarity('بل کافه', 'Bell cafe') >= 0.90)
        self.assertTrue(compute_name_similarity('کافه سلوبون', 'Cafe celebon') >= 0.90)
        self.assertTrue(compute_name_similarity('کافه پرسنت', 'Percent coffee') >= 0.90)
        self.assertTrue(compute_name_similarity('مستر قهوه', 'MR Coffee') >= 0.90)
        self.assertTrue(compute_name_similarity('کافه گستو', 'کافه Gusto') >= 0.90)

        # Containment
        self.assertTrue(compute_name_similarity('کافه کارزین(زعفرانیه)', 'Karzin cafe') >= 0.80)

        # Complete Mismatches (different venues)
        self.assertTrue(compute_name_similarity('کافه مستقل', 'کافه ۱۳۷۲') < 0.30)
        self.assertTrue(compute_name_similarity('رستوران تولوما', 'Lidoma Restaurant') < 0.70)

    def test_phone_matching(self):
        self.assertTrue(check_phone_match('09201365715', '+98 920 136 5715'))
        self.assertTrue(check_phone_match('02165012734', '+98 21 6501 2734'))
        self.assertTrue(check_phone_match('65012734', '+98 21 6501 2734'))
        self.assertFalse(check_phone_match('02165012734', '+98 21 2214 3462'))

    def test_city_mismatch(self):
        self.assertTrue(check_city_mismatch('تهران', 'خرمشهر، استان خوزستان', 'خرمشهر'))
        self.assertFalse(check_city_mismatch('تهران', 'تهران، خیابان ولیعصر', 'تهران'))

    def test_evaluate_cafe_match_strict_threshold(self):
        source = {
            "name": "کافه ویش",
            "latitude": 35.6971561,
            "longitude": 51.4069172,
            "city": "تهران",
            "phone": "09201365715",
            "instagram": "https://www.instagram.com/cafe.wish"
        }

        # True Match (Distance 10m, exact title, phone match)
        good_cand = {
            "title": "کافه ویش",
            "location": {"lat": 35.6972445, "lng": 51.4069256},
            "address": "Tehran, Nofel Loshato Street, Razi street",
            "city": "تهران",
            "phone": "+98 920 136 5715",
            "website": "https://www.instagram.com/cafe.wish?igsh=abc"
        }
        res = evaluate_cafe_match(source, good_cand, threshold=0.80)
        self.assertTrue(res.is_match)
        self.assertEqual(res.status, "matched")
        self.assertTrue(res.composite_score >= 0.80)
        self.assertTrue(res.phone_matched)

        # Distance Veto (Distance 7km, even if name is identical)
        far_cand = {
            "title": "کافه ویش",
            "location": {"lat": 35.7829683, "lng": 51.3763277},
            "address": "Tehran, Saadat Abad",
            "city": "تهران",
            "phone": "+98 21 2214 3462"
        }
        res_far = evaluate_cafe_match(source, far_cand, threshold=0.80)
        self.assertFalse(res_far.is_match)
        self.assertEqual(res_far.status, "rejected")
        self.assertEqual(res_far.composite_score, 0.0)

        # Name Mismatch Veto at close distance (15m, but totally different venue)
        diff_name_cand = {
            "title": "داروخانه دکتر راد",
            "location": {"lat": 35.6972445, "lng": 51.4069256},
            "address": "Tehran, Nofel Loshato Street",
            "city": "تهران"
        }
        res_diff = evaluate_cafe_match(source, diff_name_cand, threshold=0.80)
        self.assertFalse(res_diff.is_match)
        self.assertEqual(res_diff.status, "rejected")

    def test_select_best_candidate(self):
        source = {
            "name": "بل کافه",
            "latitude": 35.7500,
            "longitude": 51.4100,
            "city": "تهران"
        }

        # 3 candidates:
        # 1. Sponsored restaurant 2.5km away
        # 2. 'Bell cafe' 15m away (True match)
        # 3. Kiosk 30m away
        candidates = [
            {
                "title": "رستوران لوکس طلایی",
                "location": {"lat": 35.7700, "lng": 51.4200},
                "address": "تهران"
            },
            {
                "title": "Bell cafe",
                "location": {"lat": 35.7501, "lng": 51.4101},
                "address": "تهران"
            },
            {
                "title": "دکه مطبوعات",
                "location": {"lat": 35.7502, "lng": 51.4102},
                "address": "تهران"
            }
        ]

        best_cand, best_res = select_best_candidate(source, candidates, threshold=0.80)
        self.assertIsNotNone(best_cand)
        self.assertEqual(best_cand["title"], "Bell cafe")
        self.assertTrue(best_res.is_match)
        self.assertTrue(best_res.composite_score >= 0.80)

if __name__ == '__main__':
    unittest.main()
