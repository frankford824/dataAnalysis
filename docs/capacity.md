# Capacity planning

Select a profile from measured customer inputs, not workstation or design-customer hardware. The figures below are starting points; complete a representative load test with the largest expected file before production.

| Profile | Files/day | Largest file / rows | Retention | Concurrent users | Certified refresh | Local AI | Server baseline |
|---|---:|---:|---:|---:|---:|---|---|
| Small | up to 50 | 250 MB / 2 million | 2 years | 10 | daily or monthly | no | 8 vCPU, 32 GB RAM, 1 TB SSD |
| Medium | up to 500 | 1 GB / 10 million | 3 years | 50 | hourly or daily | optional separate GPU host | 16 vCPU, 64 GB RAM, 4 TB NVMe/SSD |
| Large | up to 5,000 | 5 GB / 50 million | 5 years | 200 | hourly | dedicated inference service | 32+ vCPU, 128+ GB RAM, 12+ TB NVMe/SSD |

Use `docker compose -f compose.yaml -f docs/config/compose.<profile>.yaml up -d` to apply conservative container limits. The database and object store need additional headroom for compaction, export, and restore. Keep at least 30% disk space free. Budget raw storage as `daily input × retention × 1.25`, then add intermediate/export retention and two full backup sets.

Increase worker concurrency only after measuring PostgreSQL connections, memory per Polars/DuckDB job, and object-store throughput. One 5 GB compressed workbook can require materially more than 5 GB RAM while being parsed. Large Excel sources should be converted at the source to CSV/Parquet where contractually possible.

Local AI is excluded from these baselines. Size it independently from the selected model's VRAM/RAM specification and isolate it from deterministic processing so model saturation cannot delay publication.
