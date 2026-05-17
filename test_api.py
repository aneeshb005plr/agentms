#!/usr/bin/env python3
"""
NextGenAMS Agent Engine — Quick Test Script
Run: python test_api.py

Tests the full flow:
1. Health check
2. Create session
3. Greeting test
4. IT question test
5. Follow-up test (memory)
6. Get messages
7. Get sessions
"""

import httpx
import json
import sys

BASE_URL = "http://localhost:8080/api/v1"


def print_ok(msg): print(f"  ✅ {msg}")
def print_fail(msg): print(f"  ❌ {msg}")
def print_step(msg): print(f"\n{'─'*50}\n{msg}\n{'─'*50}")


def test_health():
    print_step("Step 1 — Health Check")
    r = httpx.get(f"{BASE_URL}/health")
    if r.status_code == 200:
        print_ok(f"Service running: {r.json()}")
    else:
        print_fail(f"Health check failed: {r.status_code}")
        sys.exit(1)


def create_session() -> str:
    print_step("Step 2 — Create Session")
    r = httpx.post(f"{BASE_URL}/chat/sessions")
    if r.status_code == 200:
        data = r.json()
        session_id = data["conversation_id"]
        print_ok(f"Session created: {session_id}")
        return session_id
    else:
        print_fail(f"Failed: {r.status_code} {r.text}")
        sys.exit(1)


def chat_sync(session_id: str, message: str) -> dict:
    r = httpx.post(
        f"{BASE_URL}/chat/sync",
        json={"session_id": session_id, "message": message},
        timeout=60.0,  # agent can take time
    )
    if r.status_code == 200:
        return r.json()
    else:
        print_fail(f"Failed: {r.status_code} {r.text}")
        return {}


def test_greeting(session_id: str):
    print_step("Step 3 — Greeting Test")
    print(f"  Message: 'Hello'")
    result = chat_sync(session_id, "Hello")
    if result.get("content"):
        print_ok(f"Response: {result['content'][:200]}")
    else:
        print_fail("No response received")


def test_it_question(session_id: str):
    print_step("Step 4 — IT Support Question")
    msg = "My SAP login is not working, I get an authentication error"
    print(f"  Message: '{msg}'")
    result = chat_sync(session_id, msg)
    if result.get("content"):
        print_ok(f"Response: {result['content'][:300]}")
        if result.get("ticket_url"):
            print_ok(f"Ticket URL: {result['ticket_url']}")
    else:
        print_fail("No response received")


def test_followup(session_id: str):
    print_step("Step 5 — Follow-up Test (Memory)")
    msg = "I tried that but it still doesn't work"
    print(f"  Message: '{msg}'")
    result = chat_sync(session_id, msg)
    if result.get("content"):
        print_ok(f"Response: {result['content'][:300]}")
    else:
        print_fail("No response received")


def test_out_of_scope(session_id: str):
    print_step("Step 6 — Out of Scope Test")
    msg = "What is the capital of France?"
    print(f"  Message: '{msg}'")
    result = chat_sync(session_id, msg)
    if result.get("content"):
        print_ok(f"Response: {result['content'][:200]}")
    else:
        print_fail("No response received")


def test_get_messages(session_id: str):
    print_step("Step 7 — Get Message History")
    r = httpx.get(f"{BASE_URL}/chat/sessions/{session_id}/messages")
    if r.status_code == 200:
        data   = r.json()
        msgs   = data.get("messages", [])
        print_ok(f"Messages in conversation: {len(msgs)}")
        for msg in msgs:
            role    = msg["role"].upper()
            preview = msg["content"][:80] + "..." if len(msg["content"]) > 80 else msg["content"]
            print(f"    [{role}] {preview}")
    else:
        print_fail(f"Failed: {r.status_code}")


def test_get_sessions():
    print_step("Step 8 — Get All Sessions (Sidebar)")
    r = httpx.get(f"{BASE_URL}/chat/sessions")
    if r.status_code == 200:
        sessions = r.json()
        print_ok(f"Total sessions: {len(sessions)}")
        for s in sessions[:3]:
            print(f"    [{s['conversation_id'][:8]}...] {s['title']} — {s['message_count']} messages")
    else:
        print_fail(f"Failed: {r.status_code}")


if __name__ == "__main__":
    print("\n🚀 NextGenAMS Agent Engine — API Test\n")

    test_health()
    session_id = create_session()
    test_greeting(session_id)
    test_it_question(session_id)
    test_followup(session_id)
    test_out_of_scope(session_id)
    test_get_messages(session_id)
    test_get_sessions()

    print(f"\n{'='*50}")
    print(f"✅ All tests complete!")
    print(f"   Session ID: {session_id}")
    print(f"   View Swagger: http://localhost:8080/docs")
    print(f"{'='*50}\n")