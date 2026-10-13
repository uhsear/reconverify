# reconverify

Verify a no-post reconcile of a traditionally versioned table from a fresh read. It rebuilds the
version's view from exported copies of the state tree and the delta tables, so the check never
asks the process that ran the reconcile whether the reconcile worked.

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
PASS  a unique prefix --ap is refused, not expanded  <-- pinned defect
...
PASS  the footer reports failures by count and by name, and exits 1  <-- pinned defect
PASS  importing the module prints nothing and exposes the core
--------------------------------------------------------------------
124 assertions, 0 failed
```

The full run prints all 124 assertions. Each `...` line above is where this block is cut.

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

On 2026-10-13 `gh search repos reconverify` returned only this repository. A second search, for
"reconcile verify versioned geodatabase", returned nothing. That shows no equivalent was found
on GitHub, not that none exists.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no
database. The geodatabase is read only through CSV exports that you make.

The self-test prints the same 124 assertions, line for line, on Windows (Python 3.13.2) and on
Ubuntu (Python 3.12.3). `coverage run --branch reconverify.py --self-test` reports 100 percent
of lines and branches on Windows. Coverage is not installed on the Ubuntu host, so it was not
measured there. Python 3.9 was not available on the machine that ran the last test round. The
code uses nothing newer than 3.9, but that is a reading of the code, not a run.

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

- **The version table needs a parent name in the same form as the name.** The 9.3 documentation
  for the SQL Server version table lists `name`, `owner`, `state_id`, `parent_name` and
  `parent_owner` (I read this through a search summary of Esri's 9.3 PDF, not from the PDF
  itself, and it is old). Name and owner are separate columns, so build `OWNER.NAME` yourself.
  Something like this, which has not been run against a live server:

  ```
  SELECT owner + '.' + name AS name, state_id,
         parent_owner + '.' + parent_name AS parent_name
  FROM sde.SDE_versions
  ```

  The default version has no parent and should export an empty `parent_name`. A version name
  must appear once, and the tool refuses a duplicate. Names are matched as written, so
  `owner.sandbox` does not find `OWNER.SANDBOX`.
- **The adds table has its own object id column.** Rename it in the export to the object id
  column of the base table, or pass `--oid-field` for the name the base table uses.
- **Every file must use the same columns and the same text format.** Leave out the shape column
  from every file, or export it as text the same way in every file. A geometry-only edit is
  invisible when the shape column is left out, because the tool compares rows as text.

Export the first set before the reconcile and the second set after it, by separate queries. The
tool refuses a second export whose files are byte for byte the first.

## Usage

```
python reconverify.py --self-test
python reconverify.py before/ --edit-version OWNER.SANDBOX
python reconverify.py before/ --edit-version OWNER.SANDBOX --key-field ROAD_KEY
python reconverify.py before/ --edit-version OWNER.SANDBOX --snapshot s.json --apply
python reconverify.py after/ --edit-version OWNER.SANDBOX --before s.json
```

| Flag | Default | What it does |
|---|---|---|
| `EXPORT` | none | The export directory. Required unless `--self-test`. |
| `--edit-version` | none | The version to check, as named in `versions.csv`. Required. |
| `--before` | off | Verify `EXPORT`, taken after the reconcile, against this snapshot. |
| `--snapshot` | off | Where the plan writes its snapshot. Needs `--apply`. |
| `--oid-field` | `OBJECTID` | The object id column of the exported tables. |
| `--key-field` | off | A business key that survives a reload. It makes the reload count exact. |
| `--sample` | `10` | How many object ids are listed after a finding. `0` lists none. |
| `--apply` | off | Write the `--snapshot` file. Nothing else writes. |
| `--self-test` | off | Run the assertions and exit. |

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap` is refused
with exit 64 instead of being read as `--apply`, and `--snap` and `--bef` are refused the same
way. A snapshot is never overwritten. Delete or move the old file first.

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
| 1 | At least one finding. |
| 2 | A file could not be read or written, including a snapshot that already exists. |
| 3 | Refused. The export is not a usable state tree, the after export is the snapshot's own read, a file is not UTF-8, or the snapshot is unusable. |
| 64 | Usage error, including an unknown or abbreviated flag. |

Exit 3 is deliberately not exit 1. A job that files a finding on exit 1 must not file one when
the tool could not make sense of its input.

## Limits

- The export contract is hand-built. The SQL above has not run against a live server, and the
  column names come from Esri's 9.3 documentation, which I could not re-read from the source.
  Check them against your release.
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
- Every export file is read whole into memory. A state chain of 30,000 states reads in about
  0.01 seconds, but a very large adds table is not what this is for.
- The self-test writes synthetic exports to a temporary directory and removes it at the end. That
  is not gated by `--apply`, because it never touches a geodatabase or any file you named. A
  crash mid-run can leave the directory behind.
- If the disk fills while the snapshot is being written, a partial file can remain, and the next
  run then refuses to overwrite it. The text is built in memory first, so only the write itself
  can fail.
- Line coverage is 100 percent on Windows. The one test that times itself (a 30,000-state chain,
  under 3 seconds) can fail on a very slow machine.

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
