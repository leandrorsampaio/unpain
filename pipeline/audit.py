"""Why today's figures differ from the ones you reviewed.

`closings` already knows *that* a settled period moved: it fingerprints every money
line and reports when the fingerprint changes. What it cannot say is what moved, and
"the digest is different" is not an answer anybody can act on. This module keeps the
lines themselves at recognisable moments — a close, a successful import, a successful
backup — and compares them, so the answer is "this row's category changed from X to Y,
by rule R" rather than a changed hash.

Deliberately a *checkpoint comparison*, not a mutation journal (plan Decision 4). The
two answer different questions:

  checkpoint comparison — what is different now versus that reviewed moment?
  mutation journal      — which actions happened, in what order, and by whom?

The first is the financial question, and it fits a plain-file local app: a handful of
snapshots per year, no instrumentation of every write path, no event ordering, no
identity. The second needs all of that and is a separate project. Expanding this one
into it quietly would be the worst of both.

A checkpoint is evidence and only evidence. Nothing here is ever read to compute a
total, and a snapshot that disagrees with a fresh recomputation loses.
"""
import hashlib
from datetime import datetime, timezone

from . import settle, store
from .util import cents, read_json, write_json, year_dir

# The stored shape. Bumped independently of closings.DIGEST_VERSION, because the two
# answer to different needs: one is about what is *watched*, this is about what can be
# *explained*. A snapshot from an older version is compared for what it does hold and
# reports reduced coverage for the rest — never invented drift.
SNAPSHOT_VERSION = 1

# Rolling checkpoints kept per year, plus one per active close. Everything else is
# pruned: "since the last import" needs the last import, not every import ever
# (plan Decision 6).
ROLLING_KINDS = ("import", "backup")

# What makes two lines the same line financially. A change in any of these is a change
# a total can feel; `counterparty` is deliberately outside it, so renaming a merchant
# is reported as presentation rather than as money moving.
FINANCIAL_FIELDS = ("date", "account", "amount_cents", "category", "sharing",
                    "income_owner", "year_cost", "tax_bucket", "kind", "transfer_partner")


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def semantic_lines(year, month=None):
    """Every money line the totals see, keyed so it can be found again.

    Built with settle.money_lines/part_view rather than by re-reading split fields:
    reimplementing that inheritance would let "What changed?" and the dashboard
    disagree about what a split part means, and then the explanation of a number would
    contradict the number.
    """
    out = {}
    # Which fields a human set by hand, read from the decisions file. The effective view
    # deliberately does not carry this — it is the merged answer, and by then "the
    # category is X" no longer remembers whether a person or a rule decided that. The
    # difference is the whole point of the report: "you changed this" and "the rule that
    # categorises this changed" call for completely different reactions.
    decisions = store.decisions(year)
    for txn in store.effective_year(year):
        if month is not None and int(txn["date"][5:7]) != int(month):
            continue
        decision_fields = sorted(k for k, v in (decisions.get(txn.get("id")) or {}).items()
                                 if v is not None)
        for index, (_, part) in enumerate(settle.money_lines(txn)):
            view = settle.part_view(txn, part)
            amount = part["amount"] if part else txn.get("amount_eur")
            key = "%s:%d" % (txn.get("id"), index)
            out[key] = {
                "line_key": key,
                "transaction_id": txn.get("id"),
                "part_index": index,
                "date": txn.get("date"),
                "account": txn.get("account"),
                "amount_cents": cents(amount or 0),
                "category": view["category"],
                "sharing": view["sharing"],
                "income_owner": txn.get("income_owner"),
                "year_cost": bool(view["year_cost"]),
                "tax_bucket": view["tax_bucket"],
                "kind": txn.get("kind") or "normal",
                "transfer_partner": txn.get("transfer_partner"),
                # Kept so a row that no longer exists can still be described. After a
                # deletion the store cannot supply this, and "one line was removed"
                # without saying which is not an explanation.
                "counterparty": txn.get("counterparty") or "",
                "source_file": (txn.get("source") or {}).get("file"),
                "source_upload": (txn.get("source") or {}).get("upload"),
                "matched_rule": (txn.get("matched_rule") or {}).get("id")
                if isinstance(txn.get("matched_rule"), dict) else txn.get("matched_rule"),
                # Field provenance, not the decision itself: enough to say "you changed
                # the category" versus "the rule that categorises this changed".
                "decision_fields": decision_fields,
            }
    return out


def line_digest(lines):
    """One fingerprint over the canonical line representation.

    `closings` used to build its own row strings from the same resolver. Two
    definitions of "the watched money line" is one too many — they would drift, and
    the drift alarm and its explanation would then disagree about whether anything
    happened.
    """
    rows = []
    for key in sorted(lines):
        line = lines[key]
        rows.append("|".join(str(line.get(field) or "") for field in
                             ("transaction_id", "part_index", "date", "account",
                              "amount_cents", "category", "sharing", "year_cost",
                              "tax_bucket", "income_owner", "kind", "counterparty")))
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:16]


