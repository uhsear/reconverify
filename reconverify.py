#!/usr/bin/env python
"""Verify a no-post reconcile of a traditionally versioned table from a fresh read.

A sandbox version is reconciled with its parent and not posted, so that it
stops pinning the compress. The owner's edits must survive, nothing may reach
the parent, and the version must not fill up with rows the owner never saw.
The usual check runs in the process that ran the reconcile, reads the version
through a layer that process already holds, and can see the rows from before.

This tool never trusts that read. It rebuilds the version's view itself, from
exported copies of the state tree (SDE_states), the version list
(SDE_versions) and each table's base, adds and deletes tables. Before the
reconcile it names the owner's own edits across every state since the common
ancestor with the parent, not only the head state, and warns when the parent
reloaded rows the owner edited or edited the same rows. After the reconcile it
compares a second, separate export with the saved snapshot: the version must
hold the parent's state, hold the owner's edits, and not have been posted.

    python reconverify.py --self-test
    python reconverify.py before/ --edit-version OWNER.SANDBOX
    python reconverify.py before/ --edit-version OWNER.SANDBOX --key-field ROAD_KEY
    python reconverify.py before/ --edit-version OWNER.SANDBOX --snapshot s.json --apply
    python reconverify.py after/ --edit-version OWNER.SANDBOX --before s.json

The export is a directory of CSV files: states.csv, versions.csv, and for
each table T the files T.base.csv, T.adds.csv, T.deletes.csv and, optionally,
T.client.csv, which is the table as a client such as an arcpy layer read it.

Exit codes: 0 nothing found, 1 at least one finding, 2 a file could not be
read or written, 3 refused (the export is not a usable state tree, or the
after export is the snapshot's own read, or the snapshot is unusable), 64
usage error.
"""

from __future__ import print_function

import argparse
import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# The object id column of every exported table, unless --oid-field names one.
DEFAULT_OID_FIELD = "OBJECTID"

# How many object ids are listed after a finding. The count is the finding.
DEFAULT_SAMPLE = 10

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# Column names fixed by the geodatabase. The adds table carries the state a
# row belongs to. The deletes table names the row, the state the deleted row
# belonged to, and the state that deleted it.
STATE_FIELD = "SDE_STATE_ID"
ROW_ID_FIELD = "SDE_DELETES_ROW_ID"
DELETED_AT_FIELD = "DELETED_AT"

# The base table is state 0, and every lineage ends there.
ROOT_STATE = 0

# Written into every snapshot, and refused when absent, so a stray JSON file
# is never read as a record of the version before its reconcile.
FORMAT = "reconverify-2"

# What a snapshot must hold, at the top and in each table. A hand-edited or
# truncated file that still carries the format tag is refused, not crashed on.
SNAP_KEYS = ("version", "parent", "version_state", "parent_state", "ancestor",
             "oid_field", "key_field", "digests", "edited", "tables")
SNAP_TABLE_KEYS = ("columns", "changed", "parent", "deleted", "deleted_keys",
                   "changed_keys", "view")


# ------------------------------------------------------------- decision core

def lineage(parents, state):
    """The states from `state` back to state 0, nearest first."""
    path = []
    seen = set()
    while state != ROOT_STATE:
        if state in seen:
            raise ValueError("the state tree loops at state %d" % state)
        if state not in parents:
            raise ValueError("state %d is not in states.csv" % state)
        seen.add(state)
        path.append(state)
        state = parents[state]
    path.append(ROOT_STATE)
    return path


def common_ancestor(parents, a, b):
    """The nearest state that both lineages hold. State 0 at worst."""
    other = set(lineage(parents, b))
    return next(s for s in lineage(parents, a) if s in other)


def row_hash(row, columns):
    """One digest over every column but the object id, in a fixed order."""
    values = [row.get(c) or "" for c in columns]
    return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()


def view(name, table, states):
    """Object id -> (state, digest, key) of every row live at this lineage.

    A row is the pair (object id, state it was written at). It is live when
    its state is in the lineage and no delete made in the lineage names it.
    An update is a delete of the old pair plus a new pair, so at most one pair
    per object id is live in a consistent export.
    """
    live = set(states)
    gone = set((oid, s) for oid, s, at in table["deletes"] if at in live)
    out = {}
    for oid, s, digest, key in table["entries"]:
        if s in live and (oid, s) not in gone:
            if oid in out:
                raise ValueError("%s: object id %d is live twice, at states "
                                 "%d and %d" % (name, oid, out[oid][0], s))
            out[oid] = (s, digest, key)
    return out


def edits(ancestor_view, head_view, own_states):
    """(changed, deleted) object ids of one side since the common ancestor.

    Changed rows were written at a state of this side's own lineage.
    Deleted rows were live at the ancestor and are not live at the head.
    Both are read across every own state, not only the head state.
    """
    changed = sorted(o for o, v in head_view.items() if v[0] in own_states)
    deleted = sorted(o for o in ancestor_view if o not in head_view)
    return changed, deleted


def head_only(table, head):
    """Object ids a read of the head state alone would report as edited.

    This is the read the tool replaces. It is kept to print how much of the
    owner's work it would have missed.
    """
    adds = set(o for o, s, _, _ in table["entries"] if s == head)
    return adds | set(o for o, _, at in table["deletes"] if at == head)


def resolve(export, version):
    """(version state, parent name, parent state, common ancestor)."""
    versions = export["versions"]
    if version not in versions:
        raise ValueError("version %s is not in versions.csv" % version)
    head, parent = versions[version]
    if parent not in versions:
        raise ValueError("the parent %r of %s is not in versions.csv"
                         % (parent, version))
    parent_head = versions[parent][0]
    return (head, parent, parent_head,
            common_ancestor(export["parents"], head, parent_head))


def ids(oids, sample):
    """A short list of object ids for a finding."""
    shown = ", ".join(str(o) for o in oids[:sample])
    return ", e.g. %s" % shown if shown else ""


def reload_finding(name, deleted, parent_deleted, arrived, keys,
                   deleted_keys, changed_keys, key_field, sample):
    """Findings when rows the owner edited come back under new object ids.

    With a key field the count is exact: an arriving row whose key belongs
    to a row the owner deleted, or to a row the owner changed (the version
    would then hold that key twice). A blank key is a hole in the data, not
    a key, and matches nothing. Without a key field the signal is the parent
    deleting the same rows the owner deleted while new rows arrive, which is
    what a reload under new object ids looks like and what an ordinary edit
    can too.
    """
    if key_field:
        out = []
        hits = sorted(o for o in arrived
                      if keys[o] and keys[o] in deleted_keys)
        if hits:
            out.append("%s: %d arriving row(s) carry the %s of a row the "
                       "owner deleted%s. The owner's deletes do not hold "
                       "them back" % (name, len(hits), key_field,
                                      ids(hits, sample)))
        dupes = sorted(o for o in arrived if o not in hits and keys[o]
                       and keys[o] in changed_keys)
        if dupes:
            out.append("%s: %d arriving row(s) carry the %s of a row the "
                       "owner changed%s. The version would hold each key "
                       "twice" % (name, len(dupes), key_field,
                                  ids(dupes, sample)))
        return out
    absorbed = sorted(set(deleted) & set(parent_deleted))
    if absorbed and arrived:
        return ["%s: the parent deleted %d of the rows the owner deleted%s, "
                "and %d new row(s) arrive. A reload under new object ids "
                "looks like this. Rerun with --key-field to count it exactly"
                % (name, len(absorbed), ids(absorbed, sample), len(arrived))]
    return []


