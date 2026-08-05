"""Focused tests for the coordinate-based N26 extractor."""
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "n26" / "extract.py"
SPEC = importlib.util.spec_from_file_location("n26_extract", MODULE_PATH)
extract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extract)


def word(left, top, text, page=1):
    return extract.Word(page, left, top, 10, text)


def header(top=130, page=1):
    return [word(45, top, "Beschreibung", page),
            word(342, top, "Verbuchungsdatum", page),
            word(515, top, "Betrag", page)]


def block(top, name, date, amount, extras=(), page=1):
    """One transaction block. The name sits 2pt above its own date and amount."""
    words = [word(44, top, part, page) for part in name.split()]
    words += [word(391, top + 2, date, page), word(500, top + 2, amount, page)]
    for offset, line in enumerate(extras, start=1):
        words += [word(44, top + 14 * offset, part, page) for part in line.split()]
    return words


def summary_page(opening, outgoing, incoming, closing, page=2, title="Zusammenfassung",
                 fees=None):
    """The summary page; values sit ~1pt above their labels.

    Personal statements head it "Zusammenfassung"; joint ones "Übersicht" and add a
    "Davon Gebühren" sub-line under Ausgehende.
    """
    words = [word(44, 35, title, page)]
    rows = (("Dein alter Kontostand", opening, 153),
            ("Ausgehende Transaktionen", outgoing, 179),
            ("Einkommende Transaktionen", incoming, 205),
            ("Dein neuer Kontostand", closing, 232))
    for label, value, top in rows:
        words += [word(44 + 30 * i, top, part, page) for i, part in enumerate(label.split())]
        words += [word(505, top - 1, value, page)]
    if fees is not None:
        words += [word(44, 192, "Davon", page), word(87, 192, "Gebühren", page),
                  word(510, 192, fees, page)]
    return words


class ExtractorTests(unittest.TestCase):
    def test_german_money_with_euro_suffix(self):
        self.assertEqual(extract.money_cents("-4.475,00€"), -447500)
        self.assertEqual(extract.money_cents("+2.125,00€"), 212500)
        self.assertEqual(extract.money_cents("0,00€"), 0)

    def test_full_block_yields_category_iban_and_reference(self):
        words = header() + block(157, "Max Mustermann", "08.01.2025", "-50,00€", extras=(
            "Belastungen",
            "IBAN: DE11111111111111111111 • BIC: BANKDEFFXXX",
            "Sent from N26",
            "Wertstellung 08.01.2025",
        ))
        txns, issues, fatal = extract.parse_transactions(words)
        self.assertEqual((issues, fatal), ([], []))
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0]["date"], "2025-01-08")
        self.assertEqual(txns[0]["amount_cents"], -5000)
        self.assertEqual(txns[0]["counterparty"], "Max Mustermann")
        self.assertEqual(txns[0]["counterparty_iban"], "DE11111111111111111111")
        self.assertEqual(txns[0]["purpose"], "N26 / Belastungen · Sent from N26")

    def test_minimal_block_without_category_or_iban(self):
        words = header() + block(157, "An Jane & John", "14.01.2025", "-1.300,00€",
                                 extras=("Wertstellung 14.01.2025",))
        txns, _, fatal = extract.parse_transactions(words)
        self.assertEqual(fatal, [])
        self.assertEqual(txns[0]["counterparty"], "An Jane & John")
        self.assertEqual(txns[0]["counterparty_iban"], "")
        self.assertEqual(txns[0]["purpose"], "N26")

    def test_wertstellung_never_becomes_the_booking_date(self):
        # The value date is dropped; the Verbuchungsdatum column is authoritative.
        words = header() + block(157, "Someone", "08.01.2025", "-50,00€",
                                 extras=("Wertstellung 09.01.2025",))
        txns, _, _ = extract.parse_transactions(words)
        self.assertEqual(txns[0]["date"], "2025-01-08")
        self.assertNotIn("Wertstellung", txns[0]["purpose"])

    def test_blocks_do_not_bleed_into_each_other(self):
        words = header()
        words += block(157, "First Payer", "08.01.2025", "-50,00€",
                       extras=("Belastungen", "Sent from N26", "Wertstellung 08.01.2025"))
        words += block(244, "Second Payer", "09.01.2025", "+2.125,00€",
                       extras=("Gutschriften", "Wedding Gifts", "Wertstellung 09.01.2025"))
        txns, _, fatal = extract.parse_transactions(words)
        self.assertEqual(fatal, [])
        self.assertEqual([t["counterparty"] for t in txns], ["First Payer", "Second Payer"])
        self.assertEqual(txns[0]["purpose"], "N26 / Belastungen · Sent from N26")
        self.assertEqual(txns[1]["purpose"], "N26 / Gutschriften · Wedding Gifts")

    def test_summary_is_read_from_its_own_page(self):
        words = header() + summary_page("+50,00€", "-4.475,00€", "+4.425,00€", "0,00€")
        summary = extract.statement_summary(words)
        self.assertEqual(summary["opening_cents"], 5000)
        self.assertEqual(summary["outgoing_cents"], -447500)
        self.assertEqual(summary["incoming_cents"], 442500)
        self.assertEqual(summary["closing_cents"], 0)

    def test_a_missing_summary_is_refused(self):
        with self.assertRaises(ValueError):
            extract.statement_summary(header())

    def test_summary_page_amounts_are_not_read_as_transactions(self):
        # The summary page has no Verbuchungsdatum header, so it carries no rows.
        words = summary_page("0,00€", "0,00€", "0,00€", "0,00€")
        txns, _, _ = extract.parse_transactions(words)
        self.assertEqual(txns, [])

    def test_month_with_no_activity_extracts_as_zero_transactions(self):
        # Six of twelve real statements look like this; it must not be an error.
        words = header() + summary_page("+1.600,00€", "0,00€", "0,00€", "+1.600,00€")
        txns, issues, fatal = extract.parse_transactions(words)
        self.assertEqual((txns, issues, fatal), ([], [], []))
        summary = extract.statement_summary(words)
        self.assertEqual(summary["closing_cents"] - summary["opening_cents"], 0)


