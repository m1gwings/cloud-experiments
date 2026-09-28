# Contributor guidance

`cloud-experiments` owns disposable VM lifecycles, bounded cleanup, source and
runtime provenance, and durable transfer. EWS owns scientific execution, variant
selection, checkpoint validation/fallback, and intentional trajectory pruning.

- Inspect the selected EWS revision and its documented public recovery API before
  changing compatibility behavior. Keep version handling in `ews_contract.py`;
  never spread private EWS storage paths into cloud code or user documentation.
- Recovery v1 requires a stopped writer and a full local sealed snapshot. Keep
  graceful pause/resume separate from incremental remote transfer. Never describe
  the API as live snapshotting or hide its local copy cost.
- Publish only verified recovery commits. Upload replacement state before pruning,
  keep the preceding usable recovery point on failure, and restore into a new
  output directory. Cloud restores bytes; EWS decides reuse and rematerialization.
- Keep EWS recovery inventories logical. Cloud physical layouts may reuse loose
  blobs or immutable packs across commits; read old loose heads without migration.
  Verify each restored member, publish layouts before commits, and prune a pack
  only when no retained recovery needs any member. Leave unknown uploads alone.
- Preserve one writer per lineage, exact EWS pinning, contract compatibility,
  source/config and cloud implementation provenance, credential isolation, and
  bounded finalization/deletion. Small failure metadata must not depend on a large
  artifact transfer. Report absent-server `running` state as `interrupted`.
- Failure capsules belong to the cloud attempt namespace, outside EWS output.
  Keep them failure-only, versioned, bounded, redacted and best-effort. Complete
  diagnostic capture before cleanup when possible; a diagnostic timeout/failure
  must still lead to checked deletion. Discord is an alert, not the log store.
- Treat CPU worker allocation, synchronization interval, and timezone as
  operational controls. Use EWS's public resource/display validation; never edit
  study YAML or scientific identities to apply these controls.
- Keep modules focused and names concrete. Prefer behavior tests for recovery,
  interruption, publication ordering, leases, redaction, and cleanup. Remove dead
  compatibility scaffolding when it no longer serves an explicit supported path.
- Update README and the relevant setup/architecture/continuation guide when
  behavior changes. Document ownership and failure boundaries, not copied EWS
  implementation details.

Run `python3 -m unittest discover -s tests -v` and
`python3 -m compileall -q lib tests` before delivery. Tests must use fake providers,
temporary outputs, and local/fake rclone storage; never launch paid infrastructure
or send real Discord messages. Check the cloud-init payload remains within the
provider limit and all generated systemd units parse. After an authorized local
installation, use `./install` and the offline `cloud-doctor`.