def conflict_finding(name, changed, deleted, p_changed, p_deleted, sample):
    """A finding when both sides edited one row since the common ancestor.

    Update against update, update against delete and delete against update
    are the three conflict types Esri lists. A delete both sides made is not
    one of them. Under row-level conflict detection every hit is a conflict.
    """
    hits = sorted((set(changed) & (set(p_changed) | set(p_deleted)))
                  | (set(deleted) & set(p_changed)))
    if not hits:
        return None
    return ("%s: %d row(s) were edited by the owner and also edited or "
            "deleted in the parent since the ancestor%s. A row-level "
            "reconcile reports each as a conflict, and the side it favours "
            "overwrites the other" % (name, len(hits), ids(hits, sample)))


def client_finding(name, table, version_view, sample):
    """(line, finding) comparing a client's read with the derived view."""
    client = table["client"]
    if client is None:
        return "  client read           none in the export", None
    bad = sorted(o for o in set(client) | set(version_view)
                 if client.get(o) != (version_view[o][1]
                                      if o in version_view else None))
    line = ("  client read           %d rows, %d differ from the delta tables"
            % (len(client), len(bad)))
    if not bad:
        return line, None
    return line, ("%s: the client read differs from the delta tables on %d "
                  "row(s)%s. It is not a read of the version's current state"
                  % (name, len(bad), ids(bad, sample)))


def plan(export, version, oid_field, key_field, sample):
    """(lines, findings, snapshot) for a version before its reconcile."""
    parents = export["parents"]
    head, parent, parent_head, ancestor = resolve(export, version)
    v_lin = lineage(parents, head)
    p_lin = lineage(parents, parent_head)
    a_lin = lineage(parents, ancestor)
    own = set(v_lin) - set(a_lin)
    theirs = set(p_lin) - set(a_lin)
    lines = ["version %s at state %d, parent %s at state %d, common ancestor "
             "%d" % (version, head, parent, parent_head, ancestor)]
    findings = []
    snap = {"format": FORMAT, "version": version, "parent": parent,
            "version_state": head, "parent_state": parent_head,
            "ancestor": ancestor, "oid_field": oid_field,
            "key_field": key_field, "digests": export["digests"],
            "edited": bool(own), "tables": {}}
    for name in sorted(export["tables"]):
        table = export["tables"][name]
        a_view = view(name, table, a_lin)
        v_view = view(name, table, v_lin)
        p_view = view(name, table, p_lin)
        changed, deleted = edits(a_view, v_view, own)
        p_changed, p_deleted = edits(a_view, p_view, theirs)
        inserted = [o for o in changed if o not in a_view]
        p_inserted = [o for o in p_changed if o not in a_view]
        mine = set(changed) | set(deleted)
        lines.append("table %s" % name)
        lines.append("  version view          %d rows" % len(v_view))
        lines.append("  own edits             %d inserted, %d updated, %d "
                     "deleted" % (len(inserted), len(changed) - len(inserted),
                                  len(deleted)))
        lines.append("  head state alone      %d of those %d"
                     % (len(head_only(table, head) & mine), len(mine)))
        lines.append("  parent since ancestor %d inserted, %d updated, %d "
                     "deleted" % (len(p_inserted),
                                  len(p_changed) - len(p_inserted),
                                  len(p_deleted)))
        deleted_keys = sorted(set(a_view[o][2] for o in deleted
                                  if a_view[o][2])) if key_field else []
        changed_keys = sorted(set(v_view[o][2] for o in changed
                                  if v_view[o][2])) if key_field else []
        keys = dict((o, v[2]) for o, v in p_view.items())
        findings.extend(reload_finding(
            name, deleted, p_deleted, p_inserted, keys, set(deleted_keys),
            set(changed_keys), key_field, sample))
        found = conflict_finding(name, changed, deleted, p_changed,
                                 p_deleted, sample)
        if found:
            findings.append(found)
        line, found = client_finding(name, table, v_view, sample)
        lines.append(line)
        if found:
            findings.append(found)
        snap["tables"][name] = {
            "columns": table["columns"],
            "changed": dict((str(o), v_view[o][1]) for o in changed),
            "parent": dict((str(o), p_view[o][1] if o in p_view else None)
                           for o in changed),
            "deleted": deleted,
            "deleted_keys": deleted_keys,
            "changed_keys": changed_keys,
            "view": sorted(v_view),
        }
    return lines, findings, snap


def check_snapshot(snap):
    """Refuse anything that is not a snapshot this tool wrote."""
    if not isinstance(snap, dict) or snap.get("format") != FORMAT:
        raise ValueError("the --before file is not a reconverify snapshot")
    missing = [k for k in SNAP_KEYS if k not in snap]
    if missing:
        raise ValueError("the snapshot has no %s" % ", ".join(missing))
    if not isinstance(snap["tables"], dict) or not snap["tables"]:
        raise ValueError("the snapshot holds no table, so it verifies nothing")
    for name, was in sorted(snap["tables"].items()):
        gone = [k for k in SNAP_TABLE_KEYS
                if not isinstance(was, dict) or k not in was]
        if gone:
            raise ValueError("the snapshot of table %s has no %s"
                             % (name, ", ".join(gone)))


