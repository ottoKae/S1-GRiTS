---
name: s1-data-handoff
description: Prepare or review a portable evidence package for an existing S1-GRiTS dataset, with processing semantics, batch provenance and explicit unknowns. Use for data handoff and reproducibility questions, not SSH operation or new production.
---

# S1-GRiTS data handoff

Read [the handoff procedure](../../../docs/DATA_HANDOFF_ZH.md), the delivered dataset's description, Catalog and available batch receipts. That document is the canonical public procedure; this entry is a reader aid and does not implement an exporter.

- Identify the exact dataset and affected tile/track/time range before making claims. Current repository code is not proof of historical execution.
- Distinguish input physical quantity, processing order, temporal aggregation domain and output units. Treat `VV_dB`, `raw`, package versions and processing signatures as declarations until connected to actual execution evidence.
- Check whether a root run summary describes only the last repair. Keep different batches and processing variants distinct.
- Reuse source manifests, sidecars and receipts; label metadata declarations, historical evidence, new checks and unresolved questions separately. Do not replace unknowns with inferred defaults.
- Export only relevant evidence with relative paths. Follow the public procedure's sanitization rules; retain original hashes separately from sanitized-file hashes.
- Validate the package contents and checksum manifest. Do not change production data or imply that a sampled pixel check covers the whole cube.

This public reference contains no private SSH configuration and does not depend on a workspace-specific skill installation. Users may point their agent to this file explicitly; automatic skill discovery depends on their agent setup.
