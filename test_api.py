#!/usr/bin/env python3
"""
NextGenAMS Agent Engine — Quick Test Script
Run: python test_api.py

AUTH_ENABLED=false (local dev):
    No token needed — runs as mock user devuser001

AUTH_ENABLED=true (staging/prod):
    Set token in .env.test or pass via environment:
    TEST_AUTH_TOKEN=your_jwt_token python test_api.py
"""

import httpx
import json
import os
import sys

BASE_URL = "http://localhost:8080/api/v1"

# ── Auth token ────────────────────────────────────────────────────────────────
# If AUTH_ENABLED=false → leave empty, no token needed
# If AUTH_ENABLED=true  → set TEST_AUTH_TOKEN env var
TOKEN = os.environ.get("TEST_AUTH_TOKEN", "")

def _headers() -> dict:
    """Returns headers — includes Authorization only if token is set."""
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers


# ── Print helpers ─────────────────────────────────────────────────────────────
def print_ok(msg):   print(f"  ✅ {msg}")
def print_fail(msg): print(f"  ❌ {msg}")
def print_info(msg): print(f"  ℹ️  {msg}")
def print_step(msg): print(f"\n{'─'*55}\n{msg}\n{'─'*55}")


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_health():
    print_step("Step 1 — Health Check")
    r = httpx.get(f"{BASE_URL}/health")
    if r.status_code == 200:
        data = r.json()
        print_ok(f"Status: {data['status']} | Env: {data['env']}")
    else:
        print_fail(f"Health check failed: {r.status_code}")
        sys.exit(1)


def test_readiness():
    print_step("Step 2 — Readiness Check (MongoDB + Redis)")
    r = httpx.get(f"{BASE_URL}/health/ready")
    data = r.json()
    for service, status in data.get("checks", {}).items():
        if status == "ok":
            print_ok(f"{service}: {status}")
        else:
            print_fail(f"{service}: {status}")


def create_session() -> str:
    print_step("Step 3 — Create Session")
    r = httpx.post(
        f"{BASE_URL}/chat/sessions",
        headers=_headers(),
    )
    if r.status_code == 200:
        data       = r.json()
        session_id = data["conversation_id"]
        print_ok(f"Session created: {session_id}")
        print_info(f"User: {data.get('user_id', 'unknown')}")
        return session_id
    else:
        print_fail(f"Failed: {r.status_code} {r.text}")
        sys.exit(1)


def chat_sync(session_id: str, message: str) -> dict:
    r = httpx.post(
        f"{BASE_URL}/chat/sync",
        headers=_headers(),
        json={"session_id": session_id, "message": message},
        timeout=60.0,
    )
    if r.status_code == 200:
        return r.json()
    else:
        print_fail(f"HTTP {r.status_code}: {r.text}")
        return {}


def test_greeting(session_id: str):
    print_step("Step 4 — Greeting Test")
    print_info("Message: 'Hello'")
    result = chat_sync(session_id, "Hello")
    if result.get("content"):
        print_ok(f"Response: {result['content'][:200]}")
    else:
        print_fail("No response received")


def test_it_question(session_id: str):
    print_step("Step 5 — IT Support Question")
    msg = "My SAP login is not working, I get an authentication error"
    print_info(f"Message: '{msg}'")
    result = chat_sync(session_id, msg)
    if result.get("content"):
        print_ok(f"Response: {result['content'][:300]}")
        if result.get("ticket_url"):
            print_ok(f"Ticket URL: {result['ticket_url']}")
    else:
        print_fail("No response received")


def test_followup(session_id: str):
    print_step("Step 6 — Follow-up Test (Memory)")
    msg = "I tried that but it still doesn't work"
    print_info(f"Message: '{msg}'")
    result = chat_sync(session_id, msg)
    if result.get("content"):
        print_ok(f"Response: {result['content'][:300]}")
    else:
        print_fail("No response received")


def test_out_of_scope(session_id: str):
    print_step("Step 7 — Out of Scope Test")
    msg = "What is the capital of France?"
    print_info(f"Message: '{msg}'")
    result = chat_sync(session_id, msg)
    if result.get("content"):
        print_ok(f"Response: {result['content'][:200]}")
    else:
        print_fail("No response received")


