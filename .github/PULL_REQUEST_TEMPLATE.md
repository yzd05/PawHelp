## Summary

- **User-visible problem / motivation**:
- **What changed**:
- **Known limitations / non-goals**:

## Verification

<!-- Paste exact commands run and key output; report any unavailable local service honestly. -->

- [ ] `uv run python -m scripts.check_sources`
- [ ] `node scripts/check_frontend.cjs`
- [ ] `uv run pytest` (note whether `TEST_DATABASE_URL` was enabled)

```text
# verification output
```

## UI & compatibility checklist

- [ ] No `.env` credentials, private corpora, model weights, or customer data included.
- [ ] Database migrations, existing user data, and CLI contracts preserved (or migration path documented).
- [ ] If browser UI changed: verified both **desktop** and **narrow mobile** layouts plus the affected interactive flow (attach screenshots below).
