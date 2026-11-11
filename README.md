# reconverify

Verify a no-post reconcile of a traditionally versioned table from a fresh read. It rebuilds the
version's view from exported copies of the state tree and the delta tables, so the check never
asks the process that ran the reconcile whether the reconcile worked. It also lists, row by row,
the saved edits that never posted, and it flags nested versions that a reconcile did not reach.

A sandbox version holds the compress back, so a tech reconciles it with its parent and does not
post it. The edits are supposed to stay in the sandbox, the parent is supposed to stay as it was,
and the sandbox is supposed to stay the size it was. One run did none of that. The parent had
reloaded its copy of a table under new object ids while the reconcile ran. The sandbox took in
every reloaded row, grew several times over, and its owner's deletes did not hold anything back.

The check passed. It ran in the same process as the reconcile, read the version through a layer
that process already held open, and that layer still showed the rows from before. Nothing in the
check looked at the geodatabase again.

A second trap sits under the first. The usual way to find "the owner's edits" is to look at the
version's head state. A sandbox is edited over many states. The head state holds the last of
them, and the earlier deletes and updates are invisible to a head-state read. In the demo below,
a head-state read sees 1 of the 4 edits.

A third trap hides in the word clean. A version holds saved edits that nobody posted. The table
loads and the rows are there, so nothing looks wrong, yet the parent does not hold the edits. A
sandbox can also have versions nested under it. Reconciling the sandbox updates the sandbox, and
Esri's pages do not say that it reaches the versions nested under it. A run that covered only the
sandbox can look finished while a child version is still behind. This tool lists the unposted
rows from one fresh export, and it reads the state tree to find each nested version that no
reconcile has reached since its parent moved.

```
$ python reconverify.py --self-test
reconverify self-test: synthetic exports, no database, no network
--------------------------------------------------------------------
PASS  a lineage runs from the head back to state 0, nearest first
PASS  state 0 is its own whole lineage
...
PASS  a read of the head state alone sees 1 of the 4 own edits  <-- pinned defect
...
PASS  a parent reload over the owner's deletes is forecast  <-- pinned defect
...
PASS  a reconcile that posted is a finding  <-- pinned defect
PASS  a version that never moved, with a parent to pull, is a finding
PASS  but not when the parent is still at the common ancestor: nothing to pull in
PASS  a parent that moved after the plan, with the version unmoved, is a finding  <-- pinned defect
PASS  a version that moved on one more edit, without the parent pulled in, is a finding  <-- pinned defect
PASS  a post of delete-only edits is a finding  <-- pinned defect
...
PASS  a post that no exported row shows is read from the state tree  <-- pinned defect
...
PASS  a blank key is no key: a new road with a blank key does not match a deleted road with a blank key  <-- pinned defect
...
PASS  a parent row that repeats the key of an updated row is a finding  <-- pinned defect
...
PASS  an owner update of a row the parent deleted is forecast at the plan  <-- pinned defect
...
PASS  a parent that made the same edit as the owner is not a post  <-- pinned defect
...
PASS  verifying against the snapshot's own export is refused  <-- pinned defect
...
PASS  and lists each row with its key and the state that wrote it  <-- pinned defect
PASS  saved edits that never posted are a finding, not a clean run  <-- pinned defect
...
PASS  states that hold no visible edit are a finding, not a clean run  <-- pinned defect
...
PASS  a nested version a reconcile skipped is a finding, not a note  <-- pinned defect
...
PASS  a unique prefix --ap is refused, not expanded  <-- pinned defect
...
PASS  --unposted without --tree exits 1 for a skipped child  <-- pinned defect
...
PASS  verify exits 1 for a nested version the reconcile skipped, without any flag  <-- pinned defect
...
PASS  a unique prefix --unp is refused too  <-- pinned defect
...
PASS  the footer reports failures by count and by name, and exits 1  <-- pinned defect
PASS  importing the module prints nothing and exposes the core
--------------------------------------------------------------------
177 assertions, 0 failed
```

The full run prints all 177 assertions. Each `...` line above is where this block is cut.

## What already exists

