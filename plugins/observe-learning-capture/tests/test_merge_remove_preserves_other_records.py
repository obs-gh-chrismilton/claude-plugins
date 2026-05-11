"""Regression test: remove_from_pending must preserve other records exactly.

The old implementation read the pending file via the YAML parser, filtered
out records matching the target id, then re-serialized the surviving
records back to the file. Two problems with that approach:

  1. Records the parser failed on (malformed YAML, unrecognized shapes)
     were silently dropped on rewrite. `_parse_yaml_list` already logs
     "skipped malformed YAML record" to stderr when it skips entries,
     but the rewrite then permanently truncates those entries from disk.

  2. Even valid records that round-tripped through the parser had their
     formatting normalized -- inline lists became block lists, quote
     styles changed, indentation could differ. Not data loss, but
     surprising and a `git diff` headache for users who version-control
     `~/.claude/`.

This file tests the byte-fidelity invariant: after removing one record,
all OTHER records' bytes in the file are identical to their original form.

Strict TDD: this test FAILS against the parse-and-reserialize implementation
because the renderer normalizes formatting. It PASSES against a
text-surgery implementation that splits on the same document-boundary
regex `_parse_yaml_list` uses and rejoins the non-matching chunks verbatim.
"""
import tempfile
import unittest
from pathlib import Path


# Three records with INTENTIONALLY VARIED formatting that the renderer
# would normalize on round-trip:
#   - inline tags list (`[a, b]` not block-style)
#   - mix of double-quoted and bare scalars
#   - explicit trailing whitespace inside the literal block
# A text-surgery rewrite preserves all of this verbatim. A parser+render
# rewrite normalizes it.
_THREE_RECORDS_VARIED = """\
---
id: aaaaaaaa
title: First record
fact: |
  First fact body
  spans two lines.
proposed_section: Test
confidence: high
tags: [opal, syntax]
source:
  session_id: s1
  cwd: /t
  captured_at: "2026-05-11T00:00:00+00:00"
  excerpt: "first excerpt"
---
id: bbbbbbbb
title: Second record (target of discard)
fact: Second fact (inline, short).
proposed_section: Test
confidence: high
tags: [other]
source:
  session_id: s2
  cwd: /t
  captured_at: "2026-05-11T00:01:00+00:00"
  excerpt: "second excerpt"
---
id: cccccccc
title: Third record
fact: |
  Third fact body.
proposed_section: Test
confidence: high
tags: [more, things]
source:
  session_id: s3
  cwd: /t
  captured_at: "2026-05-11T00:02:00+00:00"
  excerpt: "third excerpt"
"""


class TestRemoveFromPendingPreservesOtherRecords(unittest.TestCase):
    """The bytes of records OTHER than the target must survive intact."""

    def test_discarding_one_record_preserves_others_verbatim(self):
        """After discarding 'bbbbbbbb', the bytes representing 'aaaaaaaa'
        and 'cccccccc' must appear in the rewritten file unchanged.

        Specifically: their inline-list tags (`tags: [opal, syntax]`)
        must NOT have been normalized to block-list form
        (`tags:\\n  - opal\\n  - syntax`). The parse-and-reserialize
        implementation normalizes; the text-surgery implementation does not.
        """
        from pipeline.merge import remove_from_pending

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            pending.write_text(_THREE_RECORDS_VARIED, encoding="utf-8")
            original = pending.read_text(encoding="utf-8")

            # Sanity: confirm the input has the formatting we expect
            self.assertIn("tags: [opal, syntax]", original)
            self.assertIn("tags: [more, things]", original)

            remove_from_pending("bbbbbbbb", pending)

            after = pending.read_text(encoding="utf-8")

            # The other records' formatting must be preserved exactly.
            self.assertIn("tags: [opal, syntax]", after,
                "aaaaaaaa's inline tags list must survive byte-for-byte; "
                "the parse-and-reserialize implementation normalizes it to "
                "block-list form, which is the defect under test.")
            self.assertIn("tags: [more, things]", after,
                "cccccccc's inline tags list must survive byte-for-byte.")

            # The target record's content must be gone.
            self.assertNotIn("Second record (target of discard)", after)
            self.assertNotIn("id: bbbbbbbb", after)

    def test_discarding_preserves_malformed_records(self):
        """If the file contains a record the YAML parser cannot interpret,
        it MUST still survive a discard of an unrelated id.

        The current parse-and-reserialize implementation reads the parsed
        records (with malformed ones skipped + logged), then rewrites the
        file with only the parsed records. Result: malformed records are
        permanently lost on disk.

        A text-surgery implementation splits on the document-boundary regex
        without parsing record interiors, so malformed records pass through
        untouched.
        """
        from pipeline.merge import remove_from_pending

        # Construct a file where one "record" has no key:value lines the
        # parser recognizes. The parser will produce an empty dict {} or
        # skip it entirely; the rewrite will then drop or empty it.
        weird = """\
---
id: validone
title: Valid record
fact: Fact.
proposed_section: Test
confidence: high
tags: [t]
source:
  session_id: s
  cwd: /t
  captured_at: "2026-05-11T00:00:00+00:00"
  excerpt: e
---
this block has no proper YAML structure at all
just plain prose that the parser should skip
without losing the surrounding records on disk
---
id: another
title: Another valid record
fact: Another fact.
proposed_section: Test
confidence: high
tags: [t]
source:
  session_id: s
  cwd: /t
  captured_at: "2026-05-11T00:00:00+00:00"
  excerpt: e
"""
        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            pending.write_text(weird, encoding="utf-8")

            # Discard validone -- the weird middle block and the "another"
            # record must both survive in the rewritten file.
            remove_from_pending("validone", pending)
            after = pending.read_text(encoding="utf-8")

            self.assertIn(
                "this block has no proper YAML structure at all",
                after,
                "Malformed/unparseable record must survive a discard of "
                "an unrelated id. The parse-and-reserialize implementation "
                "drops it on rewrite; text-surgery preserves it."
            )
            self.assertIn("id: another", after,
                "Valid record AFTER the malformed one must also survive.")
            self.assertNotIn("id: validone", after,
                "The targeted record must be gone.")


if __name__ == "__main__":
    unittest.main()