def semantic_snapshot(year, month=None):
    """The lines plus the figures they produce, in integer cents."""
    lines = semantic_lines(year, month)
    summary = (settle.month_summary(year, month) if month is not None
               else settle.year_summary(year))
    settlement = settle.settlement(year, month)
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "year": int(year),
        "month": int(month) if month is not None else None,
        "lines": lines,
        "digest": line_digest(lines),
        "figures": {
            "income_cents": cents(summary["income"]),
            "expenses_cents": cents(summary["expenses"]),
            "savings_cents": cents(summary["savings"]),
            "transactions": summary["transactions"],
            "by_category_cents": {name: cents(value)
                                  for name, value in summary["by_category"].items()},
        },
        "settlement": {
            "ratio": settlement["ratio"],
            "total_shared_cents": cents(settlement["total_shared_expenses"]),
            "paid_cents": {person: cents(value) for person, value in settlement["paid"].items()},
            "fair_share_cents": {person: cents(value)
                                 for person, value in settlement["fair_share"].items()},
            "balances_cents": {person: cents(value)
                               for person, value in settlement["balances"].items()},
            "transfer": settlement["transfer"],
        },
    }


# ---------------------------------------------------------------- storage

def path(year):
    return year_dir(year) / "audit-checkpoints.json"


def load_checkpoints(year):
    stored = read_json(path(year), default={"version": 1, "checkpoints": {}})
    found = stored.get("checkpoints")
    return found if isinstance(found, dict) else {}


def save_checkpoints(year, checkpoints):
    write_json(path(year), {"version": 1, "checkpoints": checkpoints})


def checkpoint(year, kind, *, period=None, label=None, metadata=None, month=None, when=None):
    """Record where the figures stood, under a slot name.

    Close checkpoints are keyed by their period so each active close keeps its own.
    Import and backup are rolling: one per year, the latest, because "since the last
    import" needs exactly that one.
    """
    slot = ("close:%s" % period) if kind == "close" else "last-%s" % kind
    checkpoints = load_checkpoints(year)
    checkpoints[slot] = {
        "id": slot,
        "kind": kind,
        "created_at": when or _utc_now(),
        "label": label or slot,
        "period": period,
        "metadata": metadata or {},
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot": semantic_snapshot(year, month),
    }
    save_checkpoints(year, checkpoints)
    return checkpoints[slot]


def drop(year, slot):
    """Forget one checkpoint — used when a close is reopened and no longer claims
    anything about the period."""
    checkpoints = load_checkpoints(year)
    if checkpoints.pop(slot, None) is not None:
        save_checkpoints(year, checkpoints)


def prune(year, active_closes=()):
    """Keep the rolling checkpoints and the closes that are still closed."""
    keep = {"last-%s" % kind for kind in ROLLING_KINDS}
    keep |= {"close:%s" % period for period in active_closes}
    checkpoints = load_checkpoints(year)
    trimmed = {slot: value for slot, value in checkpoints.items() if slot in keep}
    if trimmed != checkpoints:
        save_checkpoints(year, trimmed)
    return trimmed


def available_baselines(year):
    """What a person can compare against, newest first."""
    return sorted(
        ({"id": item["id"], "kind": item["kind"], "created_at": item["created_at"],
          "label": item["label"], "period": item.get("period"),
          "snapshot_version": item.get("snapshot_version"),
          "metadata": item.get("metadata") or {}}
         for item in load_checkpoints(year).values()),
        key=lambda item: (item["created_at"], item["id"]), reverse=True)


# ---------------------------------------------------------------- diffing

def diff_figures(before, after):
    """Old, new and delta for every figure — in integer cents, and including
    settlement. A comparison that reports income and expenses but not who owes whom
    misses the figure the household actually acts on."""
    out = []

    def add(group, name, old, new):
        if old != new:
            out.append({"group": group, "name": name, "old_cents": old, "new_cents": new,
                        "delta_cents": (new or 0) - (old or 0)})

    for name in ("income_cents", "expenses_cents", "savings_cents"):
        add("totals", name, before["figures"].get(name), after["figures"].get(name))
    if before["figures"].get("transactions") != after["figures"].get("transactions"):
        out.append({"group": "totals", "name": "transactions",
                    "old_cents": before["figures"].get("transactions"),
                    "new_cents": after["figures"].get("transactions"),
                    "delta_cents": (after["figures"].get("transactions") or 0)
                    - (before["figures"].get("transactions") or 0)})
    categories = set(before["figures"].get("by_category_cents", {})) | \
        set(after["figures"].get("by_category_cents", {}))
    for name in sorted(categories):
        add("category", name, before["figures"]["by_category_cents"].get(name, 0),
            after["figures"]["by_category_cents"].get(name, 0))

    old_settle, new_settle = before.get("settlement", {}), after.get("settlement", {})
    add("settlement", "total_shared_cents", old_settle.get("total_shared_cents"),
        new_settle.get("total_shared_cents"))
    for field in ("paid_cents", "fair_share_cents", "balances_cents"):
        for person in sorted(set(old_settle.get(field, {})) | set(new_settle.get(field, {}))):
            add("settlement", "%s.%s" % (field, person),
                old_settle.get(field, {}).get(person), new_settle.get(field, {}).get(person))
    return out