def verify(snap, export, version, sample):
    """(lines, findings) for a version after its reconcile, from a new read."""
    check_snapshot(snap)
    if snap["version"] != version:
        raise ValueError("the snapshot is of %s, not %s"
                         % (snap["version"], version))
    if export["digests"] == snap["digests"]:
        raise ValueError("every file in this export is byte for byte the "
                         "export the snapshot was taken from. That is the same "
                         "read, not a fresh one. Export again after the "
                         "reconcile")
    parents = export["parents"]
    head, parent, parent_head, ancestor = resolve(export, version)
    old = snap["ancestor"]
    a_lin = lineage(parents, ancestor)
    if old not in a_lin:
        raise ValueError("the old common ancestor %d is not in the lineage of "
                         "the new one %d, so the version was not reconciled "
                         "with %s" % (old, ancestor, parent))
    old_lin = lineage(parents, old)
    pulled = set(a_lin) - set(old_lin)
    v_lin = lineage(parents, head)
    p_lin = lineage(parents, parent_head)
    lines = ["version %s state %d -> %d, parent %s state %d -> %d, common "
             "ancestor %d -> %d" % (version, snap["version_state"], head,
                                    parent, snap["parent_state"],
                                    parent_head, old, ancestor)]
    findings = []
    # A reconcile leaves the parent's head in the version's own lineage. A
    # version that never moved is checked against the parent as it is now. A
    # version that moved is checked against the parent as the plan saw it.
    if head == snap["version_state"]:
        if parent_head not in v_lin:
            findings.append("%s: the parent is at state %d, which the version "
                            "does not hold, yet the version still points at "
                            "state %d. The reconcile did not run, or did not "
                            "finish" % (version, parent_head, head))
    elif snap["parent_state"] not in v_lin:
        findings.append("%s: the version moved to state %d but does not hold "
                        "state %d, the parent's state at the plan. The "
                        "reconcile did not run, or did not finish"
                        % (version, head, snap["parent_state"]))
    # A post makes the parent hold the version's own head state. Rows alone
    # cannot show it when the owner only deleted, so the state tree is read.
    posted_state = snap["edited"] and head in p_lin
    flagged = False
    key_field = snap["key_field"]
    for name in sorted(snap["tables"]):
        if name not in export["tables"]:
            raise ValueError("table %s is in the snapshot but not in the "
                             "export" % name)
        table = export["tables"][name]
        was = snap["tables"][name]
        if table["columns"] != was["columns"]:
            raise ValueError("the columns of %s changed since the snapshot"
                             % name)
        v_view = view(name, table, v_lin)
        p_view = view(name, table, p_lin)
        changed = dict((int(o), h) for o, h in was["changed"].items())
        parent_was = dict((int(o), h) for o, h in was["parent"].items())
        back = [o for o in was["deleted"] if o in v_view]
        lost = sorted(o for o, h in changed.items()
                      if o not in v_view or v_view[o][1] != h)
        posted = sorted(o for o, h in changed.items()
                        if o in p_view and p_view[o][1] == h
                        and parent_was[o] != h)
        before = set(was["view"])
        arrived = sorted(o for o in v_view if o not in before)
        _, p_deleted = edits(view(name, table, old_lin),
                             view(name, table, a_lin), pulled)
        lines.append("table %s" % name)
        lines.append("  version view          %d -> %d rows, %d arrived"
                     % (len(before), len(v_view), len(arrived)))
        lines.append("  own deletes kept      %d of %d"
                     % (len(was["deleted"]) - len(back), len(was["deleted"])))
        lines.append("  own rows identical    %d of %d"
                     % (len(changed) - len(lost), len(changed)))
        lines.append("  own rows in parent    %d" % len(posted))
        gone = [o for o in was["deleted"]
                if posted_state and o not in p_view]
        if back:
            findings.append("%s: %d row(s) the owner deleted are back%s"
                            % (name, len(back), ids(back, sample)))
        if lost:
            findings.append("%s: %d row(s) the owner changed are changed or "
                            "missing%s" % (name, len(lost), ids(lost, sample)))
        if posted:
            findings.append("%s: %d row(s) the owner changed are now in the "
                            "parent %s%s. Something posted"
                            % (name, len(posted), parent, ids(posted, sample)))
        elif gone:
            findings.append("%s: %d row(s) the owner deleted are gone from "
                            "the parent %s%s. Something posted"
                            % (name, len(gone), parent, ids(gone, sample)))
        flagged = flagged or bool(posted or gone)
        keys = dict((o, v[2]) for o, v in v_view.items())
        findings.extend(reload_finding(
            name, was["deleted"], p_deleted, arrived, keys,
            set(was["deleted_keys"]), set(was["changed_keys"]), key_field,
            sample))
        line, found = client_finding(name, table, v_view, sample)
        lines.append(line)
        if found:
            findings.append(found)
    for name in sorted(export["tables"]):
        if name not in snap["tables"]:
            lines.append("table %s is in the export but not in the snapshot, "
                         "so it was not checked" % name)
    if posted_state and not flagged:
        findings.append("%s: the parent %s now holds the version's own state "
                        "%d. Something posted" % (version, parent, head))
    return lines, findings


# ------------------------------------------------------------------- reading

def read_csv(path):
    """Rows and header of a CSV, as dicts."""
    # utf-8-sig: SQL Server Management Studio and Excel can both write a BOM,
    # and read as plain UTF-8 the first header cell would not match its name.
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            return list(reader), list(reader.fieldnames or [])
    except UnicodeDecodeError as exc:
        raise ValueError("%s is not UTF-8 text: %s"
                         % (os.path.basename(path), exc))


def need(header, names, path):
    missing = [n for n in names if n not in header]
    if missing:
        raise ValueError("%s has no column %s"
                         % (os.path.basename(path), ", ".join(missing)))


def to_int(value, what):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("%s is not a whole number: %r" % (what, value))


def file_digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def load_table(folder, name, oid_field, key_field):
    path = os.path.join(folder, name + ".base.csv")
    rows, header = read_csv(path)
    need(header, [oid_field] + ([key_field] if key_field else []), path)
    columns = sorted(c for c in header if c != oid_field)

    def entry(row, state, where):
        return (to_int(row[oid_field], "%s %s" % (where, oid_field)), state,
                row_hash(row, columns),
                (row[key_field] or "").strip() if key_field else None)

    entries = [entry(r, ROOT_STATE, path) for r in rows]
    path = os.path.join(folder, name + ".adds.csv")
    rows, header = read_csv(path)
    need(header, [oid_field, STATE_FIELD] + columns, path)
    entries += [entry(r, to_int(r[STATE_FIELD], "%s %s" % (path, STATE_FIELD)),
                      path) for r in rows]
    path = os.path.join(folder, name + ".deletes.csv")
    rows, header = read_csv(path)
    need(header, [ROW_ID_FIELD, STATE_FIELD, DELETED_AT_FIELD], path)
    deletes = set((to_int(r[ROW_ID_FIELD], path), to_int(r[STATE_FIELD], path),
                   to_int(r[DELETED_AT_FIELD], path)) for r in rows)
    client = None
    path = os.path.join(folder, name + ".client.csv")
    if os.path.exists(path):
        rows, header = read_csv(path)
        need(header, [oid_field] + columns, path)
        client = dict((to_int(r[oid_field], path), row_hash(r, columns))
                      for r in rows)
    return {"columns": columns, "entries": entries, "deletes": deletes,
            "client": client}