Esri's own [Reconcile Versions](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/reconcile-versions.html)
tool is the tool to reach for first, and it does the reconcile itself. It can abort when it finds
conflicts (`abort_if_conflicts`), it detects conflicts by row or by attribute, it resolves them
for the side you choose, and it only posts when you ask (`with_post`, which defaults to no
post). It writes a log, which Esri describes as "an ASCII file containing the contents of the
geoprocessing messages". That log is what the tool says about its own run. It is not a second
read of the geodatabase, so it cannot show that a reload from the parent grew the version, or
that a post happened through some other session. This tool reads after the fact, from exports
taken by a different query.

For the unposted edits, Esri's Differences view (the Version Changes command) is the tool to
reach for. Esri says it lists the changes "made to the version from the time it and the chosen
target version were identical", as Insert, Delete and Update, and that it "lists the modified
layers and the features that were edited in those layers"
([View traditional version changes](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/using-the-version-changes-command.html)).
It does that inside Pro, for a version you are connected to, and each feature shows its Current, Target and
Common Ancestor values. The page does not say whether the differences can be exported or scripted.
This tool adds a different thing: a list you can diff and keep from an export, an exit code, a
count of the edits the parent already holds, and a check of every nested version in the same run.

On 2026-11-10 `gh search repos reconverify` returned only this repository. Two more searches,
"versioned geodatabase unposted edits" and "reconcile post verify versions arcgis", returned
nothing. That shows no equivalent was found on GitHub, not that none exists.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no
database. The geodatabase is read only through CSV exports that you make.

The self-test prints the same 177 assertions, line for line, on Windows (Python 3.13.2), on
Windows under Python 3.12.10, on Ubuntu (Python 3.12.3) and on Ubuntu under Python 3.9.25.
The Windows 3.9 run was not reproduced here: no Windows 3.9 interpreter was available.
`coverage run --branch reconverify.py --self-test` reports 100 percent of lines and branches on
Windows (918 statements, 196 branches). Coverage is not installed on the Ubuntu host, so it was
not measured there.

```
git clone https://github.com/uhsear/reconverify.git
```

## The export contract

You make the exports. The tool reads one directory of CSV files, as UTF-8 text with a header row.

| File | Columns it reads | Source |
|---|---|---|
| `states.csv` | `state_id`, `parent_state_id` | the state table, `SDE_states` |
| `versions.csv` | `name`, `state_id`, `parent_name` | the version table, `SDE_versions` |
| `T.base.csv` | the object id column and every other column, `T` is the table | the business table |
| `T.adds.csv` | the object id column, `SDE_STATE_ID`, and the same other columns | the adds table |
| `T.deletes.csv` | `SDE_DELETES_ROW_ID`, `SDE_STATE_ID`, `DELETED_AT` | the deletes table |
| `T.client.csv` | the object id column and the same other columns, optional | the table as a client read it |