def _classify(before, after):
    """What kind of change this is, in the words a person would use."""
    changed = [field for field in FINANCIAL_FIELDS if before.get(field) != after.get(field)]
    kinds = []
    if {"amount_cents", "date"} & set(changed):
        kinds.append("amount_or_date_changed")
    if {"account", "income_owner"} & set(changed):
        kinds.append("account_or_owner_changed")
    if {"category", "sharing", "year_cost", "tax_bucket"} & set(changed):
        kinds.append("classification_changed")
    if {"kind", "transfer_partner"} & set(changed):
        kinds.append("transfer_changed")
    if before.get("source_upload") != after.get("source_upload") or \
            before.get("source_file") != after.get("source_file"):
        kinds.append("source_changed")
        changed.append("source_file")
    if not kinds and before.get("counterparty") != after.get("counterparty"):
        # The money is identical and only the label moved. Saying so is the difference
        # between "nothing financial happened" and an unexplained entry in the list.
        kinds.append("presentation_only")
        changed.append("counterparty")
    return kinds, changed


def diff_lines(before, after):
    """Added, removed and changed money lines, one entry per transaction."""
    added, removed, changed = [], [], []
    for key in sorted(set(after) - set(before)):
        added.append(dict(after[key], change="added"))
    for key in sorted(set(before) - set(after)):
        # Described from the baseline, because the store can no longer describe it.
        removed.append(dict(before[key], change="removed"))
    for key in sorted(set(before) & set(after)):
        kinds, fields = _classify(before[key], after[key])
        if not kinds:
            continue
        changed.append({
            "line_key": key,
            "transaction_id": after[key]["transaction_id"],
            "change": "changed",
            "kinds": kinds,
            "fields": sorted(set(fields)),
            "before": {field: before[key].get(field) for field in sorted(set(fields))},
            "after": {field: after[key].get(field) for field in sorted(set(fields))},
            "counterparty": after[key].get("counterparty"),
            "date": after[key].get("date"),
            "amount_cents": after[key].get("amount_cents"),
            "matched_rule": after[key].get("matched_rule"),
            "decision_fields": after[key].get("decision_fields"),
        })
    return {"added": added, "removed": removed, "changed": changed}


def _split_summary(before, after):
    """A transaction whose parts changed is one event, not several.

    Splitting a 180 EUR order into two parts removes one line and adds two. Reported
    literally that is three unrelated items and a reader has to reassemble what
    happened; reported as one parent it is 'this was split in two'.
    """
    def parts(lines):
        counts = {}
        for line in lines.values():
            counts[line["transaction_id"]] = counts.get(line["transaction_id"], 0) + 1
        return counts

    before_parts, after_parts = parts(before), parts(after)
    out = []
    for txn_id in sorted(set(before_parts) & set(after_parts)):
        if before_parts[txn_id] != after_parts[txn_id]:
            out.append({"transaction_id": txn_id,
                        "parts_before": before_parts[txn_id],
                        "parts_after": after_parts[txn_id]})
    return out


def compare(year, checkpoint_id):
    """The full comparison against one stored checkpoint."""
    checkpoints = load_checkpoints(year)
    stored = checkpoints.get(checkpoint_id)
    if not stored:
        return None
    baseline = stored["snapshot"]
    month = baseline.get("month")
    current = semantic_snapshot(year, month)

    before_lines = baseline.get("lines") or {}
    lines = diff_lines(before_lines, current["lines"])
    figures = diff_figures(baseline, current)
    splits = _split_summary(before_lines, current["lines"])

    sources_before = {line.get("source_upload") for line in before_lines.values()
                      if line.get("source_upload")}
    sources_after = {line.get("source_upload") for line in current["lines"].values()
                     if line.get("source_upload")}
    settlement_changed = any(item["group"] == "settlement" for item in figures)
    return {
        "year": int(year),
        "baseline": {"id": stored["id"], "kind": stored["kind"],
                     "created_at": stored["created_at"], "label": stored["label"],
                     "period": stored.get("period")},
        "scope": {"period": stored.get("period"), "month": month},
        # A snapshot written before a field existed can be compared for what it holds
        # and no further. Saying so beats reporting its absence as a change.
        "reduced_coverage": stored.get("snapshot_version") != SNAPSHOT_VERSION,
        "summary": {
            "added": len(lines["added"]),
            "removed": len(lines["removed"]),
            "changed": len(lines["changed"]),
            "financial_delta_cents": current["figures"]["savings_cents"]
            - baseline["figures"]["savings_cents"],
            "settlement_changed": settlement_changed,
            # Two compensating edits can leave every total identical while the lines
            # underneath both moved. The digest is what notices.
            "digest_changed": baseline.get("digest") != current.get("digest"),
        },
        "figure_changes": figures,
        "source_changes": {"added": sorted(sources_after - sources_before),
                           "removed": sorted(sources_before - sources_after)},
        "split_changes": splits,
        "line_changes": lines,
    }