def load_export(folder, oid_field, key_field):
    """Everything the core reads, from one export directory."""
    names = sorted(os.listdir(folder))
    digests = dict((n, file_digest(os.path.join(folder, n)))
                   for n in names if n.endswith(".csv"))
    path = os.path.join(folder, "states.csv")
    rows, header = read_csv(path)
    need(header, ["state_id", "parent_state_id"], path)
    parents = dict((to_int(r["state_id"], path),
                    to_int(r["parent_state_id"], path)) for r in rows)
    if len(parents) != len(rows):
        raise ValueError("states.csv lists a state more than once")
    path = os.path.join(folder, "versions.csv")
    rows, header = read_csv(path)
    need(header, ["name", "state_id", "parent_name"], path)
    versions = dict((r["name"].strip(), (to_int(r["state_id"], path),
                                         (r["parent_name"] or "").strip()))
                    for r in rows)
    if len(versions) != len(rows):
        raise ValueError("versions.csv lists a version name more than once. "
                         "Names are matched as written, so give each its "
                         "OWNER.NAME")
    tables = dict((n[:-len(".base.csv")],
                   load_table(folder, n[:-len(".base.csv")], oid_field,
                              key_field))
                  for n in names if n.endswith(".base.csv"))
    if not tables:
        raise ValueError("there is no <table>.base.csv in %s" % folder)
    return {"parents": parents, "versions": versions, "tables": tables,
            "digests": digests}


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the core and the CLI, on synthetic exports only."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, text=""):
        try:
            fn()
        except ValueError as exc:
            check(text in str(exc), label if text in str(exc) else
                  "%s (wrong message %r)" % (label, str(exc)))
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("reconverify self-test: synthetic exports, no database, no network")
    print("-" * 68)

    tmp = tempfile.mkdtemp(prefix="reconverify_")

    def put(folder, name, text):
        with open(os.path.join(folder, name), "w", newline="",
                  encoding="utf-8") as handle:
            handle.write(text)

    # One synthetic table of ten road segments. A row is (oid, key, name,
    # surface); an adds row puts its state second.
    base = [(i, "R-%02d" % i, "Road %d" % i, "DIRT") for i in range(1, 11)]

    def export(label, states, versions, adds, deletes, client=None,
               rows=None):
        folder = os.path.join(tmp, label)
        os.makedirs(folder)
        put(folder, "states.csv", "state_id,parent_state_id\n" + "".join(
            "%d,%d\n" % s for s in states))
        put(folder, "versions.csv", "name,state_id,parent_name\n" + "".join(
            "%s,%d,%s\n" % v for v in versions))
        put(folder, "ROADS.base.csv", "OBJECTID,ROAD_KEY,NAME,SURFACE\n" +
            "".join("%d,%s,%s,%s\n" % r for r in (rows or base)))
        put(folder, "ROADS.adds.csv",
            "OBJECTID,SDE_STATE_ID,ROAD_KEY,NAME,SURFACE\n" + "".join(
                "%d,%d,%s,%s,%s\n" % a for a in adds))
        put(folder, "ROADS.deletes.csv",
            "SDE_DELETES_ROW_ID,SDE_STATE_ID,DELETED_AT\n" + "".join(
                "%d,%d,%d\n" % d for d in deletes))
        if client is not None:
            put(folder, "ROADS.client.csv", "OBJECTID,ROAD_KEY,NAME,SURFACE\n"
                + "".join("%d,%s,%s,%s\n" % r for r in client))
        return folder

    # The state tree. State 1 is a parent edit both sides share, so it is the
    # common ancestor. The sandbox edited over three states, 10, 11 and 12,
    # and the parent moved on to state 2 after the sandbox was created.
    tree = [(0, 0), (1, 0), (2, 1), (10, 1), (11, 10), (12, 11)]
    versions = [("SDE.DEFAULT", 0, ""), ("OWNER.EDITS", 2, "SDE.DEFAULT"),
                ("OWNER.SANDBOX", 12, "OWNER.EDITS")]
    shared_adds = [(1, 1, "R-01", "Road 1 renamed", "DIRT")]
    shared_dels = [(1, 0, 1)]
    own_adds = [(4, 11, "R-04", "Road 4", "PAVED"),
                (101, 12, "R-101", "Road 101", "PAVED")]
    own_dels = [(2, 0, 10), (3, 0, 10), (4, 0, 11)]
    reloaded = [o for o in range(2, 11) if o != 4]
    # The parent reloads its copy: every old row but 4 deleted, every one
    # back under a new object id 2xx with the same key.
    reload_adds = [(198 + o if o < 4 else 197 + o, 2, "R-%02d" % o,
                    "Road %d" % o, "DIRT") for o in reloaded]
    reload_dels = [(o, 0, 2) for o in reloaded]
    # The quiet alternative: the parent only resurfaced road 6.
    quiet_adds = [(6, 2, "R-06", "Road 6", "CHIPSEAL")]
    quiet_dels = [(6, 0, 2)]
    before_view = [(1, "R-01", "Road 1 renamed", "DIRT"),
                   (4, "R-04", "Road 4", "PAVED")] + \
        [r for r in base if r[0] >= 5] + [(101, "R-101", "Road 101", "PAVED")]

    reload_dir = export("reload_before", tree, versions,
                        shared_adds + own_adds + reload_adds,
                        shared_dels + own_dels + reload_dels, before_view)
    quiet_dir = export("quiet_before", tree, versions,
                       shared_adds + own_adds + quiet_adds,
                       shared_dels + own_dels + quiet_dels, before_view)

    # ---- the state tree
    parents = dict(tree)
    check(lineage(parents, 12) == [12, 11, 10, 1, 0],
          "a lineage runs from the head back to state 0, nearest first")
    check(lineage(parents, 0) == [0], "state 0 is its own whole lineage")
    check(common_ancestor(parents, 12, 2) == 1,
          "the common ancestor of the sandbox and its parent is state 1")
    check(common_ancestor(parents, 2, 2) == 2,
          "a state is its own common ancestor")
    chain = dict((i, i - 1) for i in range(1, 30001))
    started = time.time()
    deep = lineage(chain, 30000)
    check(len(deep) == 30001 and time.time() - started < 3,
          "a 30000-state chain reads in well under 3 seconds, not the 9.5 "
          "a list membership test took  <-- pinned defect")
    raises(lambda: lineage({5: 6, 6: 5}, 5),
           "a state tree that loops is refused, not walked forever")
    raises(lambda: lineage({5: 7}, 5),
           "a parent state missing from states.csv is refused")

    # ---- the views and the head-state-only defect
    ex = load_export(reload_dir, DEFAULT_OID_FIELD, None)
    roads = ex["tables"]["ROADS"]
    v12 = view("ROADS", roads, lineage(parents, 12))
    check(sorted(v12) == [1, 4] + list(range(5, 11)) + [101],
          "the sandbox sees 9 rows: its deletes of 2 and 3 hold")
    check(v12[4][0] == 11 and v12[1][0] == 1,
          "an update is read from the state that wrote it")
    a_view = view("ROADS", roads, lineage(parents, 1))
    own = set(lineage(parents, 12)) - set(lineage(parents, 1))
    changed, deleted = edits(a_view, v12, own)
    check(changed == [4, 101] and deleted == [2, 3],
          "the own edits are read across states 10, 11 and 12")
    check(head_only(roads, 12) == set([101]),
          "a read of the head state alone sees 1 of the 4 own edits  "
          "<-- pinned defect")
    twice = {"entries": [(7, 0, "a", None), (7, 3, "b", None)],
             "deletes": set()}
    raises(lambda: view("T", twice, [3, 0]),
           "an object id live twice in one lineage is refused")

    # ---- plan: what a reconcile would do
    lines, findings, snap = plan(ex, "OWNER.SANDBOX", "OBJECTID", None, 10)
    text = "\n".join(lines)
    check("common ancestor 1" in text, "the plan names the common ancestor")
    check("own edits             1 inserted, 1 updated, 2 deleted" in text,
          "and counts the own edits by kind")
    check("head state alone      1 of those 4" in text,
          "and prints how much a head-state read would miss")
    check("parent since ancestor 8 inserted, 0 updated, 8 deleted" in text,
          "and counts what the parent did since the ancestor")
    check(len(findings) == 1 and "parent deleted 2 of the rows the owner "
          "deleted, e.g. 2, 3, and 8 new row(s) arrive" in findings[0],
          "a parent reload over the owner's deletes is forecast  "
          "<-- pinned defect")
    check("client read           9 rows, 0 differ" in text,
          "a client read that matches the delta tables is not a finding")
    check(snap["tables"]["ROADS"]["deleted"] == [2, 3]
          and sorted(snap["tables"]["ROADS"]["changed"]) == ["101", "4"],
          "the snapshot records the own deletes and changes")
    check(snap["tables"]["ROADS"]["parent"]["101"] is None,
          "and what the parent held for each changed row")

    ex_key = load_export(reload_dir, DEFAULT_OID_FIELD, "ROAD_KEY")
    lines, findings, snap_key = plan(ex_key, "OWNER.SANDBOX", "OBJECTID",
                                     "ROAD_KEY", 10)
    check(len(findings) == 1 and "2 arriving row(s) carry the ROAD_KEY of "
          "a row the owner deleted, e.g. 200, 201" in findings[0],
          "with a key field the reload is counted exactly: 2 rows come back")
    check(snap_key["tables"]["ROADS"]["deleted_keys"] == ["R-02", "R-03"],
          "and the snapshot keeps the keys the owner deleted")

    ex_quiet = load_export(quiet_dir, DEFAULT_OID_FIELD, None)
    lines, findings, snap_quiet = plan(ex_quiet, "OWNER.SANDBOX", "OBJECTID",
                                       None, 10)
    check(findings == [], "a parent that only resurfaced a road is no "
          "finding")
    ex_qkey = load_export(quiet_dir, DEFAULT_OID_FIELD, "ROAD_KEY")
    check(plan(ex_qkey, "OWNER.SANDBOX", "OBJECTID", "ROAD_KEY", 10)[1] == [],
          "nor with a key field")
    findings = plan(ex, "OWNER.EDITS", "OBJECTID", None, 10)[1]
    check(len(findings) == 1 and "client read differs" in findings[0],
          "the sandbox's client read, offered as the parent's, is caught")
    raises(lambda: plan(ex, "OWNER.NOBODY", "OBJECTID", None, 10),
           "a version not in versions.csv is refused")
    raises(lambda: plan(ex, "SDE.DEFAULT", "OBJECTID", None, 10),
           "DEFAULT has no parent to reconcile with, and is refused")

    # ---- after the reconcile. The sandbox now points at state 20, a child
    # of the parent's state 2, which holds the own edits again.
    after_tree = tree + [(20, 2)]
    after_versions = [("SDE.DEFAULT", 0, ""), ("OWNER.EDITS", 2,
                                               "SDE.DEFAULT"),
                      ("OWNER.SANDBOX", 20, "OWNER.EDITS")]
    redo_adds = [(4, 20, "R-04", "Road 4", "PAVED"),
                 (101, 20, "R-101", "Road 101", "PAVED")]
    redo_dels = [(4, 0, 20)]
    # Where the parent did not delete roads 2 and 3 itself, the reconcile
    # writes the owner's deletes again at the new state.
    redo_quiet = redo_dels + [(2, 0, 20), (3, 0, 20)]

    bad_dir = export("reload_after", after_tree, after_versions,
                     shared_adds + own_adds + reload_adds + redo_adds,
                     shared_dels + own_dels + reload_dels + redo_dels,
                     before_view)
    ex_bad = load_export(bad_dir, DEFAULT_OID_FIELD, None)
    lines, findings = verify(snap, ex_bad, "OWNER.SANDBOX", 3)
    text = "\n".join(lines)
    check("state 12 -> 20" in text and "common ancestor 1 -> 2" in text,
          "verify reads the new state and the new ancestor")
    check("version view          9 -> 11 rows, 8 arrived" in text,
          "and sees the version grow from 9 to 11 rows")
    check("own deletes kept      2 of 2" in text
          and "own rows identical    2 of 2" in text,
          "the owner's own edits all survived")
    check(any("and 8 new row(s) arrive" in f for f in findings),
          "yet the reload is a finding, because the deletes no longer hold  "
          "<-- pinned defect")
    check(any("client read differs from the delta tables on 14 row(s), "
              "e.g. 5, 6, 7" in f for f in findings),
          "a client read still showing the old rows is caught  "
          "<-- pinned defect")
    check(len(findings) == 2, "and those are the only two findings")
    lines, findings = verify(snap_key, load_export(
        bad_dir, DEFAULT_OID_FIELD, "ROAD_KEY"), "OWNER.SANDBOX", 10)
    check(any("2 arriving row(s) carry the ROAD_KEY" in f for f in findings),
          "with the key the snapshot carries, the 2 returning rows are named")

    good_view = [r if r[0] != 6 else (6, "R-06", "Road 6", "CHIPSEAL")
                 for r in before_view]
    good_dir = export("quiet_after", after_tree, after_versions,
                      shared_adds + own_adds + quiet_adds + redo_adds,
                      shared_dels + own_dels + quiet_dels + redo_quiet,
                      good_view)
    ex_good = load_export(good_dir, DEFAULT_OID_FIELD, None)
    lines, findings = verify(snap_quiet, ex_good, "OWNER.SANDBOX", 10)
    check(findings == [], "a clean reconcile verifies with no finding")
    check("version view          9 -> 9 rows, 0 arrived" in "\n".join(lines),
          "and its row count holds")

    # FAVOR_TARGET_VERSION on a conflict: road 4 lost its surface and the
    # deleted road 2 came back.
    lossy_dir = export("lossy_after", after_tree, after_versions,
                       shared_adds + own_adds + quiet_adds +
                       [(101, 20, "R-101", "Road 101", "PAVED")],
                       shared_dels + own_dels + quiet_dels + [(3, 0, 20)])
    lines, findings = verify(snap_quiet, load_export(
        lossy_dir, DEFAULT_OID_FIELD, None), "OWNER.SANDBOX", 10)
    check(any("1 row(s) the owner deleted are back, e.g. 2" in f
              for f in findings), "a deleted row that came back is a finding")
    check(any("1 row(s) the owner changed are changed or missing, e.g. 4" in f
              for f in findings), "an own update that was undone is a finding")
    check("client read           none in the export" in "\n".join(lines),
          "an export without a client read says so")

    # Something posted: the parent now points at the sandbox's state.
    posted_versions = [("SDE.DEFAULT", 0, ""), ("OWNER.EDITS", 20,
                                                "SDE.DEFAULT"),
                       ("OWNER.SANDBOX", 20, "OWNER.EDITS")]
    posted_dir = export("posted_after", after_tree, posted_versions,
                        shared_adds + own_adds + quiet_adds + redo_adds,
                        shared_dels + own_dels + quiet_dels + redo_quiet)
    lines, findings = verify(snap_quiet, load_export(
        posted_dir, DEFAULT_OID_FIELD, None), "OWNER.SANDBOX", 10)
    check(len(findings) == 1 and "2 row(s) the owner changed are now in the "
          "parent OWNER.EDITS, e.g. 4, 101. Something posted" in findings[0],
          "a reconcile that posted is a finding  <-- pinned defect")

    # Nothing ran: a new export, but the version never moved.
    unmoved_dir = export("unmoved_after", tree, versions,
                         shared_adds + own_adds + quiet_adds,
                         shared_dels + own_dels + quiet_dels, good_view)
    lines, findings = verify(snap_quiet, load_export(
        unmoved_dir, DEFAULT_OID_FIELD, None), "OWNER.SANDBOX", 10)
    check(any("still points at state 12" in f for f in findings),
          "a version that never moved, with a parent to pull, is a finding")
    idle_dir = export("idle_after", tree, [
        ("SDE.DEFAULT", 0, ""), ("OWNER.EDITS", 1, "SDE.DEFAULT"),
        ("OWNER.SANDBOX", 12, "OWNER.EDITS")],
        shared_adds + own_adds + quiet_adds,
        shared_dels + own_dels + quiet_dels, before_view)
    in_sync = dict(snap_quiet, parent_state=1)
    check(verify(in_sync, load_export(idle_dir, DEFAULT_OID_FIELD, None),
                 "OWNER.SANDBOX", 10)[1] == [],
          "but not when the parent is still at the common ancestor: nothing "
          "to pull in")
    early = dict(snap_quiet, parent_state=1)
    check(any("still points at state 12" in f for f in verify(
        early, load_export(unmoved_dir, DEFAULT_OID_FIELD, None),
        "OWNER.SANDBOX", 10)[1]),
          "a parent that moved after the plan, with the version unmoved, is "
          "a finding  <-- pinned defect")

    # ---- first review. A version that moved without a reconcile, a post
    # of deletes only, a post no row shows, blank keys, an update reloaded
    # under a new object id, and conflicts the plan can see.
    more_tree = tree + [(13, 12), (20, 2)]

    def reconciled(label, adds, dels, parent_at=2, rows=None, client=None):
        """The sandbox at state 20, a child of state 2, after a reconcile."""
        return export(label, more_tree, [
            ("SDE.DEFAULT", 0, ""), ("OWNER.EDITS", parent_at, "SDE.DEFAULT"),
            ("OWNER.SANDBOX", 20, "OWNER.EDITS")], adds, dels, client, rows)

    def plan_of(folder, key=None):
        return plan(load_export(folder, DEFAULT_OID_FIELD, key),
                    "OWNER.SANDBOX", DEFAULT_OID_FIELD, key, 10)

    def verify_of(snapshot, folder, key=None):
        return verify(snapshot, load_export(folder, DEFAULT_OID_FIELD, key),
                      "OWNER.SANDBOX", 10)

    edit_dir = export("edit_after", more_tree, [
        ("SDE.DEFAULT", 0, ""), ("OWNER.EDITS", 2, "SDE.DEFAULT"),
        ("OWNER.SANDBOX", 13, "OWNER.EDITS")],
        shared_adds + own_adds + quiet_adds + [(7, 13, "R-07", "Road 7",
                                                "PAVED")],
        shared_dels + own_dels + quiet_dels + [(7, 0, 13)], good_view)
    check(any("does not hold state 2" in f
              for f in verify_of(snap_quiet, edit_dir)[1]),
          "a version that moved on one more edit, without the parent pulled "
          "in, is a finding  <-- pinned defect")

    del_dels = [(2, 0, 10), (3, 0, 12)]
    del_snap = plan_of(export("del_before", tree, versions, shared_adds,
                              shared_dels + del_dels))[2]
    redo_del = [(2, 0, 20), (3, 0, 20)]
    lines, findings = verify_of(del_snap, reconciled(
        "del_posted", shared_adds, shared_dels + del_dels + redo_del, 20))
    check(len(findings) == 1 and "2 row(s) the owner deleted are gone from "
          "the parent OWNER.EDITS, e.g. 2, 3. Something posted" in findings[0],
          "a post of delete-only edits is a finding  <-- pinned defect")
    lines, findings = verify_of(del_snap, reconciled(
        "del_good", shared_adds, shared_dels + del_dels + redo_del))
    check(findings == [], "and the same deletes, reconciled but not posted, "
          "are clean")
    empty_snap = plan_of(export("empty_before", tree, versions, shared_adds,
                                shared_dels))[2]
    lines, findings = verify_of(empty_snap, reconciled(
        "empty_posted", shared_adds, shared_dels, 20))
    check(len(findings) == 1 and "now holds the version's own state 20. "
          "Something posted" in findings[0],
          "a post that no exported row shows is read from the state tree  "
          "<-- pinned defect")
    check(verify_of(empty_snap, reconciled(
        "empty_good", shared_adds, shared_dels))[1] == [],
          "and an edit-free version that was not posted is clean")
    check(verify_of(dict(empty_snap, edited=False), reconciled(
        "empty_noedit", shared_adds, shared_dels, 20))[1] == [],
          "a version that never had an edit cannot have been posted")

    blank_rows = [(2, " ", "Road 2", "DIRT") if r[0] == 2 else r for r in base]
    lines, findings, blank_snap = plan_of(export(
        "blank_before", tree, versions,
        shared_adds + own_adds + [(300, 2, " ", "Road 300", "DIRT")],
        shared_dels + own_dels, rows=blank_rows), "ROAD_KEY")
    check(findings == [] and blank_snap["tables"]["ROADS"]["deleted_keys"]
          == ["R-03"],
          "a blank key is no key: a new road with a blank key does not "
          "match a deleted road with a blank key  <-- pinned defect")
    check(reload_finding("T", [], [], [5], {5: ""}, set([""]), set(), "K",
                         10) == [],
          "and a blank key in a hand-edited snapshot matches nothing")

    upd_adds = [(4, 11, "R-04", "Road 4", "PAVED")]
    upd_dels = [(4, 0, 11)]
    twin = [(200, 2, "R-04", "Road 4", "DIRT")]
    lines, findings, upd_snap = plan_of(export(
        "upd_before", tree, versions, upd_adds + twin, upd_dels), "ROAD_KEY")
    check(len(findings) == 1 and "1 arriving row(s) carry the ROAD_KEY of a "
          "row the owner changed, e.g. 200" in findings[0],
          "a parent row that repeats the key of an updated row is a finding  "
          "<-- pinned defect")
    lines, findings = verify_of(upd_snap, reconciled(
        "upd_after", upd_adds + twin + [(4, 20, "R-04", "Road 4", "PAVED")],
        upd_dels + [(4, 0, 20)]), "ROAD_KEY")
    check(len(findings) == 1 and "1 arriving row(s) carry the ROAD_KEY of a "
          "row the owner changed, e.g. 200. The version would hold each key "
          "twice" in findings[0],
          "and the version that holds the key twice fails verify  "
          "<-- pinned defect")

    lines, findings, conflict_snap = plan_of(export(
        "conflict_before", tree, versions, upd_adds, upd_dels + [(4, 0, 2)]))
    check(len(findings) == 1 and "1 row(s) were edited by the owner and also "
          "edited or deleted in the parent since the ancestor, e.g. 4"
          in findings[0],
          "an owner update of a row the parent deleted is forecast at the "
          "plan  <-- pinned defect")
    findings = plan_of(export(
        "conflict2_before", tree, versions,
        [(5, 2, "R-05", "Road 5", "CHIPSEAL")],
        [(5, 0, 2), (5, 0, 10)]))[1]
    check(len(findings) == 1 and "e.g. 5" in findings[0],
          "so is an owner delete of a row the parent updated")
    check(conflict_finding("T", [], [5], [], [5], 10) is None,
          "a delete both sides made is not a conflict")
    lines, findings, same_snap = plan_of(export(
        "same_before", tree, versions,
        upd_adds + [(4, 2, "R-04", "Road 4", "PAVED")],
        upd_dels + [(4, 0, 2)]))
    table = same_snap["tables"]["ROADS"]
    check(table["parent"]["4"] == table["changed"]["4"],
          "the snapshot keeps the value the parent held for a changed row")
    lines, findings = verify_of(same_snap, reconciled(
        "same_after", upd_adds + [(4, 2, "R-04", "Road 4", "PAVED"),
                                  (4, 20, "R-04", "Road 4", "PAVED")],
        upd_dels + [(4, 0, 2), (4, 2, 20)]))
    check(findings == [], "a parent that made the same edit as the owner is "
          "not a post  <-- pinned defect")

    lines, findings, mix_snap = plan_of(export(
        "mix_before", tree, versions,
        upd_adds + [(101, 12, "R-101", "Road 101", "PAVED"),
                    (102, 12, "R-102", "Road 102", "PAVED")], upd_dels))
    check("own edits             2 inserted, 1 updated, 0 deleted"
          in "\n".join(lines),
          "two inserts and one update are counted apart  <-- pinned defect")
    raises(lambda: verify(snap_quiet, ex_good, "OWNER.EDITS", 10),
           "a snapshot of another version is refused  <-- pinned defect",
           "snapshot is of OWNER.SANDBOX, not OWNER.EDITS")
    for key in SNAP_KEYS:
        raises(lambda: check_snapshot(dict(
            (k, v) for k, v in snap_quiet.items() if k != key)),
            "a snapshot without %s is refused, not a KeyError  "
            "<-- pinned defect" % key, "has no ")
    raises(lambda: check_snapshot(dict(snap_quiet, tables={})),
           "a snapshot with no table verifies nothing and is refused",
           "holds no table")
    raises(lambda: check_snapshot(dict(snap_quiet, tables={"ROADS": 5})),
           "a snapshot table that is not an object is refused",
           "table ROADS has no columns")
    raises(lambda: check_snapshot(dict(snap_quiet, tables={"ROADS": {}})),
           "a snapshot table without its keys is refused", "changed_keys")
    side = os.path.join(tmp, "side_after")
    shutil.copytree(good_dir, side)
    put(side, "SIGNS.base.csv", "OBJECTID,NAME\n")
    put(side, "SIGNS.adds.csv", "OBJECTID,SDE_STATE_ID,NAME\n")
    put(side, "SIGNS.deletes.csv",
        "SDE_DELETES_ROW_ID,SDE_STATE_ID,DELETED_AT\n")
    lines, findings = verify_of(snap_quiet, side)
    check(findings == [] and "table SIGNS is in the export but not in the "
          "snapshot, so it was not checked" in lines,
          "a table the snapshot never saw is named, not skipped in silence  "
          "<-- pinned defect")

    raises(lambda: verify(snap_quiet, ex_quiet, "OWNER.SANDBOX", 10),
           "verifying against the snapshot's own export is refused  "
           "<-- pinned defect")
    raises(lambda: verify({"format": "x"}, ex_good, "OWNER.SANDBOX", 10),
           "a JSON file without the snapshot format is refused")
    raises(lambda: check_snapshot([1, 2]),
           "a JSON list is not a snapshot either")
    raises(lambda: verify(dict(snap_quiet, ancestor=12), ex_good,
                          "OWNER.SANDBOX", 10),
           "an old ancestor outside the new lineage is refused")
    gone = dict(snap_quiet, tables=dict(
        snap_quiet["tables"], SIGNS=snap_quiet["tables"]["ROADS"]))
    raises(lambda: verify(gone, ex_good, "OWNER.SANDBOX", 10),
           "a snapshot table missing from the export is refused",
           "table SIGNS is in the snapshot")
    widened = dict(snap_quiet, tables={"ROADS": dict(
        snap_quiet["tables"]["ROADS"], columns=["NAME"])})
    raises(lambda: verify(widened, ex_good, "OWNER.SANDBOX", 10),
           "a table whose columns changed is refused")

    # ---- the reader
    def bad_export(label, **files):
        folder = os.path.join(tmp, label)
        shutil.copytree(quiet_dir, folder)
        for name, text in files.items():
            put(folder, name.replace("_", "."), text)
        return folder

    raises(lambda: load_export(bad_export(
        "no_parent_col", states_csv="state_id\n1\n"), "OBJECTID", None),
        "a states.csv without parent_state_id is refused")
    raises(lambda: load_export(bad_export(
        "word_state", states_csv="state_id,parent_state_id\nx,0\n"),
        "OBJECTID", None), "a state id that is not a number is refused")
    raises(lambda: load_export(bad_export(
        "blank_state", states_csv="state_id,parent_state_id\n1\n"),
        "OBJECTID", None), "a short states.csv row is refused")
    raises(lambda: load_export(quiet_dir, "FID", None),
           "an object id column not in the base table is refused")
    raises(lambda: load_export(quiet_dir, "OBJECTID", "ASSET"),
           "a key field not in the base table is refused")
    narrow = bad_export("narrow_client",
                        ROADS_client_csv="OBJECTID,NAME\n1,Road 1\n")
    raises(lambda: load_export(narrow, "OBJECTID", None),
           "a client read missing a column is refused")
    empty = os.path.join(tmp, "empty")
    os.makedirs(empty)
    put(empty, "states.csv", "state_id,parent_state_id\n0,0\n")
    put(empty, "versions.csv", "name,state_id,parent_name\n")
    raises(lambda: load_export(empty, "OBJECTID", None),
           "an export with no table is refused")
    raises(lambda: load_export(bad_export(
        "dupe_version", versions_csv="name,state_id,parent_name\n"
        "OWNER.A,1,SDE.DEFAULT\nOWNER.A,2,SDE.DEFAULT\n"), "OBJECTID", None),
        "a version name listed twice is refused, not collapsed  "
        "<-- pinned defect", "more than once")
    raises(lambda: load_export(bad_export(
        "dupe_state", states_csv="state_id,parent_state_id\n1,0\n1,0\n"),
        "OBJECTID", None), "a state listed twice is refused, not collapsed  "
        "<-- pinned defect", "more than once")
    wide = bad_export("wide_text")
    with open(os.path.join(wide, "states.csv"), "wb") as handle:
        handle.write(b"state_id,parent_state_id\n1,0\n# caf\xe9\n")
    raises(lambda: load_export(wide, "OBJECTID", None),
           "a file that is not UTF-8 is refused and named  "
           "<-- pinned defect", "states.csv is not UTF-8")
    bom = bad_export("bom", states_csv="\ufeffstate_id,parent_state_id\n" +
                     "".join("%d,%d\n" % s for s in tree))
    check(load_export(bom, "OBJECTID", None)["parents"] == parents,
          "a states.csv with a byte order mark reads the same")
    check(row_hash({"A": "1", "B": ""}, ["A", "B"])
          == row_hash({"A": "1"}, ["A", "B"]),
          "an empty cell and a missing cell hash the same")

    # ---- the command line
    def run_cli(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    sv = ["--edit-version", "OWNER.SANDBOX"]
    code, out, err = run_cli([reload_dir] + sv)
    check(code == 1 and "VERDICT: 1 finding(s)" in out,
          "a planned reload exits 1")
    code, out, err = run_cli([quiet_dir] + sv)
    check(code == 0 and "VERDICT: nothing found" in out,
          "a quiet plan exits 0")
    snap_path = os.path.join(tmp, "snap.json")
    code, out, err = run_cli([quiet_dir] + sv + ["--snapshot", snap_path])
    check(code == 0 and "Check only. %s was not written" % snap_path in out
          and not os.path.exists(snap_path),
          "--snapshot without --apply writes nothing  <-- pinned defect")
    code, out, err = run_cli([quiet_dir] + sv + ["--snapshot", snap_path,
                                                  "--apply"])
    check(code == 0 and os.path.exists(snap_path),
          "--snapshot with --apply writes the snapshot")
    code, out, err = run_cli([quiet_dir] + sv + ["--snapshot", snap_path,
                                                  "--apply"])
    check(code == 2 and "could not write" in err,
          "an existing snapshot is never overwritten, and exits 2  "
          "<-- pinned defect")
    code, out, err = run_cli([good_dir] + sv + ["--before", snap_path])
    check(code == 0 and "VERDICT: verified from a fresh read" in out,
          "the written snapshot verifies a clean reconcile, exit 0")
    code, out, err = run_cli([lossy_dir] + sv + ["--before", snap_path])
    check(code == 1 and "FINDING" in out and "not verified" in out,
          "and fails an undone one, exit 1")
    code, out, err = run_cli([quiet_dir] + sv + ["--before", snap_path])
    check(code == 3 and "refused: every file in this export" in err,
          "the snapshot's own export as the after read exits 3")
    key_path = os.path.join(tmp, "snap_key.json")
    run_cli([reload_dir] + sv + ["--key-field", "ROAD_KEY", "--snapshot",
                                 key_path, "--apply"])
    code, out, err = run_cli([bad_dir] + sv + ["--before", key_path])
    check(code == 1 and "carry the ROAD_KEY" in out,
          "verify reads the key field from the snapshot")
    put(tmp, "junk.json", "{not json")
    code, out, err = run_cli([good_dir] + sv + ["--before", os.path.join(
        tmp, "junk.json")])
    check(code == 3, "a --before file that is not JSON exits 3")
    put(tmp, "thin.json", json.dumps({"format": FORMAT, "version": "X"}))
    code, out, err = run_cli([good_dir] + sv + ["--before", os.path.join(
        tmp, "thin.json")])
    check(code == 3 and "the snapshot has no " in err,
          "a snapshot that lost keys exits 3, not the 1 of a finding  "
          "<-- pinned defect")
    code, out, err = run_cli([good_dir] + sv + ["--before", os.path.join(
        tmp, "nowhere.json")])
    check(code == 2, "a --before file that does not exist exits 2")
    code, out, err = run_cli([os.path.join(tmp, "nowhere")] + sv)
    check(code == 2, "an export directory that does not exist exits 2")
    code, out, err = run_cli([quiet_dir] + sv + ["--snapshot", os.path.join(
        tmp, "no", "dir", "s.json"), "--apply"])
    check(code == 2 and "could not write" in err,
          "a snapshot path that cannot be written exits 2")
    code, out, err = run_cli([quiet_dir] + sv + ["--sample", "0"])
    check(code == 0, "--sample 0 is accepted")

    for argv, label in (
            ([quiet_dir], "an export without --edit-version exits 64"),
            (sv, "--edit-version without an export exits 64"),
            ([quiet_dir] + sv + ["--sample", "-1"],
             "a negative --sample exits 64"),
            ([quiet_dir] + sv + ["--apply"],
             "--apply without --snapshot exits 64"),
            ([good_dir] + sv + ["--before", snap_path, "--key-field", "K"],
             "--before with --key-field exits 64"),
            ([good_dir] + sv + ["--before", snap_path, "--snapshot", "x"],
             "--before with --snapshot exits 64"),
            ([quiet_dir] + sv + ["--ap"],
             "--ap through main exits 64, not 2  <-- pinned defect")):
        check(run_cli(argv)[0] == 64, label)
    for flag in ("--ap", "--snap", "--bef"):
        err = io.StringIO()
        refused = False
        with contextlib.redirect_stderr(err):
            try:
                _parse([quiet_dir, flag])
            except SystemExit:
                refused = True
        check(refused and "unrecognized arguments" in err.getvalue(),
              "a unique prefix %s is refused, not expanded  <-- pinned defect"
              % flag)
    check(_parse([quiet_dir, "--oid-field", "FID"]).oid_field == "FID",
          "--oid-field is read")
    code, out, err = run_cli([quiet_dir] + sv + ["--oid-field", "FID"])
    check(code == 3, "and a wrong one refuses the run with exit 3")
    code, out, err = run_cli(["--help"])
    check(code == 0 and "--edit-version" in out, "--help exits 0")
    argv_before = sys.argv
    try:
        sys.argv = ["reconverify.py", quiet_dir] + sv
        check(run_cli(None)[0] == 0,
              "main with no argv reads the arguments after the program name")
    finally:
        sys.argv = argv_before

    # ---- the harness itself
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
        raises(lambda: lineage({5: 7}, 5), "probe: the wrong message must "
               "fail", "no such text")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 4,
          "check() and raises() really do record a failure  <-- pinned defect")

    def summary(passed_count, failures):
        print("-" * 68)
        total = passed_count + len(failures)
        if failures:
            print("%d assertions, %d failed" % (total, len(failures)))
            for f in failures:
                print("  FAILED: %s" % f)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        red = summary(1, ["probe one", "probe two"])
    check(red == 1 and "3 assertions, 2 failed" in buf.getvalue()
          and "  FAILED: probe two" in buf.getvalue(),
          "the footer reports failures by count and by name, and exits 1"
          "  <-- pinned defect")

    spec = importlib.util.spec_from_file_location(
        "reconverify_imported", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    cache_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = cache_before
    check(buf.getvalue() == "" and imported.lineage({}, 0) == [0],
          "importing the module prints nothing and exposes the core")

    shutil.rmtree(tmp, ignore_errors=True)
    return summary(passed[0], failed)


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="reconverify.py",
        description="Verify a no-post reconcile of a traditional version "
                    "from a fresh read of its delta tables.",
        epilog="Nothing is written without --apply.",
        allow_abbrev=False,
    )
    ap.add_argument("export", nargs="?", metavar="EXPORT",
                    help="directory of exported state, version and delta "
                         "tables")
    ap.add_argument("--edit-version", metavar="NAME",
                    help="the version to check, as named in versions.csv")
    ap.add_argument("--before", metavar="SNAPSHOT",
                    help="verify EXPORT, taken after the reconcile, against "
                         "this snapshot")
    ap.add_argument("--snapshot", metavar="PATH",
                    help="write the plan's snapshot here. Needs --apply")
    ap.add_argument("--oid-field", default=None,
                    help="object id column (default %s)" % DEFAULT_OID_FIELD)
    ap.add_argument("--key-field", default=None,
                    help="a business key that survives a reload, to count "
                         "returning rows exactly")
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help="object ids listed per finding (default %d)"
                         % DEFAULT_SAMPLE)
    ap.add_argument("--apply", action="store_true",
                    help="write the --snapshot file. Nothing else writes")
    ap.add_argument("--self-test", action="store_true",
                    help="run the assertions and exit")
    return ap.parse_args(argv)


