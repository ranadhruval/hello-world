# R2D2 `/response` fixtures

**Synthetic until recorded.** Written from the shapes described in
`docs/research/SIGNAL_CONTRACT_REPLY.md` §2.4 (`tools_called` with tool input
and output), not from a live call.

Replace `response.json` with a real capture on the work laptop:

```bash
curl -s -X POST "$R2D2_BASE_URL/response" \
  -H 'content-type: application/json' \
  -H "Authorization: Bearer $R2D2_API_KEY" \
  -d '{"query":"why is TITAN down today"}' \
  | python -m json.tool > tests/fixtures/r2d2/response.json
```

Then `make test`. `tests/test_r2d2.py` runs against whatever is in this file, so
a shape mismatch shows up as a failing test with the key names printed, rather
than as a silent withhold in production.
