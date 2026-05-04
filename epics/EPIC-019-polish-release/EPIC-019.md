# EPIC-019: Polish, accessibility, documentation, public release

## Status: Not Started

## Overview

Final pre-release epic. Covers everything between "feature-complete" and "ready to put on GitHub for other people to use": accessibility audit, performance pass, comprehensive documentation, sample configs, security review, and the cut of the v1.0 release.

This epic is when the project transitions from "personal homelab tool" to "open-source product."

## Source documents

- Whole spec — particularly §1.1 (non-goals, double-check we haven't drifted), §10.5 (release flow), §16 (cross-cutting), §17 (open items — verify all decisions made during implementation).
- All previous epics' lessons-learned (per the `lessons-learned` skill triggered at each phase exit). Material here informs documentation gaps.

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-019-001 | Accessibility audit + fixes: invoke `impeccable:audit` skill, fix P0/P1 findings; keyboard-nav full pass; screen-reader pass; color-contrast pass |
| STAGE-019-002 | Performance pass: invoke `impeccable:optimize` skill, fix any P0/P1 findings; verify resource budget (§10.3) holds at the documented load |
| STAGE-019-003 | Polish pass: invoke `impeccable:polish` skill (alignment, spacing, micro-details); `impeccable:harden` skill (edge cases, i18n hygiene though we ship English-only) |
| STAGE-019-004 | Security review: invoke `oh-my-claudecode:security-reviewer` for OWASP top 10 audit; secret-leak scan across the entire codebase; SBOM generation; container image scan in CI |
| STAGE-019-005 | First-run UX: a fresh deploy after `git clone && docker compose up` should bring the user to a useful state in under 10 minutes, with documentation that walks them through bootstrap (master key generation, first user creation, secret seeding for HA/Pi-hole/Unifi/Synology, first probes appearing) |
| STAGE-019-006 | Comprehensive README: project description, screenshots, quickstart, architecture overview, contributing guide, security policy, code of conduct |
| STAGE-019-007 | API documentation: auto-generated from OpenAPI; published as a static site or in-repo |
| STAGE-019-008 | Plugin author documentation: how to write a collector, discoverer, channel, runbook, digest section; plugin-sdk-py walkthrough; published example plugins |
| STAGE-019-009 | Operator documentation: deployment guide, upgrade procedures, backup/restore procedures, troubleshooting, common pitfalls (e.g., master key loss) |
| STAGE-019-010 | Promote `plugin-sdk-py` to a published PyPI package |
| STAGE-019-011 | Cut v1.0 release: tag, GH Actions release.yml runs, container images published to GHCR, release notes auto-generated from CHANGELOG.md, GitHub release page populated |
| STAGE-019-012 | Post-release: announcement (r/homelab, r/selfhosted, lobste.rs, Hacker News if appropriate); set up an issue-template + good-first-issue labels |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **No regressions in any prior epic's tests.** Full integration suite passes.
- **No unresolved P0 or P1 findings** from any audit skill.
- **No secrets in git history** — verify with `gitleaks` or similar across the full history before tagging.
- **License clean** — every dependency has a compatible license; assemble a NOTICE / THIRD-PARTY file.
- **The user's host-overrides repo is genuinely separate** from the public release — verify by cloning the public repo fresh and confirming it's runnable without any private files.

## Dependencies

- All of EPICs 001–018 in their final state.

## Notes

- The "open-source product" framing means the public release prefers safe defaults over "this is how I personally configure it" defaults. This is captured in spec §16 (open-source-safe defaults).
- The host-overrides repo (the user's private "this is my actual deployment" config) must be genuinely independent. STAGE-019-005's "first-run UX" test is the verification: does the public repo, with no overrides, give a new user a complete usable experience?
- Documentation is the most under-estimated work in this epic. Plan for it to take the bulk of the effort.
