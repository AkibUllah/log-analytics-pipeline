# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- CodeQL analysis for Python and GitHub Actions on pushes and pull requests to `main` and on a weekly schedule.
- Dependabot version updates for pip, Docker and GitHub Actions, with minor and patch updates grouped per ecosystem.
- README screenshots: an animated walkthrough of the live report, a report still and a CLI run.
- Roadmap items tracked as GitHub issues under the v0.2.0 milestone.

### Changed

- CI workflow runs with a read-only `GITHUB_TOKEN`.

## [0.1.0] - 2026-10-25

First public release of Log Analytics Pipeline.

### Added

- Parsers for nginx `combined` (with the common `$request_time` suffix) and line-delimited JSON logs with flexible key names; malformed lines are counted, never fatal; format auto-detection
- Enrichment: path normalisation (`/users/123/orders/9f1c...` to `/users/{id}/orders/{uuid}`, hashed static assets to `/static/{file}.js`), status class, device / browser / bot classification
- DuckDB storage with vectorised bulk loading (50,000 rows in under a second), per-minute series with gap filling, exact p50/p95/p99 via `quantile_cont`, top endpoints, slowest endpoints, top IPs, device and status breakdowns, Parquet export
- A mergeable t-digest-style `QuantileSketch` for streaming percentiles in follow mode (within 3% of exact on 50k lognormal samples in the tests)
- Incremental ingest with a JSON checkpoint (inode + byte offset): re-running `ingest` on the same file is a no-op, rotation and truncation restart cleanly, partial trailing lines are held until complete
- `follow` mode: asyncio `tail -F` that ingests new lines, scores each completed minute and prints alerts
- Anomaly detection: z-score against a trailing window for request rate and 5xx rate, with a spread floor so quiet traffic does not trigger on a single error and with anomalous minutes excluded from the baseline so a sustained incident keeps alerting
- Text report, JSON report and a self-contained HTML report with inline SVG charts (no JavaScript, no external assets)
- Realistic synthetic log generator (daily traffic curve, e-commerce/payments endpoint mix, bots, scanners, a 5xx incident and a flash crowd) for demos and tests
- Optional read-only FastAPI stats endpoint; Dockerfile and compose with a shared DuckDB volume

[Unreleased]: https://github.com/AkibUllah/log-analytics-pipeline/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AkibUllah/log-analytics-pipeline/releases/tag/v0.1.0
