"""Focused tests for the coordinate-based Banco Rendimento extractor."""
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "banco_rendimento" / "extract.py"
SPEC = importlib.util.spec_from_file_location("banco_rendimento_extract", MODULE_PATH)
extract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extract)


def word(left, top, text, page=1):
    return extract.Word(page, left, top, 10, text)


def row(top, doc, description, amount, balance, page=1):
    """One statement line: document, description, Valor, Saldo."""
    words = [word(60.1, top, doc, page)]
    left = 121.0
    for token in description.split():
        words.append(word(left, top, token, page))
        left += 30
    words.append(word(453.9, top, amount, page))
    words.append(word(499.7, top, balance, page))
    return words


class ExtractorTests(unittest.TestCase):
    def test_brazilian_money_uses_integer_cents(self):
        self.assertEqual(extract.money_cents("1.234,56"), 123456)
        self.assertEqual(extract.money_cents("-40,00"), -4000)
        self.assertEqual(extract.money_cents("10.000,00"), 1000000)

    def test_wide_balance_is_still_in_the_saldo_column(self):
        # Both money columns are right-aligned; a ten-thousands balance starts at
        # 494.6, which an over-tight split at 495 would drop entirely.
        words = [word(60.1, 100, "02/01/2025")] + [
            word(60.1, 120, "1000001"), word(121.0, 120, "PACOTE"),
            word(458.9, 120, "-29,90"), word(494.6, 120, "10.000,00"),
        ]
        txns, issues, fatal, _ = extract.parse_transactions(words)
        self.assertEqual(fatal, [])
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0]["amount_cents"], -2990)
        self.assertEqual(txns[0]["balance_cents"], 1000000)

    def test_date_section_carries_across_a_page_break(self):
        # The header sits on page 1; its last row spills onto page 2.
        words = [word(60.1, 700, "24/11/2025", page=1)]
        words += row(720, "AAA111", "First row", "-12,00", "2.900,00", page=1)
        words += row(48, "BBB222", "Spilled row", "-40,00", "2.860,00", page=2)
        txns, issues, fatal, _ = extract.parse_transactions(words)
        self.assertEqual(fatal, [])
        self.assertEqual([t["date"] for t in txns], ["2025-11-24", "2025-11-24"])

    def test_wrapped_description_reads_in_visual_order(self):
        # The continuation line starts at the left margin, so sorting on `left`
        # alone would put it ahead of the words that precede it on the line above.
        words = [word(60.1, 100, "24/11/2025")]
        words += [word(121.0, 120, "Env"), word(150.0, 120, "Pgto"), word(200.0, 120, "AUTOS"),
                  word(60.1, 126, "ABC1234"),
                  word(453.9, 126, "-40,00"), word(499.7, 126, "2.860,00"),
                  word(121.0, 132, "LTDA")]
        txns, _, fatal, _ = extract.parse_transactions(words)
        self.assertEqual(fatal, [])
        self.assertEqual(txns[0]["counterparty"], "Env Pgto AUTOS LTDA")

    def test_saldo_final_is_a_checkpoint_not_a_transaction(self):
        words = [word(60.1, 100, "29/12/2025")]
        words += row(120, "1234567", "Rec Pgto", "100,00", "1.100,00")
        words += [word(121.0, 140, "Saldo"), word(148.1, 140, "Final"),
                  word(499.7, 140, "1.100,00")]
        txns, _, fatal, checkpoints = extract.parse_transactions(words)
        self.assertEqual(len(txns), 1)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["balance_cents"], 110000)

    def test_chain_orders_sections_newest_first_but_rows_oldest_first(self):
        # Two dates, newest section printed first, two rows inside the newer one.
        words = [word(60.1, 100, "29/12/2025")]
        words += row(120, "A", "Later one", "100,00", "1.100,00")
        words += row(140, "B", "Later two", "180,00", "1.280,00")
        words += [word(60.1, 200, "05/12/2025")]
        words += row(220, "C", "Earlier", "-200,00", "1.000,00")
        txns, _, fatal, _ = extract.parse_transactions(words)
        self.assertEqual(fatal, [])
        opening, closing, breaks = extract.verify_chain(txns)
        self.assertEqual(breaks, [])
        self.assertEqual(opening, 120000)     # 1.000,00 + 200,00
        self.assertEqual(closing, 128000)
        self.assertEqual(opening + sum(t["amount_cents"] for t in txns), closing)

    def test_a_broken_balance_chain_is_reported(self):
        words = [word(60.1, 100, "05/12/2025")]
        words += row(120, "A", "One", "-10,00", "100,00")
        words += row(140, "B", "Two", "-10,00", "85,00")   # should be 90,00
        txns, _, _, _ = extract.parse_transactions(words)
        _, _, breaks = extract.verify_chain(txns)
        self.assertEqual(len(breaks), 1)
        self.assertIn("does not follow", breaks[0])

    def test_row_without_a_date_section_is_fatal_in_strict_mode(self):
        words = row(120, "A", "Orphan", "-10,00", "100,00")
        txns, issues, fatal, _ = extract.parse_transactions(words, allow_review=False)
        self.assertEqual(txns, [])
        self.assertTrue(fatal)


if __name__ == "__main__":
    unittest.main()