def test_title(session_id: str):
    print_step("Step 8 — Verify Title Auto-Generation")
    r = httpx.get(
        f"{BASE_URL}/chat/sessions",
        headers=_headers(),
    )
    if r.status_code == 200:
        sessions = r.json()
        current  = next((s for s in sessions if s["conversation_id"] == session_id), None)
        if current:
            title = current["title"]
            if title != "New Conversation":
                print_ok(f"Title generated: '{title}'")
            else:
                print_fail("Title still 'New Conversation' — not updated")
        else:
            print_fail("Session not found in list")
    else:
        print_fail(f"Failed: {r.status_code}")


def test_get_messages(session_id: str):
    print_step("Step 9 — Get Message History")
    r = httpx.get(
        f"{BASE_URL}/chat/sessions/{session_id}/messages",
        headers=_headers(),
    )
    if r.status_code == 200:
        data = r.json()
        msgs = data.get("messages", [])
        print_ok(f"Messages: {len(msgs)} | has_more: {data.get('has_more', False)}")
        for msg in msgs:
            role    = msg["role"].upper()
            preview = msg["content"][:80] + "..." if len(msg["content"]) > 80 else msg["content"]
            print(f"    [{role}] {preview}")
    else:
        print_fail(f"Failed: {r.status_code}")


def test_get_sessions():
    print_step("Step 10 — Get All Sessions (Sidebar)")
    r = httpx.get(
        f"{BASE_URL}/chat/sessions",
        headers=_headers(),
    )
    if r.status_code == 200:
        sessions = r.json()
        print_ok(f"Total sessions: {len(sessions)}")
        for s in sessions[:5]:
            cid     = s['conversation_id'][:8]
            title   = s['title']
            count   = s['message_count']
            print(f"    [{cid}...] '{title}' — {count} messages")
    else:
        print_fail(f"Failed: {r.status_code}")


def test_reaction(session_id: str):
    print_step("Step 11 — Message Reaction (thumbs up)")
    # Get last assistant message
    r = httpx.get(
        f"{BASE_URL}/chat/sessions/{session_id}/messages",
        headers=_headers(),
    )
    if r.status_code != 200:
        print_fail("Could not fetch messages")
        return

    msgs = r.json().get("messages", [])
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    if not assistant_msgs:
        print_fail("No assistant messages found")
        return

    message_id = assistant_msgs[-1]["message_id"]
    r2 = httpx.post(
        f"{BASE_URL}/chat/messages/{message_id}/reaction",
        headers=_headers(),
        json={"reaction": "thumbs_up"},
    )
    if r2.status_code == 200:
        print_ok(f"Reaction saved on message: {message_id[:8]}...")
    else:
        print_fail(f"Failed: {r2.status_code} {r2.text}")


def test_streaming(session_id: str):
    print_step("Step 12 — Streaming Test (SSE)")
    print_info("Sending message via streaming endpoint...")
    events_received = []
    try:
        with httpx.stream(
            "POST",
            f"{BASE_URL}/chat/",
            headers=_headers(),
            json={
                "session_id": session_id,
                "message": "What is Workday used for?",
            },
            timeout=60.0,
        ) as r:
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event_type = line.replace("event:", "").strip()
                    events_received.append(event_type)
                    if event_type in ("agent_thinking", "tool_call", "tool_result", "done"):
                        print_info(f"Event: {event_type}")
                elif line.startswith("data:") and "token" not in events_received[-1:]:
                    pass   # skip data lines for brevity

        print_ok(f"Stream complete. Events received: {events_received}")
    except Exception as e:
        print_fail(f"Streaming error: {str(e)}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  NextGenAMS Agent Engine — API Test")
    if TOKEN:
        print(f"  Auth: Bearer token set")
    else:
        print(f"  Auth: No token (AUTH_ENABLED=false mode)")
    print("="*55)

    test_health()
    test_readiness()
    session_id = create_session()
    test_greeting(session_id)
    test_it_question(session_id)
    test_followup(session_id)
    test_out_of_scope(session_id)
    test_title(session_id)
    test_get_messages(session_id)
    test_get_sessions()
    test_reaction(session_id)
    test_streaming(session_id)

    print(f"\n{'='*55}")
    print(f"  ✅ All tests complete!")
    print(f"  Session ID : {session_id}")
    print(f"  Swagger UI : http://localhost:8080/docs")
    print(f"{'='*55}\n")