# GBrain HTTP MCP And Aliyun Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-query GBrain subprocess startup with a persistent local HTTP MCP service and add an independent Aliyun embedding retrieval fallback.

**Architecture:** Keep lexical retrieval always available, call GBrain over authenticated localhost HTTP MCP, and activate Aliyun semantic reranking when GBrain is unavailable or produces no authorized results. Use bounded TTL caches and a short circuit breaker so external failures do not dominate latency.

**Tech Stack:** Python 3.10+, FastAPI, urllib, SQLite/PostgreSQL, Bun/GBrain HTTP MCP, PowerShell.

---

### Task 1: External Embedding Provider
- [ ] Add failing tests for response validation, ten-item batching and caching.
- [ ] Add DashScope configuration fields.
- [ ] Implement the provider and make tests green.

### Task 2: GBrain Circuit Breaker
- [ ] Add failing tests for failure threshold, cooldown and reset.
- [ ] Implement breaker state and diagnostics.

### Task 3: Retrieval Fusion
- [ ] Add failing tests for semantic promotion and graceful failure.
- [ ] Trigger semantic reranking when GBrain produces no authorized results.
- [ ] Fuse lexical and semantic ranks and expose strategy metrics.

### Task 4: Persistent Service Scripts
- [ ] Add start, status and stop scripts.
- [ ] Add health checks, foreign-port rejection and durable logs.
- [ ] Document configuration and token creation.

### Task 5: Runtime Verification
- [ ] Configure the localhost MCP endpoint and token.
- [ ] Start GBrain and validate authenticated MCP access.
- [ ] Run backend tests, frontend build and script checks.
