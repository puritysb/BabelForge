# AGENTS.md

## 프로젝트 지침 (Codex / OpenCode / Antigravity & AI Agents)

이 프로젝트(**BabelForge** — 공용 도메인 책 → 이중언어 EPUB → OPDS 파이프라인)에서 작업하는 모든 AI
에이전트는 다음을 따르십시오.

### 1. 컨텍스트 파악 (필수)
- 작업 시작 전 **반드시 [`CLAUDE.md`](CLAUDE.md) 를 먼저 읽으십시오.** 아키텍처·핵심 파일·실행
  명령·외부 계약(cross-repo)·컨벤션/함정의 **단일 진실 공급원(SSOT)** 입니다.
- 사용자용 개요는 [`README.md`](README.md) 입니다.

### 2. 스킬 = 절차의 정본
- 책 번역 파이프라인 운영 절차(검색 → 선택 → 번역 → 배포)는 **skill** 입니다. 정본은
  [`.agents/skills/book-translator/SKILL.md`](.agents/skills/book-translator/SKILL.md) 입니다.
  - **Codex** 는 `.agents/skills/` 를 자동 발견합니다.
  - **Claude Code** 는 `.claude/skills/` 의 얇은 포인터를 통해 같은 정본에 도달합니다.
  - **OpenClaw** 게이트웨이 에이전트는 `~/.openclaw/workspace/skills/book-translator/` 의
    포인터로 이 레포의 정본을 참조합니다.
- 명령어를 직접 유추해 실행하지 말고 스킬의 절차를 따르십시오. 절차 내용을 여러 곳에 복붙하지
  마십시오(정본만 편집 — drift 방지).

### 3. 핵심 원칙 (요약 — 상세는 CLAUDE.md)
- **비밀정보:** `.env`(`GLM_API_KEY`)는 gitignore. 커밋·출력 금지. `config.py` 는 env 로만 읽음.
- **경로 이식성:** 모든 경로는 `__file__` 기반(`BASE_DIR`). 자기 경로를 하드코딩하지 말 것
  (형제 레포 `crosspoint-agentdeck` 참조만 home-relative 예외).
- **EPUB 포맷 = 계약:** 블록 레벨 `<p class="cp-original">`/`cp-translation` 만. `<span>` 금지.
  포맷 SSOT 는 `~/github/crosspoint-agentdeck/docs/bilingual-epub.md`(소비자=e-ink 펌웨어).
- **launchd:** `com.local.book-translator-watcher` 가 `auto_push_watcher.py` 를 20s 주기로 구동.
  디렉토리 이동 시 plist 경로 재지정 필요.
- **Anna's Archive:** `annas:` 접두어로만 opt-in(저작권=사용자 책임).
