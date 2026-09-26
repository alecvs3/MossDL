"""Cross-client download benchmark harness (MossDL vs other download managers).

Standard library + psutil only. Run modules from ``docs/benchmarks``:

    python -m harness.server --help
    python -m harness.runner --help
    python -m harness.aggregate --help
"""

SCHEMA_VERSION = "mossdl-bench/1"
