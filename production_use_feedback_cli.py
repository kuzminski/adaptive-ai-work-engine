#!/usr/bin/env python3
"""Attach operator feedback to a production-use evidence session.

Minimal, UI-freeze-respecting mechanism (§23/§11 of the evidence-capture
brief): this talks straight to `production_use_evidence.EvidenceRecorder`, no
server required, so feedback can be attached to any session — one driven
through the live canvas, or one produced by a scripted evidence/test
harness — without touching the frozen live-canvas HTML/JS.

Usage:

    python production_use_feedback_cli.py <session_id> USEFUL \\
        --reuse-intent YES --comment "did exactly what I asked"

    python production_use_feedback_cli.py <session_id> NOT_USEFUL

Re-running for the same `session_id` records a new feedback row that
supersedes the prior one (append-only; nothing is overwritten in place).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

import production_use_evidence as puv


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_id")
    ap.add_argument("usefulness", choices=puv.USEFULNESS_VALUES)
    ap.add_argument("--reuse-intent", choices=puv.REUSE_INTENT_VALUES, default=None,
                    help="would the operator use this workflow again")
    ap.add_argument("--comment", default=None)
    ap.add_argument("--evidence-root", type=Path, default=None)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    recorder = puv.EvidenceRecorder(args.evidence_root)
    try:
        row = recorder.record_feedback(session_id=args.session_id, usefulness=args.usefulness,
                                       reuse_intent=args.reuse_intent, comment=args.comment)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(row, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