class JointAccountTests(unittest.TestCase):
    """The Gemeinschaftskonto statement is the same bank with a different template."""

    def test_summary_page_titled_uebersicht_is_accepted(self):
        words = summary_page("+45,00€", "-755,00€", "+5.425,00€", "+4.715,00€",
                             title="Übersicht", fees="0,00€")
        summary = extract.statement_summary(words)
        self.assertEqual(summary["opening_cents"], 4500)
        self.assertEqual(summary["closing_cents"], 471500)

    def test_davon_gebuehren_is_not_mistaken_for_a_balance(self):
        # It sits between Ausgehende and Einkommende and matches no label.
        words = summary_page("+45,00€", "-755,00€", "+5.425,00€", "+4.715,00€",
                             title="Übersicht", fees="-99,00€")
        summary = extract.statement_summary(words)
        self.assertEqual(summary["outgoing_cents"], -75500)
        self.assertEqual(summary["incoming_cents"], 542500)

    def test_wider_description_column_is_calibrated_from_the_header(self):
        # Joint statements put Verbuchungsdatum at 359 and let descriptions reach
        # 348; a split tuned to the personal layout (340) would truncate them.
        words = [word(60, 194, "Beschreibung"), word(359, 194, "Verbuchungsdatum"),
                 word(506, 194, "Betrag")]
        words += [word(60, 212, "VersWerk"), word(300, 212, "Musterstadt"),
                  word(340, 212, "Beispiel"),
                  word(359, 212, "03.01.2025"), word(494, 212, "-755,00€"),
                  word(60, 226, "Wertstellung"), word(113, 226, "03.01.2025")]
        txns, _, fatal = extract.parse_transactions(words)
        self.assertEqual(fatal, [])
        self.assertEqual(txns[0]["counterparty"], "VersWerk Musterstadt Beispiel")
        self.assertEqual(txns[0]["amount_cents"], -75500)

    def test_last_block_stops_at_wertstellung_not_at_the_footer(self):
        # The joint footer starts at 702, above the personal one at 762, so a fixed
        # cut-off pulls the address block and the account's own IBAN into the text.
        words = header()
        words += block(157, "Von Main Account", "14.01.2025", "+1.300,00€",
                       extras=("Wertstellung 14.01.2025",))
        words += [word(44, 702, "MAX"), word(90, 702, "MUSTERMANN"),
                  word(200, 702, "Kontotyp:"), word(260, 702, "Gemeinschaftskonto"),
                  word(44, 719, "IBAN:"), word(85, 719, "DE22222222222222222222")]
        txns, _, fatal = extract.parse_transactions(words)
        self.assertEqual(fatal, [])
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0]["purpose"], "N26")
        self.assertEqual(txns[0]["counterparty_iban"], "")


class BalanceAnchorTests(unittest.TestCase):
    """The statement's own balances become checkpoints the doctor re-verifies."""

    def test_anchors_bracket_the_period(self):
        summary = extract.statement_summary(
            header() + summary_page("+50,00€", "-4.475,00€", "+4.425,00€", "0,00€"))
        summary["period_text"] = "01.01.2025 - 31.01.2025"
        anchors = extract.balance_anchors(summary)
        # The old balance predates the period, so a transaction on day one falls
        # inside the span rather than on its boundary.
        self.assertEqual(anchors, [
            {"date": "2024-12-31", "balance": 50.0},
            {"date": "2025-01-31", "balance": 0.0},
        ])

    def test_no_anchors_without_a_readable_period(self):
        summary = extract.statement_summary(
            header() + summary_page("0,00€", "0,00€", "0,00€", "0,00€"))
        summary["period_text"] = ""
        self.assertEqual(extract.balance_anchors(summary), [])


if __name__ == "__main__":
    unittest.main()
