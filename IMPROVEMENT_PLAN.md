# Book Translator Pipeline — Full Analysis & Improvement Plan
# 2026-07-05

## Current Architecture

pipeline.py → sources/adapter → extract.py → translate.py (GLM-5.2) → assemble.py → publish.py → device_push.py

### Current Translation Flow
1. Input: list[Chapter] from extract.py
2. Batching: groups of 8 paragraphs
3. Per batch: GLM-5.2 API call, P-delimited output, split+realign
4. 2-Pass: proofread batch (original + draft -> polished)
5. Concurrency: ThreadPoolExecutor(max_workers=8)
6. Checkpointing: JSON, source hash validated

### Problems Identified

#### Critical
1. No HTML tag preservation (i, b, em destroyed)
2. No context continuity between batches (terminology drift)
3. Source quality issue (#38145 incomplete — 6/9 chapters missing)
4. urllib Request object reused across retries (data stream consumed)
5. Batch size too small (8 vs GLM-5.2 1M context capacity)

#### Important
6. No retry on proofread failure (silent quality drop)
7. No MCP integration opportunity used
8. No terminology auto-extraction
9. No quality scoring

## Improvement Plan

### Phase 1: Quick Wins (P0)
- HTML tag placeholder system
- Fix urllib Request reuse bug
- Batch size 8 to 20
- Source switch to #51935

### Phase 2: MCP Integration (P1)
- Auto-glossary builder via web_search_prime
- Batch context propagation
- Web reader for ambiguous passages

### Phase 3: Architecture (P2)
- Pre-translation analysis stage
- Quality scoring (back-translation)
- asyncio migration
