"""Repo-docs extraction pipeline (wizard-rebuild §3a-c).

Splits sentinel files fetched from a user-supplied git repo into
typed `DocChunk`s that the §3b intent tagger labels and the §3c
cache persists. The Git-mode BuildPlan extractor (§3 chunk 5)
reads these chunks to populate plan fields with provenance.
"""
