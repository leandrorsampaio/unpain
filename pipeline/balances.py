"""The year grid behind Settings › Balances: what every account was worth at each month end.

A balance is only evidence when it comes from the bank, so this module keeps two kinds of
number strictly apart. A **recorded** balance is an anchor a human or a statement put there;
a **derived** balance is what the ledger computes from the last recorded one. Derived numbers
are shown so you can compare them against your banking app, never to be adopted as recorded —
adopting them would turn every cell green and prove nothing (see the reconciliation gate).

One cell owns one calendar month and shows the last anchor recorded inside it, even when that
anchor does not sit on the month end (a statement dated the 29th is still December's balance).
Otherwise the cell derives the month-end level. The opening column is December of the year
before, because a year's opening balance and the previous year's closing balance are one number,
not two.
"""
from datetime import date as date_type, timedelta

from . import anchors, networth, store
from .util import cents, load_accounts


def _month_end(year, month):
    nxt = date_type(year + 1, 1, 1) if month == 12 else date_type(year, month + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


def periods(year):
    """The 13 columns of the grid: last December, then every month end of `year`."""
    year = int(year)
    out = [{"key": "opening", "month": 0, "date": _month_end(year - 1, 12),
            "start": "%d-12-01" % (year - 1)}]
    for month in range(1, 13):
        out.append({"key": "%d-%02d" % (year, month), "month": month,
                    "date": _month_end(year, month), "start": "%d-%02d-01" % (year, month)})
    return out


def _cell(period, anchor, derived, span, today):
    """One account-month: the recorded balance if there is one, else what the ledger derives."""
    cell = {"key": period["key"], "date": period["date"], "derived": derived}
    if anchor is not None:
        # Deriving a level at a date that HAS an anchor just echoes that anchor back — the
        # reconstruction snaps to it. What is worth showing next to a recorded figure is what
        # the ledger reached carrying forward from the PREVIOUS anchor, which the span holds.
        cell["derived"] = None if not span else round((cents(anchor["balance"]) + span["actual_cents"]
                                                       - span["expected_cents"]) / 100.0, 2)
        cell.update({
            "status": "ok" if (span or {}).get("ok") is True
                      else "mismatch" if (span or {}).get("ok") is False else "recorded",
            "balance": anchor["balance"],
            "anchor_date": anchor["date"],
            "source": anchor.get("source"),
            "kind": anchor.get("kind"),
        })
        if span:
            cell["span_from"] = span["from"]
            cell["diff_cents"] = span["actual_cents"] - span["expected_cents"]
            if span.get("reason"):
                cell["reason"] = span["reason"]
        return cell
    # No balance was ever recorded, so nothing anchors the arithmetic — an unknown level,
    # which is a different answer from "no movement" and must not read as zero.
    cell["status"] = "derived" if derived is not None else ("future" if period["date"] > today else "unknown")
    cell["balance"] = None
    return cell


def grid(year, today=None):
    """Every account × every month end of `year`, each cell recorded or derived."""
    year = int(year)
    today = today or date_type.today().isoformat()
    accounts, _ = load_accounts()
    cols = periods(year)
    raw_by_year = {y: store.load_year_raw(y) for y in store.years()}

    rows = []
    for aid, account in accounts.items():
        anchor_rows = anchors._all_anchors(aid)
        txns = sorted((t for year_rows in raw_by_year.values() for t in year_rows
                       if t.get("account") == aid), key=lambda t: t.get("date", ""))
        spans = {s["to"]: s for s in
                 anchors.verify_loaded(aid, account, anchor_rows, raw_by_year)}

        cells = []
        for period in cols:
            inside = [a for a in anchor_rows if period["start"] <= a["date"] <= period["date"]]
            anchor = inside[-1] if inside else None
            derived = None
            if period["date"] <= today:
                native = networth._native_at(period["date"], anchor_rows, txns)
                derived = None if native is None else round(native, 2)
            cells.append(_cell(period, anchor, derived, spans.get(anchor["date"]) if anchor else None, today))

        recorded = sum(1 for c in cells if c.get("anchor_date"))
        rows.append({
            "id": aid,
            "label": account.get("label") or aid,
            "currency": (account.get("currency") or "EUR").upper(),
            "type": account.get("type"),
            "in_networth": (account.get("type") or "").lower() not in networth.EXCLUDED_TYPES,
            "recorded": recorded,
            "cells": cells,
        })

    rows.sort(key=lambda r: (not r["in_networth"], r["label"].lower()))
    return {"year": year, "today": today, "periods": cols, "accounts": rows,
            "totals": _totals(rows, cols)}


def _totals(rows, cols):
    """Per-column EUR-account totals — a sanity line, not the net-worth figure.

    Deliberately narrow: only accounts that count toward net worth, and only EUR ones, because
    a column mixing currencies would need a rate per cell and stops being a column of one thing.
    A month where some account has no figure still gets its sum, but carries `covered` of
    `accounts` so the UI can mark it partial. Hiding the number entirely taught the reader
    nothing; printing it as if it were whole would be a lie.
    """
    out = []
    eligible = [r for r in rows if r["in_networth"] and r["currency"] == "EUR"]
    for index, period in enumerate(cols):
        values = [r["cells"][index]["balance"] if r["cells"][index]["balance"] is not None
                  else r["cells"][index]["derived"] for r in eligible]
        known = [v for v in values if v is not None]
        out.append({"key": period["key"],
                    "total": round(sum(cents(v) for v in known) / 100.0, 2) if known else None,
                    "covered": len(known), "accounts": len(eligible),
                    "complete": bool(known) and len(known) == len(eligible)})
    return out
