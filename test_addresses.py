import json
import unittest
from unittest.mock import MagicMock, patch

import addresses


def street(name="Маршала Тита", city="Elemir", county="Central Banat Administrative District", countrycode="RS", **extra):
    return {"properties": {"name": name, "city": city, "county": county, "countrycode": countrycode,
                           "osm_key": "highway", "type": "street", "postcode": "23208", **extra}}


class AddressTests(unittest.TestCase):
    def setUp(self):
        addresses._cache.clear()

    def test_shorthand_and_serbian_latin(self):
        self.assertEqual(addresses.normalize_query("mars tit 2c elem"), ("Maršala Tita Elemir", "2C"))
        self.assertEqual(addresses.normalize_query("марш тит 2ц елем"), ("Maršala Tita Elemir", "2C"))
        matches = addresses.suggestions([street("Трг маршала Тита"), street()], "Maršala Tita Elemir", "2C")
        self.assertEqual(matches[0]["label"], "Maršala Tita, Elemir, 2C")
        self.assertFalse(matches[0]["house_number_verified"])

    def test_numbered_streets_and_postcodes(self):
        self.assertEqual(addresses.normalize_query("2. oktobra 8 elem"), ("2. oktobra Elemir", "8"))
        self.assertEqual(addresses.normalize_query("2 oktobra 8 elem"), ("2 oktobra Elemir", "8"))
        self.assertEqual(addresses.normalize_query("mars tit 2c elem 23208")[1], "2C")
        self.assertEqual(addresses.normalize_query("mars tit 2/3 elem"), ("Maršala Tita Elemir", "2/3"))

    def test_banat_priority_dedup_and_country(self):
        matches = addresses.suggestions([
            street(city="Novi Sad", county="South Bačka"),
            street(countrycode="RO"), street(), street(),
            street(housenumber="2C"),
        ], "Maršala Tita", "2C")
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0]["city"], "Elemir")
        self.assertEqual(matches[0]["region"], "Banat, Serbia")
        self.assertEqual(matches[1]["city"], "Novi Sad")

    @patch("addresses.time.sleep")
    @patch("addresses.requests.get")
    def test_query_cache_and_house_number_not_sent(self, get, sleep):
        response = MagicMock()
        response.status_code = 200
        response.iter_content.return_value = [json.dumps({"features": [street()]}).encode()]
        get.return_value.__enter__.return_value = response
        self.assertEqual(addresses.search("mars tit 2c elem")[0]["label"], "Maršala Tita, Elemir, 2C")
        self.assertEqual(addresses.search("mars tit 3d elem")[0]["label"], "Maršala Tita, Elemir, 3D")
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["params"]["q"], "Maršala Tita Elemir")
        self.assertEqual(get.call_args.kwargs["params"]["countrycode"], "RS")

    @patch("addresses.time.sleep")
    @patch("addresses.requests.get")
    def test_provider_errors_are_explicit(self, get, sleep):
        response = MagicMock()
        response.status_code = 503
        get.return_value.__enter__.return_value = response
        with self.assertRaises(addresses.AddressUnavailable):
            addresses.search("mars tit elem")
        response.status_code = 200
        response.iter_content.return_value = [b'{"features":[{"properties":{"countrycode":42}}]}']
        with self.assertRaises(addresses.AddressUnavailable):
            addresses.search("mars tit elem")

    def test_query_validation(self):
        for query in (None, [], "ab", "a" * 201):
            with self.assertRaises(ValueError):
                addresses.search(query)


if __name__ == "__main__":
    unittest.main()
