"""Focused tests for the coordinate-based Trade Republic extractor."""
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "trade_republic" / "extract.py"
SPEC = importlib.util.spec_from_file_location("trade_republic_extract", MODULE_PATH)
extract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extract)


def word(left, top, text):
    return extract.Word(1, left, top, 10, text)


class ExtractorTests(unittest.TestCase):
    def test_german_money_uses_integer_cents(self):
        self.assertEqual(extract.money_cents("1.234,56"), 123456)

    def test_multiline_german_date(self):
        words = [word(74, 100, "03"), word(74, 108, "Feb."), word(74, 116, "2025")]
        self.assertEqual(extract.parse_date(words), "2025-02-03")

    def test_short_german_month_variants(self):
        for token, expected in (("Sep.", "2025-09-03"), ("Mrz", "2025-03-03")):
            words = [word(74, 100, "03"), word(74, 108, token), word(74, 116, "2025")]
            self.assertEqual(extract.parse_date(words), expected)

    def test_allow_review_keeps_balance_proven_incomplete_row(self):
        words = [
            word(501, 50, "SALDO"),
            word(74, 75, "03"), word(74, 83, "Feb."), word(74, 91, "2025"),
            word(101, 79, "Kartentransaktion"),
            word(430, 79, "5,00"), word(480, 79, "95,00"),
        ]
        strict, issues, fatal, _ = extract.parse_transactions(words, 10000, allow_review=False)
        self.assertEqual(strict, [])
        self.assertTrue(issues)
        self.assertTrue(fatal)

        reviewed, issues, fatal, last = extract.parse_transactions(words, 10000, allow_review=True)
        self.assertEqual(last, 9500)
        self.assertEqual(len(reviewed), 1)
        self.assertEqual(reviewed[0]["amount_cents"], -500)
        self.assertTrue(reviewed[0]["force_review"])
        self.assertIn("EXTRACTION REVIEW", reviewed[0]["purpose"])
        self.assertEqual(fatal, [])


    def test_unsigned_printed_amount_corroborates_a_debit(self):
        # Annual statements print bare magnitudes and let the running balance carry
        # direction, so a debit shows as "7,50" against a delta of -7.50.
        self.assertTrue(extract.amount_agrees("7,50", -750))
        self.assertTrue(extract.amount_agrees("7,50", 750))

    def test_printed_amount_of_the_wrong_size_still_disagrees(self):
        self.assertFalse(extract.amount_agrees("7,50", -751))
        self.assertFalse(extract.amount_agrees("1.234,56", -750))

    def test_a_signed_printed_amount_is_still_checked_signed(self):
        # Where the statement does give a sign, keep using it: a printed -5,00
        # against a rising balance is a genuine disagreement, not a magnitude match.
        self.assertTrue(extract.amount_agrees("-5,00", -500))
        self.assertFalse(extract.amount_agrees("-5,00", 500))

    def test_debit_row_without_a_minus_sign_parses_strictly(self):
        words = [
            word(501, 50, "SALDO"),
            word(74, 75, "03"), word(74, 83, "Feb."), word(74, 91, "2025"),
            word(101, 79, "Kartentransaktion"),
            word(160, 79, "OSIANDER"),
            word(430, 79, "5,00"), word(480, 79, "95,00"),
        ]
        txns, issues, fatal, last = extract.parse_transactions(words, 10000, allow_review=False)
        self.assertEqual(issues, [])
        self.assertEqual(fatal, [])
        self.assertEqual(last, 9500)
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0]["amount_cents"], -500)
        self.assertFalse(txns[0]["force_review"])


if __name__ == "__main__":
    unittest.main()
