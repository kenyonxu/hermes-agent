# FIX: plugin transform_llm_output content suppressed on streaming platforms

**Date**: 2026-05-20  
**Author**: Kai.Xu  
**Upstream PR**: [#29119](https://github.com/NousResearch/hermes-agent/pull/29119)

---

## 1. Problem

Plugin hooks registered on `transform_llm_output` modify the final response AFTER streaming completes. However, three separate suppression points in the codebase silently discard the transformed content, making the hook effectively invisible on all streaming platforms (Discord, Telegram, ACP, etc.).

## 2. Root Cause

### Suppression Point 1: ACP Adapter

`acp_adapter/server.py:1537` — the `not streamed_message` guard skips `final_response` delivery when streaming was used:

```python
if final_response and conn and not streamed_message:  # ← blocks transformed content
    update = acp.update_agent_message_text(final_response)
```

### Suppression Point 2: Gateway Final-Send Gate

`gateway/run.py:17578` — the normal final send is suppressed when content was already streamed:

```python
if not _is_empty_sentinel and (_streamed or _previewed or _content_delivered):
    response["already_sent"] = True  # ← transformed content never sent
```

### Suppression Point 3: Field Stripping in run_sync()

`gateway/run.py:16939-16958` — `run_sync()` cherry-picks fields from the `run_conversation()` result dict into a new response dict. The `response_transformed` flag (set by the conversation loop when a hook modifies the response) was not included in the cherry-pick list:

```python
return {
    "final_response": final_response,
    "response_previewed": result.get("response_previewed", False),
    # response_transformed was MISSING here
}
```

This broke the data flow: conversation_loop → result dict → gateway response → suppression check.

## 3. Fix (5 coordinated changes)

### 3.1 `agent/conversation_loop.py`
- Initialize `_response_transformed = False` before the hook section
- Set `_response_transformed = True` when a `transform_llm_output` hook returns a non-empty string
- Include `"response_transformed": _response_transformed` in the result dict

### 3.2 `gateway/run.py` — `run_sync()` return dict
- Add `"response_transformed": result.get("response_transformed", False)` to the cherry-picked fields (both normal and error return paths)

### 3.3 `gateway/run.py` — suppression check
- When `response_transformed=True`, edit the existing streamed message in-place via the adapter's `edit_message()` with the full transformed content
- Set `already_sent=True` to prevent the normal send from creating a duplicate message

### 3.4 `gateway/stream_consumer.py`
- Expose `message_id` and `accumulated_text` as public read-only `@property` attributes so the gateway can access the streamed message identifier for editing

### 3.5 `acp_adapter/server.py`
- Remove the `not streamed_message` guard — the final response (possibly transformed by plugins) should always be delivered to the ACP client

## 4. Data Flow After Fix

```
LLM generates response → streamed to client (message_id=X)
transform_llm_output hook appends content → response_transformed=True
     ↓
conversation_loop: result["response_transformed"] = True
     ↓
gateway run_sync(): propagates response_transformed
     ↓
gateway suppression check: transformed=True, has message_id
     ↓
adapter.edit_message(chat_id, message_id, content=final_response_with_transform)
     ↓
already_sent=True → normal send suppressed (no duplicate)
```

## 5. Behavior

| Scenario | Before | After |
|----------|--------|-------|
| Plugin active, streaming | Transformed content silently dropped | Content appended via message edit |
| Plugin active, non-streaming | Works | Works (unchanged) |
| Plugin inactive | Normal delivery | Normal delivery (unchanged) |
| No plugin installed | Normal delivery | Normal delivery (unchanged) |

## 6. Verification

```bash
# Gateway trace log
grep "TRACE: final-send check" agent.log
# transformed=True  → message edited in-place, no duplicate
# transformed=False → original suppression path, no duplicate
```