The delta tables are documented by Esri. The adds table holds "the state to which the feature
belongs". In the deletes table, `SDE_state_ID` is "the state from which the row is being
deleted", `SDE_Deletes_Row_ID` is "the unique ID of the row being updated or deleted", and
`Deleted_At` is "the state that is making the modification"
([ArcSDE delta tables, 10.0](https://help.arcgis.com/en/geodatabase/10.0/sdk/arcsde/concepts/versioning/basicprinciples/deltatables.htm)).
An insert writes a row to the adds table, a delete writes one to the deletes table, and an
update writes to both
([Edit versioned data with SQL](https://desktop.arcgis.com/en/arcmap/latest/manage-data/using-sql-with-gdbs/overview-edit-versioned-data-with-sql.htm)).
The tool therefore reads a row as the pair (object id, state it was written at). The pair is live
when its state is in the version's lineage and no delete made in that lineage names it. Esri
defines the common ancestor as "a representation of the data when the edit version and its
parent version were identical"
([Versioning vocabulary](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/versioning-vocabulary.html)).

Three things about the exports are not guaranteed, and the tool enforces what it can:

- **The version table needs a parent name in the same form as the name.** Esri's system table
  page for the geodatabase says the versions table holds "a name, owner, status (public or
  private), state ID, and description", and the states table holds the "state ID of the parent
  state"
  ([System tables, 10.0](https://help.arcgis.com/en/geodatabase/10.0/sdk/arcsde/concepts/schemaobjects/tables/systemtables.htm)).
  That page does not name a parent version column. I could not confirm one from Esri's current
  pages, so this repository gives no SQL for it. Look at the columns of your own versions table
  (`SELECT TOP 1 * FROM sde.SDE_versions` on SQL Server), and build `OWNER.NAME` for the name and
  for the parent name yourself. Esri says the system tables may be viewed with SQL and must not
  be altered with anything but ArcGIS software
  ([System tables in PostgreSQL](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/manage-postgresql/geodatabase-system-tables-postgresql.html)).
  The default version has no parent and should export an empty `parent_name`. A version name
  must appear once, and the tool refuses a duplicate. Names are matched as written, so
  `owner.sandbox` does not find `OWNER.SANDBOX`.
- **The adds table has its own object id column.** Rename it in the export to the object id
  column of the base table, or pass `--oid-field` for the name the base table uses.
- **Every file must use the same columns and the same text format.** Leave out the shape column
  from every file, or export it as text the same way in every file. A geometry-only edit is
  invisible when the shape column is left out, because the tool compares rows as text. Under
  `--unposted` such a row reads as a put-back row and is a finding.

Export the first set before the reconcile and the second set after it, by separate queries. The
tool refuses a second export whose files are byte for byte the first.

## Usage

```
python reconverify.py --self-test
python reconverify.py before/ --edit-version OWNER.SANDBOX
python reconverify.py before/ --edit-version OWNER.SANDBOX --key-field ROAD_KEY
python reconverify.py before/ --edit-version OWNER.SANDBOX --snapshot s.json --apply
python reconverify.py after/ --edit-version OWNER.SANDBOX --before s.json
python reconverify.py now/ --edit-version OWNER.SANDBOX --unposted
python reconverify.py now/ --edit-version OWNER.SANDBOX --unposted --tree
```

| Flag | Default | What it does |
|---|---|---|
| `EXPORT` | none | The export directory. Required unless `--self-test`. |
| `--edit-version` | none | The version to check, as named in `versions.csv`. Required. |
| `--before` | off | Verify `EXPORT`, taken after the reconcile, against this snapshot. |
| `--snapshot` | off | Where the plan writes its snapshot. Needs `--apply`. |
| `--oid-field` | `OBJECTID` | The object id column of the exported tables. |
| `--key-field` | off | A business key that survives a reload. It makes the reload count exact. |
| `--sample` | `10` | With a plan or `--before`, how many object ids are listed after a finding. `0` lists none. |
| `--unposted` | off | List the edits the version holds that its parent does not, row by row. Reads one export. Writes nothing. |
| `--tree` | off | With `--unposted`, also list the unposted rows of every version nested under `--edit-version`. |
| `--rows` | `100` | With `--unposted`, rows listed per table. `0` lists every row. |
| `--apply` | off | Write the `--snapshot` file. Nothing else writes. |
| `--self-test` | off | Run the assertions and exit. |

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap` is refused
with exit 64 instead of being read as `--apply`, and `--snap` and `--bef` are refused the same
way. A snapshot is never overwritten. Delete or move the old file first.

`--unposted` takes no `--before`, `--snapshot` or `--apply`. `--rows`, `--sample` and
`--tree` need the mode that uses them: `--rows` and `--tree` need `--unposted`, and `--sample` is refused with it. A flag that would do nothing is a usage error, exit 64.

`--before` takes no `--snapshot`, `--oid-field` or `--key-field`. The snapshot carries the fields
it was taken with, and a different key would compare two reads that were never comparable.

## The plan: before the reconcile

The plan reads the first export and names what a reconcile would put at risk.

```
$ python reconverify.py before_conflict --edit-version OWNER.SANDBOX
reconverify plan: before_conflict
version OWNER.SANDBOX at state 12, parent OWNER.EDITS at state 2, common ancestor 1
table ROADS
  version view          9 rows
  own edits             1 inserted, 1 updated, 2 deleted
  head state alone      1 of those 4
  parent since ancestor 0 inserted, 0 updated, 1 deleted
  client read           none in the export
FINDING  ROADS: 1 row(s) were edited by the owner and also edited or deleted in the parent since the ancestor, e.g. 4. A row-level reconcile reports each as a conflict, and the side it favours overwrites the other
VERDICT: 1 finding(s). Explain each before you reconcile.
```

The owner's edits are counted across every state since the common ancestor, and the line
`head state alone` shows how many of them a head-state read would have found. A finding is
raised for:

- **A conflict.** The owner and the parent both edited a row since the ancestor, as an update
  against an update, an update against a delete, or a delete against an update. These are the
  conflict types Esri lists
  ([Conflicts, REST API](https://developers.arcgis.com/rest/services-reference/enterprise/conflicts/)).
  Esri's default is to define conflicts by row: "Any changes to the same row or feature in the
  parent and child versions will conflict during reconcile", and in a traditional version the
  default resolution favours the target version
  ([Reconcile Versions](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/reconcile-versions.html)).
  A delete that both sides made is not listed. Column-level detection is not modelled.
- **A reload.** With `--key-field`, a row that arrives in the version under a new object id with
  the key of a row the owner deleted, or the key of a row the owner changed. The second case
  leaves the version holding the key twice. A blank key is a hole in the data and matches
  nothing. Without `--key-field`, the signal is weaker: the parent deleted rows the owner also
  deleted while new rows arrive.

```
$ python reconverify.py before_reload --edit-version OWNER.SANDBOX --key-field ROAD_KEY
...
FINDING  ROADS: 2 arriving row(s) carry the ROAD_KEY of a row the owner deleted, e.g. 200, 201. The owner's deletes do not hold them back
VERDICT: 1 finding(s). Explain each before you reconcile.
```

- **A client read that disagrees.** If `T.client.csv` is present, it must match the rows derived
  from the delta tables. A client that still shows old rows is the failure in the story above.

## The verify: after the reconcile

`--before s.json` reads the second export and compares it with the snapshot.

```
$ python reconverify.py after_clean --edit-version OWNER.SANDBOX --before snap.json
reconverify verify: after_clean
version OWNER.SANDBOX state 12 -> 20, parent OWNER.EDITS state 2 -> 2, common ancestor 1 -> 2
table ROADS
  version view          9 -> 9 rows, 0 arrived
  own deletes kept      2 of 2
  own rows identical    2 of 2
  own rows in parent    0
  client read           none in the export
VERDICT: verified from a fresh read. Own edits intact, nothing posted, no reload.
```

A finding is raised when any of these hold:

- **The reconcile did not run.** A reconcile leaves the parent's head state in the version's own
  lineage. A version that never moved is checked against the parent as it is now. A version that
  moved is checked against the parent as the plan saw it.
- **An owner delete came back, or an owner change was undone.**
- **Something posted.** A post makes the parent hold the version's head state. The tool reads the
  state tree for this, so a post of delete-only edits is caught, and so is a post whose rows sit
  in a table that was not exported. It also compares rows: a changed row that is now in the
  parent, and was not there at the plan, is named. A parent that made the same edit as the owner
  before the plan is not a post.
- **A reload.** Rows arrived that carry a key the owner deleted or changed, with the same rules
  as the plan.
- **A client read that disagrees** with the derived view.

```
$ python reconverify.py after_lost --edit-version OWNER.SANDBOX --before snap.json
...
FINDING  ROADS: 1 row(s) the owner deleted are back, e.g. 2
FINDING  ROADS: 1 row(s) the owner changed are changed or missing, e.g. 4
VERDICT: 2 finding(s). The reconcile is not verified.

$ python reconverify.py after_posted --edit-version OWNER.SANDBOX --before snap.json
...
FINDING  ROADS: 2 row(s) the owner changed are now in the parent OWNER.EDITS, e.g. 4, 101. Something posted
VERDICT: 1 finding(s). The reconcile is not verified.

$ python reconverify.py del_posted --edit-version OWNER.SANDBOX --before dsnap.json
...
FINDING  ROADS: 2 row(s) the owner deleted are gone from the parent OWNER.EDITS, e.g. 2, 3. Something posted
VERDICT: 1 finding(s). The reconcile is not verified.

$ python reconverify.py after_idle --edit-version OWNER.SANDBOX --before early.json
...
FINDING  OWNER.SANDBOX: the parent is at state 2, which the version does not hold, yet the version still points at state 12. The reconcile did not run, or did not finish
VERDICT: 1 finding(s). The reconcile is not verified.
```

A second export that is byte for byte the snapshot's export is refused, because it is the same
read:

```
$ python reconverify.py before_quiet --edit-version OWNER.SANDBOX --before snap.json
refused: every file in this export is byte for byte the export the snapshot was taken from. That is the same read, not a fresh one. Export again after the reconcile
```

## Unposted rows: what never posted

`--unposted` reads one export, taken now, and lists what the version holds that its parent does
not. The headline comes from the state tree: how many states of the version the parent lacks.
The list comes from the rows. An own edit is listed when the parent does not hold it:

- an inserted row, when the parent has no identical row,
- an updated row, when the parent's row differs from the version's,
- a deleted row, when the parent still holds it.

An own edit is counted and not listed when the parent already holds the same row, or has already
deleted the row. A row whose exported columns match the ancestor's is counted and is also a finding:
the owner put it back, or only a column that was not exported changed (a vertex move, with the
shape column left out). The tool cannot tell which.
A row the parent also edited since the ancestor says so. That is a conflict waiting for the post.

```
$ python reconverify.py nested_before --edit-version OWNER.SANDBOX --unposted --key-field ROAD_KEY
reconverify unposted: nested_before
version OWNER.SANDBOX at state 12, parent OWNER.EDITS at state 2, common ancestor 1
  states the parent does not hold  3
table ROADS
  unposted              4 row(s): 1 inserted, 1 updated, 2 deleted
  already in the parent 0, same as the ancestor 0
    inserted 101  ROAD_KEY R-101  written at state 12
    updated  4  ROAD_KEY R-04  written at state 11
    deleted  2  ROAD_KEY R-02
    deleted  3  ROAD_KEY R-03
FINDING  ROADS: 4 edit(s) saved in OWNER.SANDBOX were never posted to OWNER.EDITS. The rows are listed above
VERDICT: 1 finding(s). Saved edits did not post, or the tool could not tell. 2 nested version(s) under OWNER.SANDBOX, 0 behind their parent, have no rows listed. Add --tree.
```

The verdict carries a sentence for the nested versions whose rows the run did not list. The sandbox above
has two, so the sentence names them. After a no-post reconcile the sandbox's edits are still
unposted, so this list is the one to read when you ask "did anything post?". A version that the
parent holds in full has nothing to post:

```
$ python reconverify.py posted_after --edit-version OWNER.SANDBOX --unposted
reconverify unposted: posted_after
version OWNER.SANDBOX at state 20, parent OWNER.EDITS at state 20, common ancestor 20
  nothing to post: the parent holds every state of the version
VERDICT: no unposted edit in the exported tables.
```

A version whose states hold no edit that any exported table shows is a finding. The tool cannot
tell an empty state from an edit in a table that it was not given, so it does not say clean:

```
$ python reconverify.py idle_states --edit-version OWNER.SANDBOX --unposted
reconverify unposted: idle_states
version OWNER.SANDBOX at state 12, parent OWNER.EDITS at state 2, common ancestor 1
  states the parent does not hold  3
table ROADS
  unposted              0 row(s): 0 inserted, 0 updated, 0 deleted
  already in the parent 0, same as the ancestor 0
FINDING  OWNER.SANDBOX: holds 3 state(s) the parent does not, and no exported table shows an edit in them. The edits sit in a table that was not exported, or the states hold none. The tool cannot tell which
VERDICT: 1 finding(s). Saved edits did not post, or the tool could not tell.
```

The list is capped at `--rows` rows per table, and the output says how many rows it left out.
`--rows 0` lists every row.

## Nested versions: what a reconcile skipped

The version tree comes from the parent names in `versions.csv`. A version nested under the one you
name is a child, a grandchild and so on. Esri defines a child version as "the enterprise
geodatabase version that was created from another version"
([Versioning vocabulary](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/versioning-vocabulary.html)).

Esri's Reconcile Versions page describes the edit version as the one "reconciled with the selected
target version", and says its dialog shows versions only in that ancestry: "Only versions that
are in the direct ancestry of the selected target version will be displayed"
([Reconcile Versions](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/reconcile-versions.html)).
It does not say whether the `ALL_VERSIONS` mode reaches versions nested under a version that is
not the target. The tool therefore does not assume. It reads the outcome. A nested version is
behind when its state lineage does not hold the state its parent now points at. That means no
reconcile of it has run since the parent moved. Esri says a post needs the target to be
unchanged since the last reconcile: "You can only post changes if the target version has not been
modified since you last reconciled changes"
([Reconcile and post edits to a traditional version](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/reconcile-and-post-edits-to-a-version.html)).
By that rule, a child that is behind cannot post to its parent until it reconciles again.

Each nested version that is behind is a finding, with or without a flag, in `verify` and in
`--unposted`, so a job that reads only the exit code sees a skipped child. `--tree` is an
`--unposted` option that also lists the unposted rows of every nested version against its own
parent. The plan names the nested versions and does not judge them. They are judged after the
reconcile.

```
$ python reconverify.py nested_after --edit-version OWNER.SANDBOX --before snap.json
...
nested versions        2, 1 behind their parent
FINDING  OWNER.CHILD: behind its parent OWNER.SANDBOX. Its state 13 does not hold state 20, where the parent now points. A reconcile run did not reach it, or ran before the parent moved
VERDICT: 1 finding(s). The reconcile is not verified.
```

A tree reconciled top down, the sandbox first and then each child in turn, holds at every level:

```
$ python reconverify.py nested_top --edit-version OWNER.SANDBOX --before snap.json
...
nested versions        2, 0 behind their parent
VERDICT: verified from a fresh read. Own edits intact, nothing posted, no reload.
```

## Nothing writes without --apply

The only file the tool writes is the snapshot, and only with `--apply`:

```
$ python reconverify.py before_quiet --edit-version OWNER.SANDBOX --snapshot snap.json
...
Check only. snap.json was not written. Re-run with --apply.

$ python reconverify.py before_quiet --edit-version OWNER.SANDBOX --snapshot snap.json --apply
...
wrote snap.json
```

The snapshot is opened with mode `x`, so an existing file is refused with exit 2 and is never
overwritten. The snapshot stores the exact `--key-field` values of every row the owner deleted or
changed. Point `--key-field` at a business key, not at a column that holds personal data, and
treat the snapshot as sensitive as that column.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Nothing found. |
| 1 | At least one finding. A nested version that is behind is one, in every mode. With `--unposted`, so is an unposted edit, a row that matches its ancestor, or an export the tool could not read an edit from. |
| 2 | A file could not be read or written, including a snapshot that already exists. |
| 3 | Refused. The export is not a usable state tree, the after export is the snapshot's own read, a file is not UTF-8, or the snapshot is unusable. |
| 64 | Usage error, including an unknown or abbreviated flag. |

Exit 3 is deliberately not exit 1. A job that files a finding on exit 1 must not file one when
the tool could not make sense of its input.

## Limits

- The export contract is hand-built. No SQL in this repository has run against a live server. The
  parent column of the versions table is not named on any Esri page I could read, so you build
  `parent_name` from your own release. The delta table column names come from Esri's 10.0 pages.
- A post is read from the state tree when the plan saw an edit. A version with no edits at the
  plan cannot be shown to have posted, and a sandbox with no edits verifies as `0 of 0`, exit 0.
- The post test assumes a post leaves the parent holding the version's head state. Esri says only
  that "the post process pushes changes from an edit version to the target version"
  ([Versioning vocabulary](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/versioning-vocabulary.html)),
  so that model is mine, and a post that leaves a different state tree is not caught by it.
  The row comparison still catches a posted change or delete that is visible in the rows.
- Edits made after the plan are not in the snapshot, so `own edits intact` covers only the edits
  the plan named.
- Only tables that have a `T.base.csv` are checked. An edit in a table that was not exported is
  invisible, apart from the post test above. A table that is in the second export and not in the
  snapshot is named in the output and not checked.
- The row comparison is by text. The client read and the delta tables must come from one
  formatter. A client file that writes NULL as `None` where the delta export writes an empty
  cell, or `1.0` where the other writes `1`, is reported as differing on every such row. A
  client read that lacks a column the base table has is refused with exit 3.
- A reconcile that merges conflicts by column rather than by row changes the owner's row, so
  verify reports the row as changed. The tool understands row-level conflicts only.
- The freshness test compares file bytes. An export that differs by one byte from the snapshot's
  export passes it. A byte-different re-export of an unchanged state counts as fresh. The tool
  cannot prove that the second export came from a different process.
- The original reconcile script also raised a problem when a table's row count jumped by more
  than 10 percent. This tool prints `N -> M rows, K arrived` and flags growth only when it
  matches a key or an owner delete. A parent reload that overlaps none of the owner's rows grows
  the version with no finding.
- After a compress, the states the snapshot names may be gone. A snapshot whose old common
  ancestor is not in the new lineage is refused with exit 3. Verify before the compress.
- A zero-byte CSV with no header row, such as an empty deletes table from an export that omits
  headers, is refused with exit 3 and `has no column`. Write the header even for an empty table.
- A CSV cell larger than 128 KB, such as long geometry text, raises `csv.Error` and the run exits
  2.
- Every export file is read whole into memory. Walking the lineage of a 30,000-state chain takes
  about 0.01 seconds. That is not a timing of the whole export load, and a very large adds table
  is not what this is for.
- The self-test writes synthetic exports to a temporary directory and removes it at the end. That
  is not gated by `--apply`, because it never touches a geodatabase or any file you named. A
  crash mid-run can leave the directory behind.
- If the disk fills while the snapshot is being written, a partial file can remain, and the next
  run then refuses to overwrite it. The text is built in memory first, so only the write itself
  can fail.
- Line and branch coverage is 100 percent on Windows and was not measured on Ubuntu. The one test that times itself (a 30,000-state chain,
  under 3 seconds) can fail on a very slow machine.
- `--unposted` reads one export and has no snapshot to compare it with, so it cannot tell a stale
  export from a fresh one. Export it by a query of its own, after the last edit you care about.
  This tool never starts a child process or opens a geodatabase. The fresh read is the export you
  make, and no exporter is included, because the one in Pro needs `arcpy`.
- An unposted row is a row the parent does not hold in the same text form. A parent row that
  matches by chance counts as already held. A row the owner inserted and then deleted inside the
  version leaves no trace, so it is neither listed nor counted.
- A version with states since the ancestor and no edit in any exported table is a finding. The
  tool cannot tell an edit in a table it was not given from a state that holds no edit. A sandbox
  that was reconciled with nothing to carry can therefore exit 1 under `--unposted`.
- A nested version is behind when its lineage lacks its parent's head state. The tool cannot know
  whether the run was meant to reach it, so a child that you left alone on purpose is reported. That a reconcile leaves the parent's head in the version's own lineage
  is the tool's model of the state tree, shown on synthetic exports and not on a live server.
- Esri's pages do not say whether `ALL_VERSIONS` reaches versions nested under a version that is
  not the target. This repository did not run the Esri tool, so it makes no claim about what that
  tool did on any run. It checks the outcome that the exports show.
- The Differences view lists changes against a chosen target. This tool compares a version only
  with its own parent.
- A row the owner put back to its ancestor content is counted and never listed, even when the
  parent edited that row since the ancestor.
- A shape-only edit with the shape column left out of the export reads as a put-back row, so it
  is a finding that the tool cannot explain further.
- After a no-post reconcile the sandbox's own edits are unposted by design, so a correct
  reconcile still exits 1 under `--unposted`.
- `--unposted` cannot judge a table that it was not given once any exported table shows an edit
  the parent already holds.
- `--edit-version` must name a version that has a parent. The default version is refused with
  exit 3, so `--unposted` cannot sweep every version from the root.
- A version that moved and whose parent then moved after the reconcile is checked only against the
  parent's state at the plan.
- Column names are matched in upper case and by case. A PostgreSQL export with lower-case headers
  is refused with exit 3 (`has no column`), and `--oid-field` changes only the object id column.
- Output is printed with the console's encoding. A key, version or table name that the encoding
  cannot print raises `UnicodeEncodeError`, and the run exits 1, which a job reads as a finding.
  Set `PYTHONUTF8=1`.
- Any uncaught exception exits 1, the same code as a finding. A snapshot edited by hand into the
  wrong value types, with its keys and format tag intact, can crash the run that way instead of
  exit 3.
- Values from the export are printed as they are, so a control or escape character in a key or
  name reaches the terminal.
- Do not write the snapshot into the export directory, because the snapshot would then count as
  an export file in the freshness test. The tool does not restrict the snapshot's file
  permissions.
- The Windows 3.9 run of the self-test is not reproduced here. The 3.9.25 run was on Ubuntu.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [compressfloor](https://github.com/uhsear/compressfloor) - name what holds an enterprise
  geodatabase's compress floor. When it names a sandbox, this tool is the check to run before
  you reconcile and clear it.
- [replicakeep](https://github.com/uhsear/replicakeep) - plan, then repair, an idle replica that
  may hold the compress floor.
- [sdeindex](https://github.com/uhsear/sdeindex) - plan or audit an index cleanup for an
  enterprise geodatabase in SQL Server, including the adds table that this tool reads.
