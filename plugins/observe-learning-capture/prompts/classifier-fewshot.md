# Few-shot examples for the Observe learning classifier

## YES — capture these

### Example 1: undocumented API behavior
Conversation excerpt:
> "Tried calling deleteDatastream(id:42767020) — returned validation error
> 'cannot delete datastream with active poller'. Had to delete the poller
> first via deletePoller(id:...) then the datastream call succeeded."

Output:
```yaml
- title: "Datastreams with pollers cannot be deleted directly"
  fact: |
    deleteDatastream rejects datastreams with active pollers; the poller
    must be deleted first via deletePoller(id:).
  proposed_section: "Object Management and Cleanup"
  confidence: high
  tags: [delete, datastream, poller, cascade]
```

### Example 2: OPAL syntax quirk
Conversation excerpt:
> "OPAL filter accepts `time > now() - 168h` but rejects `time > now() - 7d`
> with 'expected duration literal'."

Output:
```yaml
- title: "OPAL 'Nd' time literals rejected"
  fact: |
    OPAL duration literals like '7d' are rejected; use hours instead
    ('168h'). Also '14d' → '336h'.
  proposed_section: "OPAL Gotchas"
  confidence: high
  tags: [opal, syntax, time-literals]
```

## NO — do NOT capture these

### Example 3: customer-specific
Conversation excerpt:
> "EchoNet's tenant ID is 153283189978 and the main billing dataset is
> at /usage/Billing."

Reasoning: tenant ID and dataset path are customer-specific. They belong
in `~/Work/EchoNet/CLAUDE.md`, not in ObserveIE.md.

Output: `[]`

### Example 4: speculation
Conversation excerpt:
> "I think we might be able to bypass the cascade-ordering by using
> deleteFolder, but I haven't verified yet."

Reasoning: "I think… haven't verified" → not confirmed. Only capture
confirmed facts.

Output: `[]`

### Example 5: tier-1 documented behavior
Conversation excerpt:
> "OPAL has a `filter` verb that takes a boolean expression."

Reasoning: this is in the public OPAL docs. Only capture non-obvious or
gotcha-shaped facts.

Output: `[]`