def usage(message):
    print("error: %s" % message, file=sys.stderr)
    return 64


def main(argv=None):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means a file
        # could not be read. A mistyped --ap is a usage error, so it gets 64.
        return 64 if exc.code == 2 else exc.code

    if args.self_test:
        return self_test()
    if not args.export or not args.edit_version:
        return usage("an export directory and --edit-version are required. "
                     "Use --self-test to check the tool without them.")
    if args.sample < 0:
        return usage("--sample cannot be negative.")
    if args.apply and not args.snapshot:
        return usage("--apply needs --snapshot. Nothing else writes.")
    if args.before and (args.snapshot or args.oid_field or args.key_field):
        # The snapshot carries the fields it was taken with. A different key
        # here would compare two reads that were never comparable.
        return usage("--before takes no --snapshot, --oid-field or "
                     "--key-field. The snapshot carries its own.")

    try:
        if args.before:
            with open(args.before, "r", encoding="utf-8") as handle:
                snap = json.load(handle)
            check_snapshot(snap)
            export = load_export(args.export, snap["oid_field"],
                                 snap["key_field"])
            lines, findings = verify(snap, export, args.edit_version,
                                     args.sample)
        else:
            oid_field = args.oid_field or DEFAULT_OID_FIELD
            export = load_export(args.export, oid_field, args.key_field)
            lines, findings, snap = plan(export, args.edit_version, oid_field,
                                         args.key_field, args.sample)
    except ValueError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 3
    except (IOError, OSError, csv.Error) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    print("reconverify %s: %s" % ("verify" if args.before else "plan",
                                  args.export))
    for line in lines:
        print(line)
    for finding in findings:
        print("FINDING  %s" % finding)
    if args.before:
        print("VERDICT: %d finding(s). The reconcile is not verified."
              % len(findings) if findings else
              "VERDICT: verified from a fresh read. Own edits intact, nothing "
              "posted, no reload.")
    else:
        print("VERDICT: %d finding(s). Explain each before you reconcile."
              % len(findings) if findings else
              "VERDICT: nothing found that a reconcile would undo.")

    if args.snapshot:
        if not args.apply:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.snapshot)
        else:
            try:
                # Serialised first, so a failure cannot leave half a file.
                # "x" refuses an existing file: an old snapshot is the only
                # record of the version before its reconcile.
                text = json.dumps(snap, sort_keys=True)
                with open(args.snapshot, "x", encoding="utf-8") as handle:
                    handle.write(text)
            except (IOError, OSError) as exc:
                print("error: could not write %s: %s" % (args.snapshot, exc),
                      file=sys.stderr)
                return 2
            print("\nwrote %s" % args.snapshot)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
