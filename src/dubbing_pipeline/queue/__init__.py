"""
Queue backends (Level 2 Redis + Level 1 fallback).

This package provides a single canonical integration point used by:
- job submission (web/API) to enqueue new jobs
- the worker/executor loop to claim work and manage distributed locks

Redis is optional; when unavailable the system falls back to the existing in-proc queue.
"""

