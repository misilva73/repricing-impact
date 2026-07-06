"""repricing_impact — analysis package over the ClickHouse ``gas_analysis`` warehouse.

Shared helpers (promoted from the original EDA notebook, since removed, and
ported from ``repricing-forensics``) used by the precompute pipeline:

- :mod:`repricing_impact.clickhouse` — SQLAlchemy engine factory + query helper.
- :mod:`repricing_impact.opcodes` — opcode byte->mnemonic table + array decoders.
- :mod:`repricing_impact.labels` — known mainnet contract address labels.
- :mod:`repricing_impact.config` — chain/schedule constants, paths, pinned-config resolver.
"""

__version__ = "0.1.0"
