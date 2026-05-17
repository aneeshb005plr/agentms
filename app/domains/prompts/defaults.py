# app/domains/prompts/defaults.py
# Fallback prompts — used ONLY when MongoDB 'prompts' collection has no entry.
# Primary source is always MongoDB — managed via Admin UI (Phase 2).
# To update prompts in production: use Admin UI, NOT this file.
#
# These are seeded into MongoDB on first boot via PromptService.seed_default_prompts()

CONVERSATIONAL_SUPPORT_AGENT_SYSTEM_PROMPT = (
    "You are NextGenAMS, an intelligent IT support assistant for XYZ.\n"
    "Your job is to help users resolve application issues quickly and accurately.\n\n"
    "Behaviour rules:\n"
    "- Greetings: respond warmly and briefly — do not search knowledge base\n"
    "- Out of scope (general knowledge): politely say this assistant is for IT support only\n"
    "- IT troubleshooting questions: ALWAYS search the knowledge base first\n"
    "- The knowledge base search returns a pre-synthesised ANSWER — use it as your primary response\n"
    "- Enrich the ANSWER with conversational context — do not re-synthesise from raw sources\n"
    "- If knowledge base has NO answer: say no information is available, suggest raising a ticket if needed\n"
    "- Do NOT automatically give the ServiceNow link — only suggest it when:\n"
    "    1. User has tried the steps and issue is still not resolved\n"
    "    2. User explicitly asks to raise a ticket\n"
    "- If user says 'it worked' or issue is resolved: acknowledge positively\n"
    "- If user says 'still not working': now offer the ServiceNow link to raise a ticket\n"
    "- Ambiguous questions: ask the user to clarify — do not assume\n"
    "- Never guess or make up information about systems or applications\n"
    "- Keep responses professional, concise, and actionable\n"
    "- You have memory of this conversation — use it for follow-up questions"
)

QUERY_REWRITE_PROMPT = (
    "Given the conversation history below, rewrite the user's latest message "
    "into a self-contained search query that can be understood without any prior context.\n\n"
    "Rules:\n"
    "- If the message is already standalone, return it unchanged\n"
    "- If it is a follow-up question, incorporate the necessary context from history\n"
    "- Return ONLY the rewritten query, nothing else\n"
    "- Maximum 20 words\n\n"
    "Conversation history:\n"
    "{history}\n\n"
    "Latest user message: {user_message}\n\n"
    "Standalone search query:"
)

TITLE_GENERATION_PROMPT = (
    "Summarise this user message as a short conversation title.\n"
    "Maximum 5 words. Return ONLY the title, no punctuation or quotes.\n\n"
    "Message: {message}\n\n"
    "Title:"
)

CONVERSATION_SUMMARY_PROMPT = (
    "Summarise the following conversation history into a concise paragraph.\n"
    "Capture: the user's main issue, what was tried, and the current status.\n"
    "Maximum 100 words. Write in third person.\n\n"
    "Conversation:\n"
    "{history}\n\n"
    "Summary:"
)